// Shared chrome for all pages (Generate / History / Settings): theme init and
// a minimal toast. app.js keeps its own richer toast copy and overrides
// window.showToast after this file loads on the index page.

// The anti-FOUC inline script in <head> has already set
// documentElement.dataset.theme before paint; this only toggles on click.
// Graceful no-op on pages without the #theme-toggle button.
function initTheme() {
    const btn = document.getElementById('theme-toggle');
    if (!btn) return;
    btn.addEventListener('click', () => {
        const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
        document.documentElement.dataset.theme = next;
        try { localStorage.setItem('theme', next); } catch { /* private mode */ }
    });
}

// Minimal toast: plain auto-dismissing banner into #toast-root (falling back
// to the console when the mount point is missing).
function toast(message, type = 'info', ms = null) {
    const root = document.getElementById('toast-root');
    if (!root) { console.log(`[${type}] ${message}`); return; }
    const timeout = ms || (type === 'error' ? 6000 : 3500);
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    const text = document.createElement('span');
    text.className = 'toast-text';
    text.textContent = message;
    el.appendChild(text);
    root.appendChild(el);
    el.addEventListener('click', () => el.remove());
    setTimeout(() => el.classList.add('toast-out'), Math.max(0, timeout - 300));
    setTimeout(() => el.remove(), timeout);
}

window.initTheme = initTheme;
window.showToast = toast; // app.js, when loaded after this, replaces it

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initTheme);
} else {
    initTheme();
}
