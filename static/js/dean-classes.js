(function () {
    'use strict';
    const search = document.getElementById('class-search');
    const teacher = document.getElementById('teacher-filter');
    const summary = document.getElementById('class-page-summary');
    if (!search || !teacher || !summary) return;
    const rows = Array.from(document.querySelectorAll('.class-row'));
    const previous = document.getElementById('class-previous');
    const next = document.getElementById('class-next');
    const pageSize = 15;
    let page = 1;
    function render() {
        const query = search.value.trim().toLocaleLowerCase();
        const matching = rows.filter(row => (teacher.value === 'all' || row.dataset.teacher === teacher.value)
            && row.dataset.search.toLocaleLowerCase().includes(query));
        const pages = Math.max(1, Math.ceil(matching.length / pageSize));
        page = Math.min(page, pages);
        rows.forEach(row => { row.hidden = true; });
        const offset = (page - 1) * pageSize;
        matching.slice(offset, offset + pageSize).forEach(row => { row.hidden = false; });
        summary.textContent = matching.length
            ? `${offset + 1}–${Math.min(offset + pageSize, matching.length)} of ${matching.length} classes · Page ${page} of ${pages}`
            : 'No classes match. Change the search or teacher filter.';
        previous.disabled = page <= 1;
        next.disabled = page >= pages;
    }
    search.addEventListener('input', () => { page = 1; render(); });
    teacher.addEventListener('change', () => { page = 1; render(); });
    previous.addEventListener('click', () => { if (page > 1) { page--; render(); } });
    next.addEventListener('click', () => { if (!next.disabled) { page++; render(); } });
    render();
})();
