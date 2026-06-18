from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import os
import random
import re
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

VN_TZ = datetime.timezone(datetime.timedelta(hours=7))


def _clean_env(val: str | None) -> str:
    if not val:
        return ""
    v = val.strip().strip('"').strip("'")
    if v.lower().startswith("bearer "):
        v = v[7:].strip()
    return v


_ROOT = Path(__file__).resolve().parent
for _f in (".env", "env"):
    if (_ROOT / _f).exists():
        load_dotenv(_ROOT / _f)
        break
else:
    load_dotenv()

BANK_NUMBER = _clean_env(os.getenv("BANK_NUMBER"))
BANK_NAME = (_clean_env(os.getenv("BANK_NAME", "tpbank")) or "tpbank").lower()
if BANK_NAME in ("msbbank",):
    BANK_NAME = "msb"
if BANK_NAME in ("tpbank", "tp bank"):
    BANK_NAME = "tpbank"
ACCOUNT_NAME = _clean_env(os.getenv("ACCOUNT_NAME", "NGO DUC DUY"))
BANK_DISPLAY = _clean_env(os.getenv("BANK_DISPLAY", "TP BANK"))
SEPAY_TOKEN = _clean_env(os.getenv("SEPAY_TOKEN") or os.getenv("SEPAY_API_KEY"))
ORDER_EXPIRE_SEC = int(os.getenv("ORDER_EXPIRE_MINUTES", "15")) * 60
PUBLIC_URL = _clean_env(
    os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8080"
).rstrip("/")
WEBHOOK_PORT = int(os.getenv("PORT") or os.getenv("WEBHOOK_PORT") or "8080")
SHOP_THUMBNAIL = _clean_env(os.getenv("SHOP_THUMBNAIL", ""))
SUPPORT_TEXT = _clean_env(os.getenv("SUPPORT_TEXT", "Ticket server · DM admin"))
MIN_DEPOSIT = int(os.getenv("MIN_DEPOSIT", "5000"))
USE_UNIQUE_AMOUNT = os.getenv("DEPOSIT_UNIQUE_SUFFIX", "1").lower() not in ("0", "false", "no")

API_AIMBOT_BASE = _clean_env(os.getenv("API_AIMBOT_BASE", "https://aovduy-h4bn.onrender.com")).rstrip("/")
API_ADMIN_USER = _clean_env(os.getenv("API_ADMIN_USER"))
API_ADMIN_PASS = _clean_env(os.getenv("API_ADMIN_PASS"))

PRODUCTS = {
    "aimlock_pro": {
        "label": "AimLock Pro",
        "emoji": "🎯",
        "tagline": "Ghim Đầu Cực Mạnh · Hỗ Trợ Đầy Đủ",
        "server": "AimLock Pro Exe",
        "packages": [
            {"id": "ap_1d", "name": "AimLock Pro 1 Ngày", "price": 15_000, "duration": "1 ngày", "days": 1},
            {"id": "ap_7d", "name": "AimLock Pro 7 Ngày", "price": 60_000, "duration": "7 ngày", "days": 7},
            {"id": "ap_1m", "name": "AimLock Pro 1 Tháng", "price": 150_000, "duration": "1 tháng", "days": 30},
            {"id": "ap_1ob", "name": "AimLock Pro 1 OB", "price": 450_000, "duration": "1 OB", "days": 90},
        ],
    },
}

PKG: dict[str, dict] = {}
for _pk, _pv in PRODUCTS.items():
    for _p in _pv["packages"]:
        PKG[_p["id"]] = {**_p, "product_key": _pk, "product_label": _pv["label"]}


class _VNFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.datetime.fromtimestamp(record.created, tz=VN_TZ)
        return dt.strftime(datefmt or "%d/%m/%Y %H:%M:%S")


_handler = logging.StreamHandler()
_handler.setFormatter(_VNFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("shop")

# ─────────────────────────────────────────────────────────────
# DATA & SESSIONS
# ─────────────────────────────────────────────────────────────

DATA_FILE = _ROOT / "data.json"
API_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=20)
_api_lock: asyncio.Lock | None = None

users: dict[str, dict] = {}  # username -> {password_hash, balance, keys: [ {key, package_name, duration, purchased_at} ]}
orders: dict[str, dict] = {}  # order_id -> {username, base_amount, transfer_amount, paid, created_at, sepay_since_id}
processed_txns: set[str] = set()
active_sessions: dict[str, str] = {}  # session_id -> username
_sepay_auth_failed = False


def _vn_now_str() -> str:
    return datetime.datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def hash_password(password: str) -> str:
    salt = "ducduy_boutique_salt_secure_987"
    return hashlib.sha256((password + salt).encode("utf-8")).hexdigest()


def _load_data() -> None:
    global users, orders, processed_txns
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
        users = d.get("users", {})
        orders = d.get("orders", {})
        processed_txns = set(str(x) for x in d.get("processed_txns", []))
        
        # Compatibility migration if old data exists
        balances_old = d.get("balances", {})
        if balances_old and not users:
            # Migrate old discord balances to guest/migrated accounts if needed
            for uid, bal in balances_old.items():
                uname = f"discord_{uid}"
                users[uname] = {
                    "password_hash": hash_password(str(uid)),
                    "balance": bal,
                    "keys": []
                }
        
        pending = sum(1 for o in orders.values() if not o.get("paid"))
        log.info("Da tai data: %d don (%d cho), %d user", len(orders), pending, len(users))
    except FileNotFoundError:
        log.info("Chua co data.json — tao moi")
    except Exception as e:
        log.error("Loi doc data: %s", e)


def _save_data() -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "users": users,
                    "orders": orders,
                    "processed_txns": sorted(processed_txns)[-5000:],
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        log.error("Loi ghi data: %s", e)


_load_data()


# ─────────────────────────────────────────────────────────────
# API KEY CREATION
# ─────────────────────────────────────────────────────────────

def _duration_payload(pkg: dict) -> dict:
    if pkg.get("hours"):
        return {"duration_hours": int(pkg["hours"])}
    return {"days": int(pkg.get("days") or 1)}


def _extract_key(data: dict) -> str | None:
    if not isinstance(data, dict):
        return None
    k = data.get("key") or data.get("key_string") or data.get("license")
    if isinstance(k, dict):
        k = k.get("key")
    return str(k).strip() if k else None


async def api_create_key(product_key: str, pkg: dict, buyer_name: str) -> str | None:
    """Dang nhap admin + POST /api/createkey — tra ve key string."""
    global _api_lock
    if _api_lock is None:
        _api_lock = asyncio.Lock()
    if not API_ADMIN_USER or not API_ADMIN_PASS:
        log.error("Thieu API_ADMIN_USER / API_ADMIN_PASS tren Render")
        return None

    base = API_AIMBOT_BASE
    note = f"web-{pkg['id']}-{buyer_name}"
    body = {
        **_duration_payload(pkg),
        "key_type": "single_device",
        "created_by": "DucDuyBoutique",
        "note": note,
    }

    async with _api_lock:
        try:
            async with aiohttp.ClientSession(
                timeout=API_TIMEOUT,
                headers={"User-Agent": "DucDuyBoutique/3.0", "Accept": "application/json"},
            ) as session:
                login = await session.post(
                    f"{base}/api/login",
                    json={"username": API_ADMIN_USER, "password": API_ADMIN_PASS},
                )
                if login.status != 200:
                    txt = await login.text()
                    log.error("API login fail %s: %s", login.status, txt[:200])
                    return None

                log.info("API login OK @ %s", base)
                resp = await session.post(f"{base}/api/createkey", json=body)
                raw = await resp.text()
                try:
                    data = json.loads(raw) if raw.strip().startswith("{") else {}
                except json.JSONDecodeError:
                    data = {}

                if resp.status not in (200, 201):
                    log.error("API createkey %s: %s", resp.status, raw[:250])
                    return None

                key = _extract_key(data)
                if key:
                    log.info("API key OK [%s] %s…", pkg["id"], key[:12])
                return key
        except asyncio.TimeoutError:
            log.error("API timeout %s — server co the dang ngu (Render free)", base)
            return None
        except Exception as e:
            log.error("API loi: %s", e)
            return None


# ─────────────────────────────────────────────────────────────
# DEPOSIT ORDERS & QR
# ─────────────────────────────────────────────────────────────

def _make_order_id() -> str:
    base = "NAP" + str(int(time.time()))
    oid, n = base, 0
    while oid in orders:
        n += 1
        oid = base + str(n)
    return oid


def _pending_transfer_amounts() -> set[int]:
    return {
        int(o.get("transfer_amount") or o.get("amount") or 0)
        for o in orders.values()
        if not o.get("paid") and not _order_expired(o)
    }


def _alloc_transfer_amount(base: int) -> int:
    if not USE_UNIQUE_AMOUNT:
        return base
    used = _pending_transfer_amounts()
    for off in range(1, 1000):
        t = base + off
        if t not in used:
            return t
    return base + random.randint(1000, 9999)


def _order_expired(o: dict) -> bool:
    return (time.time() - float(o.get("created_at", 0))) > ORDER_EXPIRE_SEC


def _credit(o: dict) -> int:
    return int(o.get("base_amount") or o.get("amount") or 0)


def _transfer(o: dict) -> int:
    return int(o.get("transfer_amount") or o.get("amount") or 0)


def create_order(username: str, base: int, sepay_since_id: int) -> tuple[str, int, int]:
    oid = _make_order_id()
    transfer = _alloc_transfer_amount(base)
    orders[oid] = {
        "username": username,
        "base_amount": base,
        "transfer_amount": transfer,
        "paid": False,
        "created_at": time.time(),
        "created_at_vn": _vn_now_str(),
        "sepay_since_id": sepay_since_id,
    }
    _save_data()
    return oid, base, transfer


def qr_url(transfer: int, oid: str) -> str:
    name = ACCOUNT_NAME.replace(" ", "%20")
    return (
        f"https://img.vietqr.io/image/{BANK_NAME}-{BANK_NUMBER}-compact2.png"
        f"?amount={transfer}&addInfo={oid}&accountName={name}"
    )


# ─────────────────────────────────────────────────────────────
# SEPAY SYNC
# ─────────────────────────────────────────────────────────────

SEPAY_URL = "https://my.sepay.vn/userapi/transactions/list"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=25)


def _parse_amount(val) -> int:
    try:
        return int(float(val or 0))
    except (TypeError, ValueError):
        return 0


def _txn_amount(txn: dict) -> int:
    for k in ("amount_in", "transferAmount", "amount"):
        v = txn.get(k)
        if v is not None and str(v).strip():
            n = _parse_amount(v)
            if n > 0:
                return n
    return 0


def _txn_text(txn: dict) -> str:
    parts = [
        txn.get("transaction_content"),
        txn.get("content"),
        txn.get("description"),
        txn.get("code"),
        txn.get("reference_number"),
        txn.get("referenceCode"),
    ]
    return " ".join(str(p or "") for p in parts).upper()


def _txn_date(txn: dict) -> str:
    return str(txn.get("transaction_date") or txn.get("transactionDate") or "").strip()


def _txn_ts(txn: dict) -> float | None:
    s = _txn_date(txn)[:19]
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=VN_TZ).timestamp()
    except ValueError:
        return None


def _txn_id(txn: dict) -> int:
    try:
        return int(str(txn.get("id") or "0"))
    except ValueError:
        return 0


def _txn_fp(txn: dict) -> str:
    tid = str(txn.get("id") or "").strip()
    if tid and tid not in ("0", "None"):
        return "id:" + tid
    ref = str(txn.get("reference_number") or txn.get("referenceCode") or "").strip()
    if ref:
        return "ref:" + ref
    return f"fp:{_txn_date(txn)}|{_txn_amount(txn)}|{_txn_text(txn)[:60]}"


def _is_incoming(txn: dict) -> bool:
    t = str(txn.get("transferType") or "").lower()
    if t == "out":
        return False
    if t == "in":
        return True
    try:
        ain = float(txn.get("amount_in") or 0)
        aout = float(txn.get("amount_out") or 0)
        if aout > 0 and ain <= 0:
            return False
        return ain > 0
    except (TypeError, ValueError):
        return _txn_amount(txn) > 0


def _nap_in_text(oid: str, text: str) -> bool:
    if not text:
        return False
    compact = re.sub(r"[^A-Z0-9]", "", text.upper())
    up = oid.upper()
    if up in compact:
        return True
    return any(m == up for m in re.findall(r"NAP\d{8,}", compact))


def _txn_ok_for_order(txn: dict, order: dict) -> bool:
    tid = _txn_id(txn)
    since = int(order.get("sepay_since_id") or 0)
    if tid and since and tid <= since:
        return False
    created = float(order.get("created_at") or 0)
    ts = _txn_ts(txn)
    if ts is None:
        return bool(tid and since and tid > since)
    if ts < created + 2:
        return False
    if ts > created + ORDER_EXPIRE_SEC + 300:
        return False
    return True


def _find_order(txn: dict) -> tuple[str | None, str | None]:
    fp = _txn_fp(txn)
    if fp in processed_txns:
        return None, None
    if not _is_incoming(txn):
        return None, None
    amt = _txn_amount(txn)
    if amt <= 0:
        return None, None

    text = _txn_text(txn)
    pending = [(oid, o) for oid, o in orders.items() if not o.get("paid") and not _order_expired(o)]

    # 1) Ma NAP trong noi dung
    for oid, o in sorted(pending, key=lambda x: x[1]["created_at"], reverse=True):
        if not _nap_in_text(oid, text):
            continue
        if not _txn_ok_for_order(txn, o):
            continue
        if amt >= _transfer(o):
            log.info("Khop NAP %s | +%d | %s", oid, _credit(o), text[:50])
            return oid, fp

    # 2) Dung so tien CK
    for oid, o in sorted(pending, key=lambda x: x[1]["created_at"], reverse=True):
        if amt != _transfer(o):
            continue
        if not _txn_ok_for_order(txn, o):
            continue
        log.info("Khop CK %s | %d | +%d", oid, amt, _credit(o))
        return oid, fp

    return None, None


async def sepay_fetch(limit: int = 50) -> tuple[int, list[dict]]:
    global _sepay_auth_failed
    if not SEPAY_TOKEN or _sepay_auth_failed:
        return (401 if _sepay_auth_failed else 0), []
    headers = {"Authorization": f"Bearer {SEPAY_TOKEN}", "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(SEPAY_URL, headers=headers, params={"limit": limit}, timeout=HTTP_TIMEOUT) as r:
                body = await r.text()
                if r.status == 401:
                    _sepay_auth_failed = True
                    log.error("SePay 401 — cap nhat SEPAY_TOKEN tren Render")
                    return 401, []
                if r.status != 200:
                    log.warning("SePay HTTP %s: %s", r.status, body[:150])
                    return r.status, []
                data = json.loads(body) if body.strip().startswith("{") else {}
                txns = data.get("transactions") or []
                if BANK_NUMBER:
                    want = re.sub(r"\D", "", BANK_NUMBER)

                    def _bank_ok(t: dict) -> bool:
                        acct = re.sub(r"\D", "", str(t.get("account_number") or ""))
                        if not acct:
                            return True
                        return acct == want or acct.endswith(want) or want.endswith(acct)

                    txns = [t for t in txns if _bank_ok(t)]
                return 200, txns
    except Exception as e:
        log.error("SePay loi: %s", e)
        return 0, []


async def sepay_latest_id() -> int:
    st, txns = await sepay_fetch(10)
    if st != 200 or not txns:
        return 0
    return max(_txn_id(t) for t in txns)


async def lock_old_txns() -> None:
    st, txns = await sepay_fetch(100)
    if st != 200:
        return
    added = 0
    for t in txns:
        fp = _txn_fp(t)
        if fp not in processed_txns:
            processed_txns.add(fp)
            added += 1
    if added:
        _save_data()
        log.info("Da khoa %d GD SePay cu (chi nhan CK sau khi tao don moi)", added)


async def confirm_payment(oid: str, fp: str | None = None) -> None:
    o = orders.get(oid)
    if not o or o.get("paid"):
        return
    if fp and fp in processed_txns:
        return

    o["paid"] = True
    o["paid_at"] = time.time()
    if fp:
        processed_txns.add(fp)

    username = o.get("username")
    credit = _credit(o)
    
    if username in users:
        users[username]["balance"] += credit
        _save_data()
        log.info("XAC NHAN %s | +%s | user %s | du %s", oid, credit, username, users[username]["balance"])
    else:
        log.error("Khong tim thay user %s de cong tien cho don %s", username, oid)


# ─────────────────────────────────────────────────────────────
# BACKGROUND SEPAY POLLING
# ─────────────────────────────────────────────────────────────

async def poll_sepay_loop() -> None:
    log.info("Bat dau background loop check GD SePay...")
    while True:
        try:
            pending = [oid for oid, o in orders.items() if not o.get("paid") and not _order_expired(o)]
            if pending and SEPAY_TOKEN and not _sepay_auth_failed:
                st, txns = await sepay_fetch(60)
                if st == 200:
                    for txn in txns:
                        oid, fp = _find_order(txn)
                        if oid:
                            await confirm_payment(oid, fp)
        except Exception as e:
            log.error("Loi trong loop poll SePay: %s", e)
        await asyncio.sleep(12)


# ─────────────────────────────────────────────────────────────
# HTTP WEB CONTROLLERS
# ─────────────────────────────────────────────────────────────

def get_session_user(request: web.Request) -> str | None:
    sid = request.cookies.get("session_id")
    if not sid:
        return None
    return active_sessions.get(sid)


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(_ROOT / "static" / "index.html")


async def get_config(request: web.Request) -> web.Response:
    return web.json_response({
        "min_deposit": MIN_DEPOSIT,
        "support_text": SUPPORT_TEXT
    })


async def auth_register(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        
        if len(username) < 3:
            return web.json_response({"error": "Tên đăng nhập tối thiểu 3 ký tự."}, status=400)
        if len(password) < 6:
            return web.json_response({"error": "Mật khẩu tối thiểu 6 ký tự."}, status=400)
            
        if username in users:
            return web.json_response({"error": "Tên đăng nhập đã tồn tại."}, status=400)
            
        users[username] = {
            "password_hash": hash_password(password),
            "balance": 0,
            "keys": []
        }
        _save_data()
        
        # Auto login
        sid = str(uuid.uuid4())
        active_sessions[sid] = username
        resp = web.json_response({"success": True})
        resp.set_cookie("session_id", sid, max_age=86400 * 30, httponly=True)
        return resp
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def auth_login(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        
        user_obj = users.get(username)
        if not user_obj or user_obj["password_hash"] != hash_password(password):
            return web.json_response({"error": "Tài khoản hoặc mật khẩu không chính xác."}, status=400)
            
        sid = str(uuid.uuid4())
        active_sessions[sid] = username
        resp = web.json_response({"success": True})
        resp.set_cookie("session_id", sid, max_age=86400 * 30, httponly=True)
        return resp
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def auth_logout(request: web.Request) -> web.Response:
    sid = request.cookies.get("session_id")
    if sid in active_sessions:
        active_sessions.pop(sid, None)
    resp = web.json_response({"success": True})
    resp.del_cookie("session_id")
    return resp


async def user_info(request: web.Request) -> web.Response:
    username = get_session_user(request)
    if not username or username not in users:
        return web.json_response({"error": "Chưa đăng nhập"}, status=401)
        
    u = users[username]
    return web.json_response({
        "username": username,
        "balance": u["balance"],
        "keys": u.get("keys", [])
    })


async def deposit_create(request: web.Request) -> web.Response:
    username = get_session_user(request)
    if not username or username not in users:
        return web.json_response({"error": "Chưa đăng nhập"}, status=401)
        
    try:
        body = await request.json()
        amount = int(body.get("amount") or 0)
        if amount < MIN_DEPOSIT:
            return web.json_response({"error": f"Số tiền tối thiểu là {MIN_DEPOSIT:,}đ"}, status=400)
            
        since = await sepay_latest_id() if SEPAY_TOKEN else 0
        oid, base, transfer = create_order(username, amount, since)
        
        return web.json_response({
            "order_id": oid,
            "base_amount": base,
            "transfer_amount": transfer,
            "qr_url": qr_url(transfer, oid),
            "bank_number": BANK_NUMBER,
            "bank_display": BANK_DISPLAY,
            "account_name": ACCOUNT_NAME
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def buy_key(request: web.Request) -> web.Response:
    username = get_session_user(request)
    if not username or username not in users:
        return web.json_response({"error": "Chưa đăng nhập"}, status=401)
        
    try:
        body = await request.json()
        pkg_id = str(body.get("package_id") or "")
        
        p = PKG.get(pkg_id)
        if not p:
            return web.json_response({"error": "Gói sản phẩm không hợp lệ."}, status=400)
            
        u = users[username]
        total = p["price"]
        
        if u["balance"] < total:
            return web.json_response({"error": f"Số dư không đủ. Cần thêm {total - u['balance']:,}đ."}, status=400)
            
        # Call backend API
        key = await api_create_key(p["product_key"], p, username)
        if not key:
            return web.json_response({"error": "Không thể kết nối đến backend API. Thử lại sau 1 phút."}, status=500)
            
        # Deduct balance
        u["balance"] -= total
        
        # Save key
        if "keys" not in u:
            u["keys"] = []
        
        key_record = {
            "key": key,
            "package_name": p["name"],
            "duration": p["duration"],
            "purchased_at": _vn_now_str()
        }
        u["keys"].insert(0, key_record) # Add to beginning of history
        _save_data()
        
        return web.json_response({
            "success": True,
            "key_record": key_record,
            "new_balance": u["balance"]
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def sepay_webhook(request: web.Request) -> web.Response:
    try:
        ct = (request.headers.get("Content-Type") or "").lower()
        if "json" in ct:
            raw = await request.json()
        else:
            post = await request.post()
            raw = {k: v for k, v in post.items()}
            
        txn = raw.get("transaction") or raw.get("data") or raw.get("payload") or raw
        if not isinstance(txn, dict):
            txn = raw
            
        oid, fp = _find_order(txn)
        if oid:
            await confirm_payment(oid, fp)
        return web.json_response({"success": True})
    except Exception as e:
        log.error("Webhook loi: %s", e)
        return web.json_response({"success": False}, status=500)


# ─────────────────────────────────────────────────────────────
# START WEB APP
# ─────────────────────────────────────────────────────────────

async def start_background_tasks(app: web.Application) -> None:
    app['sepay_poll'] = asyncio.create_task(poll_sepay_loop())
    if SEPAY_TOKEN and not _sepay_auth_failed:
        app['sepay_lock'] = asyncio.create_task(lock_old_txns())


async def cleanup_background_tasks(app: web.Application) -> None:
    if 'sepay_poll' in app:
        app['sepay_poll'].cancel()
        try:
            await app['sepay_poll']
        except asyncio.CancelledError:
            pass
    if 'sepay_lock' in app:
        app['sepay_lock'].cancel()
        try:
            await app['sepay_lock']
        except asyncio.CancelledError:
            pass


def make_app() -> web.Application:
    app = web.Application()
    
    # Routes
    app.router.add_get("/", index)
    app.router.add_get("/api/config", get_config)
    app.router.add_post("/api/auth/register", auth_register)
    app.router.add_post("/api/auth/login", auth_login)
    app.router.add_post("/api/auth/logout", auth_logout)
    app.router.add_get("/api/user/info", user_info)
    app.router.add_post("/api/deposit/create", deposit_create)
    app.router.add_post("/api/buy", buy_key)
    app.router.add_post("/webhook", sepay_webhook)
    
    # Static files routing
    app.router.add_static("/static/", path=_ROOT / "static", name="static")
    
    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    
    return app


if __name__ == "__main__":
    log.info("Khoi dong web server tren cong %s", WEBHOOK_PORT)
    app = make_app()
    web.run_app(app, host="0.0.0.0", port=WEBHOOK_PORT, access_log=None)

