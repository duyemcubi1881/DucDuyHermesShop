// App State
let user = null;
let activeDepositOrder = null;
let depositPollInterval = null;
let currentPurchasePackage = null;

// Initial Load
document.addEventListener("DOMContentLoaded", () => {
    checkAuthStatus();
    loadConfig();
});

// Load basic configuration (like minimum deposit, support text)
async function loadConfig() {
    try {
        const res = await fetch("/api/config");
        if (res.ok) {
            const config = await res.json();
            document.getElementById("min-deposit-display").innerText = formatCurrency(config.min_deposit);
            document.getElementById("deposit-amount").min = config.min_deposit;
            document.getElementById("support-text-display").innerText = config.support_text;
        }
    } catch (err) {
        console.error("Lỗi tải cấu hình:", err);
    }
}

// Check if user is logged in
async function checkAuthStatus() {
    try {
        const res = await fetch("/api/user/info");
        if (res.ok) {
            user = await res.json();
            renderAuthenticatedUI();
        } else {
            user = null;
            renderGuestUI();
        }
    } catch (err) {
        user = null;
        renderGuestUI();
    }
}

// UI Rendering
function renderAuthenticatedUI() {
    const navAuth = document.getElementById("nav-auth-section");
    navAuth.innerHTML = `
        <div class="user-nav-box">
            <span class="user-nav-balance"><i class="fa-solid fa-wallet"></i> ${formatCurrency(user.balance)}</span>
            <button class="btn btn-secondary btn-sm" onclick="handleLogout()"><i class="fa-solid fa-sign-out-alt"></i> Đăng Xuất</button>
        </div>
    `;

    document.getElementById("dashboard-section").classList.remove("hidden");
    document.getElementById("user-balance").innerText = formatCurrency(user.balance);
    document.getElementById("user-display-name").innerText = user.username;

    // Keys History Table
    const tbody = document.getElementById("keys-history-body");
    if (user.keys && user.keys.length > 0) {
        tbody.innerHTML = user.keys.map(k => `
            <tr>
                <td><strong>${k.package_name}</strong></td>
                <td><span class="key-code">${k.key}</span></td>
                <td>${k.duration || "N/A"}</td>
                <td>${k.purchased_at}</td>
                <td>
                    <button class="btn btn-secondary btn-sm" onclick="copyToClipboard('${k.key}')">
                        <i class="fa-regular fa-copy"></i> Sao Chép
                    </button>
                </td>
            </tr>
        `).join("");
    } else {
        tbody.innerHTML = `<tr><td colspan="5" class="empty-table">Bạn chưa mua key nào.</td></tr>`;
    }
}

function renderGuestUI() {
    const navAuth = document.getElementById("nav-auth-section");
    navAuth.innerHTML = `
        <button class="btn btn-primary btn-sm" onclick="openAuthModal()"><i class="fa-solid fa-user-lock"></i> Đăng Nhập / Đăng Ký</button>
    `;
    document.getElementById("dashboard-section").classList.add("hidden");
}

// Auth Actions
function openAuthModal() {
    document.getElementById("auth-modal").classList.remove("hidden");
    switchAuthTab('login');
}

function closeAuthModal() {
    document.getElementById("auth-modal").classList.add("hidden");
}

function switchAuthTab(tab) {
    const tabLogin = document.getElementById("tab-login");
    const tabRegister = document.getElementById("tab-register");
    const formLogin = document.getElementById("login-form");
    const formRegister = document.getElementById("register-form");

    if (tab === 'login') {
        tabLogin.classList.add("active");
        tabRegister.classList.remove("active");
        formLogin.classList.remove("hidden");
        formRegister.classList.add("hidden");
    } else {
        tabRegister.classList.add("active");
        tabLogin.classList.remove("active");
        formRegister.classList.remove("hidden");
        formLogin.classList.add("hidden");
    }
}

async function handleAuthSubmit(event, type) {
    event.preventDefault();
    const usernameInput = document.getElementById(`${type}-username`);
    const passwordInput = document.getElementById(`${type}-password`);

    const username = usernameInput.value.trim();
    const password = passwordInput.value;

    try {
        const res = await fetch(`/api/auth/${type}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password })
        });

        const data = await res.json();
        if (res.ok) {
            showToast(type === 'login' ? "Đăng nhập thành công!" : "Đăng ký thành công!", "success");
            closeAuthModal();
            usernameInput.value = "";
            passwordInput.value = "";
            checkAuthStatus();
        } else {
            showToast(data.error || "Có lỗi xảy ra, vui lòng thử lại.", "error");
        }
    } catch (err) {
        showToast("Lỗi kết nối máy chủ.", "error");
    }
}

async function handleLogout() {
    try {
        await fetch("/api/auth/logout", { method: "POST" });
        showToast("Đã đăng xuất.", "success");
        checkAuthStatus();
    } catch (err) {
        showToast("Lỗi kết nối máy chủ.", "error");
    }
}

// Deposit Actions
function openDepositModal() {
    if (!user) {
        openAuthModal();
        showToast("Vui lòng đăng nhập trước khi nạp tiền.", "error");
        return;
    }
    document.getElementById("deposit-modal").classList.remove("hidden");
    document.getElementById("deposit-input-step").classList.remove("hidden");
    document.getElementById("deposit-qr-step").classList.add("hidden");
    document.getElementById("deposit-amount").value = "";
}

// Close Deposit Modal
function closeDepositModal() {
    document.getElementById("deposit-modal").classList.add("hidden");
    stopDepositPolling();
}

async function generateDepositQR() {
    const amountInput = document.getElementById("deposit-amount");
    const amount = parseInt(amountInput.value);

    if (isNaN(amount) || amount < 5000) {
        showToast("Số tiền nạp tối thiểu là 5,000đ.", "error");
        return;
    }

    try {
        const res = await fetch("/api/deposit/create", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ amount })
        });

        const data = await res.json();
        if (res.ok) {
            activeDepositOrder = data; // { order_id, base_amount, transfer_amount, qr_url, bank_number, bank_display, account_name }
            
            // Show QR steps
            document.getElementById("deposit-input-step").classList.add("hidden");
            document.getElementById("deposit-qr-step").classList.remove("hidden");
            
            document.getElementById("deposit-qr-img").src = data.qr_url;
            document.getElementById("deposit-qr-amount").innerText = formatCurrency(data.transfer_amount);
            
            const memoEl = document.getElementById("deposit-qr-memo");
            memoEl.innerHTML = `${data.order_id} <i class="fa-regular fa-copy"></i>`;
            
            document.getElementById("deposit-bank-display").innerText = data.bank_display;
            document.getElementById("deposit-bank-number").innerText = data.bank_number;
            document.getElementById("deposit-account-name").innerText = data.account_name;

            startDepositPolling();
        } else {
            showToast(data.error || "Lỗi tạo hóa đơn nạp.", "error");
        }
    } catch (err) {
        showToast("Lỗi kết nối máy chủ.", "error");
    }
}

function startDepositPolling() {
    stopDepositPolling();
    let oldBalance = user ? user.balance : 0;
    
    // Poll user balance info every 4 seconds to see if it is updated (meaning SePay processed payment!)
    depositPollInterval = setInterval(async () => {
        try {
            const res = await fetch("/api/user/info");
            if (res.ok) {
                const freshUser = await res.json();
                if (freshUser.balance > oldBalance) {
                    showToast(`Nạp tiền thành công! Đã cộng +${formatCurrency(freshUser.balance - oldBalance)}`, "success");
                    user = freshUser;
                    renderAuthenticatedUI();
                    closeDepositModal();
                }
            }
        } catch (err) {
            console.error("Lỗi polling nạp tiền:", err);
        }
    }, 4000);
}

function stopDepositPolling() {
    if (depositPollInterval) {
        clearInterval(depositPollInterval);
        depositPollInterval = null;
    }
    activeDepositOrder = null;
}

function copyMemo() {
    if (activeDepositOrder) {
        copyToClipboard(activeDepositOrder.order_id);
    }
}

// Purchase Actions
function initiatePurchase(pkgId, pkgName, price) {
    if (!user) {
        openAuthModal();
        showToast("Vui lòng đăng nhập trước khi mua hàng.", "error");
        return;
    }

    if (user.balance < price) {
        showToast(`Số dư không đủ! Bạn cần nạp thêm ${formatCurrency(price - user.balance)}`, "error");
        return;
    }

    currentPurchasePackage = { id: pkgId, name: pkgName, price };
    document.getElementById("buy-pkg-name").innerText = pkgName;
    document.getElementById("buy-pkg-price").innerText = formatCurrency(price);
    
    // Wire confirm action
    const confirmBtn = document.getElementById("btn-confirm-purchase");
    confirmBtn.onclick = executePurchase;
    
    document.getElementById("buy-modal").classList.remove("hidden");
}

function closeBuyModal() {
    document.getElementById("buy-modal").classList.add("hidden");
    currentPurchasePackage = null;
}

async function executePurchase() {
    if (!currentPurchasePackage) return;
    
    const confirmBtn = document.getElementById("btn-confirm-purchase");
    confirmBtn.disabled = true;
    confirmBtn.innerHTML = `<div class="spinner" style="border-top-color: #fff;"></div> Đang tạo key...`;

    try {
        const res = await fetch("/api/buy", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ package_id: currentPurchasePackage.id })
        });

        const data = await res.json();
        if (res.ok) {
            showToast(`Mua key thành công! Key đã được thêm vào tài khoản của bạn.`, "success");
            closeBuyModal();
            checkAuthStatus(); // Refresh balance and table
        } else {
            showToast(data.error || "Giao dịch không thành công. Hãy thử lại.", "error");
        }
    } catch (err) {
        showToast("Lỗi kết nối máy chủ.", "error");
    } finally {
        confirmBtn.disabled = false;
        confirmBtn.innerHTML = `Xác Nhận & Trừ Ví`;
    }
}

// Utilities
function formatCurrency(amount) {
    return amount.toLocaleString('vi-VN') + '₫';
}

function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        showToast("Đã sao chép vào bộ nhớ tạm!", "success");
    }).catch(err => {
        showToast("Không thể sao chép tự động.", "error");
    });
}

function showToast(message, type = "success") {
    const toast = document.getElementById("toast");
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `
        <i class="fa-solid ${type === 'success' ? 'fa-circle-check' : 'fa-circle-exclamation'}"></i>
        <span>${message}</span>
    `;
    toast.classList.remove("hidden");
    
    // Hide toast after 4 seconds
    setTimeout(() => {
        toast.classList.add("hidden");
    }, 4000);
}
