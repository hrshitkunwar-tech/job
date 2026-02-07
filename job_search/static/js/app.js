// Job Search MVP - Frontend JavaScript

// Highlight active nav link
document.addEventListener('DOMContentLoaded', () => {
    const path = window.location.pathname;
    document.querySelectorAll('nav a').forEach(link => {
        if (link.getAttribute('href') === path ||
            (path.startsWith('/jobs') && link.getAttribute('href') === '/jobs') ||
            (path === '/' && link.getAttribute('href') === '/dashboard')) {
            link.classList.add('text-indigo-600', 'font-semibold');
            link.classList.remove('text-gray-600');
        }
    });
});

// Generic API helper
async function apiCall(url, method = 'GET', body = null) {
    const opts = {
        method,
        headers: {'Content-Type': 'application/json'},
    };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    if (!res.ok) {
        const err = await res.json().catch(() => ({detail: 'Request failed'}));
        throw new Error(err.detail || 'Request failed');
    }
    return res.json();
}
