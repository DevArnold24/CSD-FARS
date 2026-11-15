(() => {
    const filters = document.getElementById('report-filters');
    const exportForm = document.getElementById('export-form');
    const notice = document.getElementById('filter-notice');
    if (!filters) return;
    // Date edits must still work on a validation-error page without an export.
    const selectRange = event => {
        if (['start_date', 'end_date'].includes(event.target.name)) {
            filters.elements.date_scope.value = 'range';
        }
    };
    filters.addEventListener('input', selectRange);
    filters.addEventListener('change', selectRange);
    if (!exportForm) return;
    const initial = new URLSearchParams(new FormData(filters)).toString();
    const isDirty = () => new URLSearchParams(new FormData(filters)).toString() !== initial;
    const button = exportForm.querySelector('button');
    const empty = button.disabled;
    const changed = () => {
        const dirty = isDirty();
        exportForm.dataset.filtersDirty = String(dirty);
        notice.hidden = !dirty;
        button.disabled = dirty || empty;
    };
    filters.addEventListener('input', changed);
    filters.addEventListener('change', changed);
    window.addEventListener('pageshow', event => {
        if (!event.persisted) return;
        if (!isDirty()) {
            window.location.reload();
        } else {
            // Preserve unsubmitted selections when Back restores an older page.
            changed();
            notice.textContent = 'This page was restored. Apply your filters to load current saved attendance.';
        }
    });
})();
