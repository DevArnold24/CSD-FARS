const sidebarToggle = document.querySelector('.sidebar-toggle');
const sidebar = document.getElementById('app-sidebar');
const sidebarBackdrop = document.querySelector('.sidebar-backdrop');
const mobileSidebarQuery = window.matchMedia('(max-width: 900px)');

// Keep the current page visible when a full-page navigation rebuilds the menu.
// Scroll only the menu, so opening a lower item never moves the page content.
function revealCurrentSidebarItem() {
    const nav = sidebar?.querySelector('.sidebar__nav');
    const current = nav?.querySelector('[aria-current="page"]');
    if (!nav || !current) return;
    const menuBounds = nav.getBoundingClientRect();
    const itemBounds = current.getBoundingClientRect();
    if (itemBounds.bottom > menuBounds.bottom) {
        nav.scrollTop += itemBounds.bottom - menuBounds.bottom;
    } else if (itemBounds.top < menuBounds.top) {
        nav.scrollTop -= menuBounds.top - itemBounds.top;
    }
}

function sidebarFocusableElements() {
    if (!sidebar) return [];
    return Array.from(sidebar.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter(element => !element.hidden && element.offsetParent !== null);
}

function syncSidebarAccessibility(open) {
    if (!sidebar) return;
    const hidden = mobileSidebarQuery.matches && !open;
    sidebar.toggleAttribute('inert', hidden);
    sidebar.setAttribute('aria-hidden', String(hidden));
}

function setSidebarOpen(open, restoreFocus = false) {
    if (!sidebarToggle || !sidebar || !sidebarBackdrop) return;
    open = Boolean(open && mobileSidebarQuery.matches);
    document.body.classList.toggle('sidebar-open', open);
    sidebarToggle.setAttribute('aria-expanded', String(open));
    sidebarBackdrop.setAttribute('aria-hidden', String(!open));
    sidebarBackdrop.tabIndex = -1;
    syncSidebarAccessibility(open);
    if (open) {
        (sidebar.querySelector('[aria-current="page"]') || sidebarFocusableElements()[0])?.focus();
        revealCurrentSidebarItem();
    } else if (restoreFocus) {
        sidebarToggle.focus();
    }
}

sidebarToggle?.addEventListener('click', () => {
    setSidebarOpen(!document.body.classList.contains('sidebar-open'));
});
sidebarBackdrop?.addEventListener('click', () => setSidebarOpen(false, true));
sidebar?.addEventListener('click', event => {
    if (event.target.closest('a') && window.matchMedia('(max-width: 900px)').matches) {
        setSidebarOpen(false);
    }
});
document.addEventListener('keydown', event => {
    const isOpen = document.body.classList.contains('sidebar-open');
    if (event.key === 'Escape' && isOpen) {
        setSidebarOpen(false, true);
        return;
    }
    if (event.key !== 'Tab' || !isOpen || !mobileSidebarQuery.matches) return;
    const focusable = sidebarFocusableElements();
    if (!focusable.length) {
        event.preventDefault();
        return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
    }
});
mobileSidebarQuery.addEventListener('change', event => {
    if (!event.matches) setSidebarOpen(false);
    else syncSidebarAccessibility(document.body.classList.contains('sidebar-open'));
});

syncSidebarAccessibility(false);
revealCurrentSidebarItem();
window.addEventListener('resize', revealCurrentSidebarItem);

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { setSidebarOpen };
}
