(() => {
    const form = document.getElementById('class-edit-form');
    if (!form) return;
    const save = document.getElementById('class-edit-save');
    const archive = document.getElementById('class-edit-archive');
    const error = document.getElementById('class-edit-error');
    const originalLabel = save.textContent;
    let pending = false;
    if (error) error.focus();

    form.addEventListener('submit', event => {
        if (pending) {
            event.preventDefault();
            return;
        }
        if (event.defaultPrevented) return;
        pending = true;
        save.disabled = true;
        archive.disabled = true;
        save.textContent = save.dataset.savingLabel || 'Saving Class...';
        form.setAttribute('aria-busy', 'true');
    });
    window.addEventListener('pageshow', () => {
        pending = false;
        save.disabled = false;
        archive.disabled = false;
        save.textContent = originalLabel;
        form.setAttribute('aria-busy', 'false');
    });
})();
