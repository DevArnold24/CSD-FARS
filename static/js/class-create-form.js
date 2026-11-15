(() => {
    const form = document.querySelector('.class-create-form');
    if (!form) return;
    const button = form.querySelector('button[type="submit"]');
    const originalLabel = button.textContent;
    const initiallyDisabled = button.disabled;
    let pending = false;
    const error = document.getElementById('class-create-error');
    if (error) error.focus();

    form.addEventListener('submit', event => {
        if (pending || initiallyDisabled) {
            event.preventDefault();
            return;
        }
        if (event.defaultPrevented) return;
        pending = true;
        button.disabled = true;
        button.textContent = 'Creating Class...';
        form.setAttribute('aria-busy', 'true');
    });
    window.addEventListener('pageshow', () => {
        pending = false;
        button.disabled = initiallyDisabled;
        button.textContent = originalLabel;
        form.setAttribute('aria-busy', 'false');
    });
})();
