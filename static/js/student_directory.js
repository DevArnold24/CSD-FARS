const directoryBody = document.getElementById('directory-body');
const canViewClassData = document.getElementById('directory-class') !== null;
const canViewAttendance = document.getElementById('directory-attendance') !== null;
const canManageStudentStatus = document.getElementById('directory-status') !== null;
const directoryColumnCount = 5 + Number(canViewClassData) + Number(canViewAttendance) + Number(canManageStudentStatus);
const filters = {
    q: document.getElementById('directory-q'),
    program_id: document.getElementById('directory-program'),
    year_level: document.getElementById('directory-year'),
    class_id: document.getElementById('directory-class'),
    biometric_status: document.getElementById('directory-biometric'),
    roster_status: document.getElementById('directory-roster'),
    attendance_status: document.getElementById('directory-attendance'),
    student_status: document.getElementById('directory-status')
};

let directoryTimer = null;
let directoryRequestController = null;
let directoryRequestSequence = 0;
let directoryOffset = 0;
const directoryLimit = 50;
const previousButton = document.getElementById('directory-previous');
const nextButton = document.getElementById('directory-next');
const pageStatus = document.getElementById('directory-page-status');
const directoryFeedback = document.getElementById('directory-feedback');
const retryButton = document.getElementById('directory-retry');

function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
}

function badgeClass(status) {
    if (status === 'Ready') return 'badge badge--success';
    if (status === 'Consent Missing') return 'badge badge--error';
    return 'badge badge--warning';
}

function formatDate(value) {
    // Already normalized to institutional wall time by the server. Parsing it
    // as a browser-local Date can shift it across a daylight-saving gap.
    return value ? String(value) : 'No attendance';
}

function updateSummary(summary) {
    Object.entries(summary).forEach(([key, value]) => {
        const element = document.getElementById(`summary-${key}`);
        if (element) element.textContent = value;
    });
}

function renderRows(students, directoryQuery = '') {
    if (!students.length) {
        directoryBody.innerHTML = `<tr><td colspan="${directoryColumnCount}" style="padding: var(--space-md) 0; color: var(--color-text-muted); text-align: center;">No students match these filters.</td></tr>`;
        return;
    }

    directoryBody.innerHTML = students.map(student => {
        const profileUrl = `/students/${encodeURIComponent(String(student.id))}` +
            (directoryQuery ? `?directory=${encodeURIComponent(directoryQuery)}` : '');
        return `
        <tr class="student-directory-row">
            <td style="padding: var(--space-md) 0; color: var(--color-primary);"><a href="${profileUrl}" class="student-directory-link">${escapeHtml(student.student_id)}</a></td>
            <td style="padding: var(--space-md) 0; font-weight: var(--font-weight-medium);"><a href="${profileUrl}" class="student-directory-link">${escapeHtml(student.name)}</a></td>
            <td style="padding: var(--space-md) 0;">${escapeHtml(student.program)}</td>
            <td style="padding: var(--space-md) 0;">${escapeHtml(student.year_level)}</td>
            <td style="padding: var(--space-md) 0;"><span class="${badgeClass(student.biometric_status)}">${escapeHtml(student.biometric_status)}</span></td>
            ${canManageStudentStatus ? `<td style="padding:var(--space-md) 0;">${student.is_active ? 'Active' : 'Deactivated'}</td>` : ''}
            ${canViewClassData ? `<td style="padding: var(--space-md) 0; color: var(--color-text-muted);">${escapeHtml(student.class_count ? student.class_codes : 'Not in class')}</td>` : ''}
            ${canViewAttendance ? `<td style="padding: var(--space-md) 0; color: var(--color-text-muted);">${escapeHtml(formatDate(student.latest_attendance_at))}</td>` : ''}
        </tr>
    `;
    }).join('');
}

function clearSummary() {
    updateSummary({total:'--', ready:'--', needs_setup:'--', unenrolled:'--', never_attended:'--'});
}

function showDirectoryFeedback(message, error = false) {
    directoryFeedback.textContent = message;
    directoryFeedback.setAttribute('role', error ? 'alert' : 'status');
    directoryFeedback.style.color = error ? 'var(--color-error)' : '';
}

function restoreDirectoryLocation() {
    const params = new URLSearchParams(window.location.search);
    Object.entries(filters).forEach(([key, element]) => {
        if (element) element.value = params.get(key) || (key === 'student_status' ? 'active' : '');
    });
    const offset = Number(params.get('offset') || 0);
    directoryOffset = Number.isSafeInteger(offset) && offset > 0 ? offset : 0;
}

function saveDirectoryLocation(params) {
    const locationParams = new URLSearchParams(params);
    locationParams.delete('limit');
    if (directoryOffset > 0) locationParams.set('offset', directoryOffset);
    else locationParams.delete('offset');
    if (locationParams.get('student_status') === 'active') locationParams.delete('student_status');
    const search = locationParams.toString();
    window.history.replaceState(null, '', window.location.pathname + (search ? '?' + search : ''));
    return search;
}

async function loadDirectory() {
    clearTimeout(directoryTimer);
    const requestSequence = ++directoryRequestSequence;
    if (directoryRequestController) directoryRequestController.abort();
    const controller = new AbortController();
    directoryRequestController = controller;
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 15000);
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, element]) => {
        if (element && element.value.trim()) params.set(key, element.value.trim());
    });
    params.set('limit', directoryLimit);
    params.set('offset', directoryOffset);
    previousButton.disabled = true;
    nextButton.disabled = true;
    retryButton.hidden = true;
    pageStatus.textContent = 'Loading students...';
    showDirectoryFeedback('Loading student directory...');
    clearSummary();
    directoryBody.setAttribute('aria-busy', 'true');
    directoryBody.innerHTML = `<tr><td colspan="${directoryColumnCount}">Loading students...</td></tr>`;
    try {
        const response = await fetch(`/api/students/directory?${params.toString()}`, {
            signal: controller.signal, headers: {'Accept':'application/json'}
        });
        if (requestSequence !== directoryRequestSequence) return;
        const data = await AuthResponse.readJson(response);
        if (requestSequence !== directoryRequestSequence) return;
        if (AuthResponse.redirectIfUnauthorized(response, data)) return;
        if (!response.ok) throw new Error(data.error || 'Unable to load student directory.');
        if (!Array.isArray(data.students) || !data.summary || typeof data.summary !== 'object' ||
            !Number.isSafeInteger(data.summary.total) || data.summary.total < 0 ||
            !Number.isSafeInteger(data.offset) || data.offset < 0 ||
            typeof data.has_next !== 'boolean' || typeof data.has_previous !== 'boolean') {
            throw new Error('The directory returned an incomplete result. Retry loading.');
        }
        directoryOffset = data.offset;
        updateSummary(data.summary);
        renderRows(data.students, saveDirectoryLocation(params));
        previousButton.disabled = !data.has_previous;
        nextButton.disabled = !data.has_next;
        const first = data.students.length ? directoryOffset + 1 : 0;
        const last = directoryOffset + data.students.length;
        pageStatus.textContent = `Showing ${first}–${last} of ${data.summary.total} matching students`;
        showDirectoryFeedback(data.students.length ? '' : 'No students match these filters. Try Clear Filters.');
    } catch (error) {
        if (requestSequence !== directoryRequestSequence) return;
        clearSummary();
        pageStatus.textContent = 'Results unavailable';
        showDirectoryFeedback(timedOut ? 'The directory is taking too long to respond. Retry loading.' :
            error.message || 'Network error loading student directory. Retry loading.', true);
        retryButton.hidden = false;
        directoryBody.innerHTML = `<tr><td colspan="${directoryColumnCount}">Student results could not be loaded.</td></tr>`;
    } finally {
        clearTimeout(timeout);
        if (requestSequence === directoryRequestSequence) {
            directoryBody.removeAttribute('aria-busy');
            directoryRequestController = null;
        }
    }
}

function directoryFilterChanged() {
    clearTimeout(directoryTimer);
    directoryOffset = 0;
    // Invalidate immediately, including while the typing debounce is pending.
    ++directoryRequestSequence;
    if (directoryRequestController) directoryRequestController.abort();
    previousButton.disabled = true;
    nextButton.disabled = true;
    retryButton.hidden = true;
    clearSummary();
    pageStatus.textContent = 'Updating filters...';
    showDirectoryFeedback('Updating filters...');
    directoryBody.innerHTML = `<tr><td colspan="${directoryColumnCount}">Updating filters...</td></tr>`;
    directoryTimer = setTimeout(loadDirectory, 200);
}

Object.values(filters).forEach(element => {
    if (!element) return;
    element.addEventListener(element.tagName === 'INPUT' ? 'input' : 'change', directoryFilterChanged);
});
previousButton.addEventListener('click', () => {
    if (previousButton.disabled) return;
    directoryOffset = Math.max(0, directoryOffset - directoryLimit);
    return loadDirectory();
});
nextButton.addEventListener('click', () => {
    if (nextButton.disabled) return;
    directoryOffset += directoryLimit;
    return loadDirectory();
});
retryButton.addEventListener('click', loadDirectory);
document.getElementById('directory-reset').addEventListener('click', () => {
    Object.entries(filters).forEach(([key, element]) => {
        if (element) element.value = key === 'student_status' ? 'active' : '';
    });
    directoryOffset = 0;
    return loadDirectory();
});
window.addEventListener('popstate', () => { restoreDirectoryLocation(); loadDirectory(); });
restoreDirectoryLocation();
loadDirectory();
