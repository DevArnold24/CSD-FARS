document.addEventListener("DOMContentLoaded", () => {
    const enrollmentsContainer = document.querySelector(".enrollments-container");
    const availableList = document.getElementById("available-list");
    const enrolledList = document.getElementById("enrolled-list");
    const searchInput = document.getElementById("student-search");
    const programFilter = document.getElementById("program-filter");
    const yearFilter = document.getElementById("year-filter");
    const btnEnrollSelected = document.getElementById("btn-enroll-selected");
    const selectedCount = document.getElementById("selected-count");

    const config = window.SESSION_CONFIG || {};
    const feedback = document.getElementById('roster-feedback');
    const reloadLink = document.getElementById('roster-reload');
    const previousButton = document.getElementById('roster-previous');
    const nextButton = document.getElementById('roster-next');
    const pageStatus = document.getElementById('roster-page-status');
    const retryButton = document.getElementById('roster-retry');
    const clearButton = document.getElementById('roster-clear');
    const enrolledCount = document.getElementById('roster-enrolled-count');
    let debounceTimer;
    let searchController = null;
    let searchSequence = 0;
    let searchBusy = false;
    let writeBusy = false;
    let uncertainWrite = false;
    let pageNeedsReload = false;
    let offset = 0;
    let hasNext = false;
    const limit = 50;
    const writesLocked = () => config.rosterLocked === true || writeBusy || uncertainWrite || pageNeedsReload;

    // This history entry holds view preferences only, never roster data or selections.
    function rememberView() {
        if (!config.viewerId || !config.classId) return;
        try {
            window.history.replaceState({...window.history.state, rosterView: {
                viewerId: String(config.viewerId), classId: String(config.classId),
                search: searchInput.value.slice(0, 120),
                program: programFilter?.value || '', year: yearFilter?.value || '', offset
            }}, '');
        } catch (_) { /* Restricted browser storage must not block roster work. */ }
    }

    function restoreView() {
        try {
            const view = window.history.state?.rosterView;
            if (!config.viewerId || !view || view.viewerId !== String(config.viewerId) ||
                view.classId !== String(config.classId)) return;
            const validOption = (select, value) => typeof value === 'string' &&
                Array.from(select?.options || []).some(option => option.value === value);
            if (typeof view.search !== 'string' || view.search.length > 120 ||
                !validOption(programFilter, view.program) || !validOption(yearFilter, view.year) ||
                !Number.isSafeInteger(view.offset) || view.offset < 0 || view.offset % limit !== 0) {
                showFeedback('Your previous filters are no longer available. Review the filters before selecting students.', true);
                return;
            }
            searchInput.value = view.search;
            programFilter.value = view.program;
            yearFilter.value = view.year;
            offset = view.offset;
        } catch (_) { /* Search remains usable when browser history is unavailable. */ }
    }

    // Escape HTML helper
    function escapeHTML(str) {
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    function selectedAvailableRows() {
        return Array.from(availableList.querySelectorAll(".batch-select:checked"))
            .map(input => input.closest(".student-row"))
            .filter(Boolean);
    }

    function updateBatchControls() {
        if (!btnEnrollSelected || !selectedCount) return;
        const count = selectedAvailableRows().length;
        selectedCount.innerText = `${count} selected`;
        btnEnrollSelected.disabled = count === 0 || writesLocked() || searchBusy;
    }

    function studentInfoMarkup(student, canViewProfile = true) {
        const name = escapeHTML(student.name);
        const label = canViewProfile
            ? `<a href="/students/${encodeURIComponent(student.id)}" style="color: var(--color-primary); text-decoration: none;" onmouseover="this.style.textDecoration='underline'" onmouseout="this.style.textDecoration='none'">${name}</a>`
            : name;
        return `
            <div>
                <div style="font-weight: var(--font-weight-medium);">${label}</div>
                <div style="font-size: var(--font-size-xs); color: var(--color-text-muted);">${escapeHTML(student.student_id)}</div>
            </div>
        `;
    }

    function createEnrolledRow(student) {
        const row = document.createElement("div");
        row.className = "student-row";
        row.dataset.studentId = student.id;
        row.style.display = "flex";
        row.style.alignItems = "center";
        row.style.justifyContent = "space-between";
        row.style.padding = "var(--space-sm)";
        row.style.borderBottom = "1px solid var(--color-border)";
        row.innerHTML = `
            ${studentInfoMarkup(student)}
            <button class="enrollment-action-btn" data-action="remove" aria-label="Remove ${escapeHTML(student.name)} from this class" style="background: var(--color-error); color: white; border: none; padding: 4px 12px; border-radius: var(--radius-sm); cursor: pointer; font-size: var(--font-size-xs);">Remove</button>
        `;
        return row;
    }

    function addEnrolledStudentRow(student) {
        const existing = enrolledList.querySelector(`.student-row[data-student-id="${student.id}"]`);
        if (existing) return;
        const emptyState = enrolledList.querySelector(".empty-state");
        if (emptyState) {
            emptyState.style.display = "none";
        }
        enrolledList.appendChild(createEnrolledRow(student));
        updateEnrolledEmptyState();
    }

    // Render Available Students list
    function renderAvailableStudents(students) {
        availableList.innerHTML = "";
        if (students.length === 0) {
            updateBatchControls();
            const p = document.createElement("p");
            p.style.color = "var(--color-text-muted)";
            p.style.textAlign = "center";
            p.style.marginTop = "var(--space-md)";
            p.className = "empty-state";
            p.innerText = "No available students found.";
            availableList.appendChild(p);
            return;
        }

        students.forEach(student => {
            const row = document.createElement("div");
            row.className = "student-row";
            row.dataset.studentId = student.id;
            row.dataset.studentName = student.name;
            row.dataset.studentPublicId = student.student_id;
            row.style.display = "flex";
            row.style.alignItems = "center";
            row.style.justifyContent = "space-between";
            row.style.padding = "var(--space-sm)";
            row.style.borderBottom = "1px solid var(--color-border)";

            row.innerHTML = `
                <div style="display: flex; align-items: center; gap: var(--space-sm); min-width: 0;">
                    <input type="checkbox" class="batch-select" aria-label="Select ${escapeHTML(student.name)}" style="width: 16px; height: 16px;">
                    ${studentInfoMarkup(student, window.SESSION_CONFIG?.canViewUnenrolledProfiles === true)}
                </div>
                <button class="enrollment-action-btn" data-action="add" aria-label="Add ${escapeHTML(student.name)} to this class" style="background: var(--color-success); color: white; border: none; padding: 4px 12px; border-radius: var(--radius-sm); cursor: pointer; font-size: var(--font-size-xs);">Add</button>
            `;
            availableList.appendChild(row);
        });
        updateBatchControls();
    }

    function syncControls() {
        const locked = writesLocked() || searchBusy;
        enrollmentsContainer.querySelectorAll('.enrollment-action-btn').forEach(button => { button.disabled = locked; });
        availableList.querySelectorAll('.batch-select').forEach(input => { input.disabled = locked; });
        [searchInput, programFilter, yearFilter, clearButton, retryButton].filter(Boolean)
            .forEach(control => { control.disabled = writeBusy; });
        if (previousButton) previousButton.disabled = writeBusy || searchBusy || offset === 0;
        if (nextButton) nextButton.disabled = writeBusy || searchBusy || !hasNext;
        updateBatchControls();
    }

    function showFeedback(message, error = false, needsReload = false) {
        if (feedback) {
            feedback.textContent = message;
            feedback.className = `flash flash--${error ? 'error' : 'success'}`;
            feedback.hidden = false;
            feedback.setAttribute('role', error ? 'alert' : 'status');
            feedback.focus();
        } else if (error) AppModal.alert(message);
        if (reloadLink) {
            reloadLink.hidden = !needsReload;
            reloadLink.textContent = pageNeedsReload ? 'Reload page' : 'Reload Saved Roster';
        }
    }

    async function rosterRequest(url, options = {}, controller = new AbortController()) {
        let timer;
        const deadline = new Promise((_, reject) => {
            timer = setTimeout(() => {
                controller.abort();
                reject(new Error('The request is taking too long to respond.'));
            }, 15000);
        });
        try {
            return await Promise.race([(async () => {
                const response = await fetch(url, {...options, signal: controller.signal});
                const data = await AuthResponse.readJson(response);
                return {response, data};
            })(), deadline]);
        } finally { clearTimeout(timer); }
    }

    function clearAvailable(message) {
        availableList.replaceChildren();
        const text = document.createElement('p');
        text.className = 'empty-state';
        text.textContent = message;
        availableList.appendChild(text);
        updateBatchControls();
    }

    async function performSearch() {
        if (writeBusy) return;
        clearTimeout(debounceTimer);
        const sequence = ++searchSequence;
        if (searchController) searchController.abort();
        const controller = new AbortController();
        searchController = controller;
        searchBusy = true;
        rememberView();
        let url = `/api/students/search?paged=1&offset=${offset}&q=${encodeURIComponent(searchInput.value.trim())}&program_id=${encodeURIComponent(programFilter?.value || '')}&year_level=${encodeURIComponent(yearFilter?.value || '')}`;
        if (config.classId) url += `&class_id=${encodeURIComponent(config.classId)}`;
        availableList.setAttribute('aria-busy', 'true');
        clearAvailable('Searching available students...');
        if (pageStatus) pageStatus.textContent = 'Loading students...';
        if (retryButton) retryButton.hidden = true;
        syncControls();
        try {
            const {response, data} = await rosterRequest(url, {headers:{'Accept':'application/json'}}, controller);
            if (sequence !== searchSequence) return;
            if (AuthResponse.redirectIfUnauthorized(response, data)) return;
            if (!response.ok) throw new Error(data.error || 'Unable to search students.');
            if (!Array.isArray(data.students) || !Number.isSafeInteger(data.total) || data.total < 0 ||
                !Number.isSafeInteger(data.offset) || data.offset < 0 || typeof data.has_next !== 'boolean') {
                throw new Error('Student search returned an incomplete result. Retry loading.');
            }
            offset = data.offset;
            rememberView();
            hasNext = data.has_next;
            renderAvailableStudents(data.students);
            if (pageStatus) pageStatus.textContent = `Showing ${data.students.length ? offset+1 : 0}–${offset+data.students.length} of ${data.total} available students`;
        } catch (error) {
            if (sequence !== searchSequence) return;
            clearAvailable(error.message || 'Student search could not be loaded.');
            availableList.querySelector('.empty-state')?.setAttribute('role','alert');
            if (pageStatus) pageStatus.textContent = 'Results unavailable';
            if (retryButton) retryButton.hidden = false;
            hasNext = false;
        } finally {
            if (sequence === searchSequence) {
                searchBusy = false;
                searchController = null;
                availableList.removeAttribute('aria-busy');
                syncControls();
            }
        }
    }

    function searchChanged(delayed = false) {
        if (writeBusy) return;
        clearTimeout(debounceTimer);
        ++searchSequence;
        if (searchController) searchController.abort();
        offset = 0;
        rememberView();
        searchBusy = true;
        clearAvailable('Updating filters...');
        if (pageStatus) pageStatus.textContent = 'Updating filters...';
        if (retryButton) retryButton.hidden = true;
        syncControls();
        if (delayed) debounceTimer = setTimeout(performSearch, 300);
        else return performSearch();
    }

    function updateEnrolledEmptyState() {
        const rows = enrolledList.querySelectorAll('.student-row');
        let empty = enrolledList.querySelector('.empty-state');
        if (!rows.length && !empty) {
            empty = document.createElement('p');
            empty.className = 'empty-state';
            empty.textContent = 'No students currently enrolled.';
            enrolledList.appendChild(empty);
        }
        if (empty) empty.style.display = rows.length ? 'none' : 'block';
        if (enrolledCount) enrolledCount.textContent = `(${rows.length})`;
    }

    async function changeRoster(body, students, trigger, batch = false) {
        if (writesLocked() || searchBusy) return;
        writeBusy = true;
        syncControls();
        const label = trigger.innerText;
        trigger.innerText = 'Saving...';
        let success = false;
        try {
            const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const url = batch ? config.batchEnrollmentsApiUrl : config.enrollmentsApiUrl;
            const {response, data} = await rosterRequest(url, {
                method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':csrf,'Accept':'application/json'},
                body:JSON.stringify(body)
            });
            if (AuthResponse.redirectIfUnauthorized(response, data)) return;
            if (!response.ok) {
                // Match the existing server CSRF rejection contract, not arbitrary errors.
                if (response.status === 400 && data.error === 'CSRF token missing or expired.') {
                    pageNeedsReload = true;
                    showFeedback("This page needs refreshing. Your change wasn't saved. Reload the page, then select the students and try again.", true, true);
                    return;
                }
                if (response.status >= 500) throw new Error('Unconfirmed server result');
                showFeedback(data.error || 'The roster change was rejected. Your selection has been kept.', true);
                return;
            }
            if (typeof data.message !== 'string' || !data.message) throw new Error('Unconfirmed response');
            if (batch) {
                const requested = new Set(students.map(student => String(student.id)));
                const confirmed = data.enrolled_student_ids;
                if (!Array.isArray(confirmed) || confirmed.some(id => !Number.isSafeInteger(id) || id <= 0 || !requested.has(String(id)))) {
                    throw new Error('Unconfirmed membership');
                }
                const confirmedIds = new Set(confirmed.map(String));
                students.filter(student => confirmedIds.has(String(student.id))).forEach(addEnrolledStudentRow);
            } else {
                if (String(data.student_id) !== String(body.student_id) || data.action !== body.action) throw new Error('Unconfirmed membership');
                if (body.action === 'add') addEnrolledStudentRow(students[0]);
                else enrolledList.querySelector(`.student-row[data-student-id="${body.student_id}"]`)?.remove();
            }
            updateEnrolledEmptyState();
            showFeedback(data.message);
            success = true;
        } catch (error) {
            uncertainWrite = true;
            showFeedback('The roster change could not be confirmed. Reload Saved Roster to check what was saved before making another change.', true, true);
        } finally {
            writeBusy = false;
            trigger.innerText = label;
            syncControls();
        }
        if (success) await performSearch();
    }

    reloadLink?.addEventListener('click', event => {
        event.preventDefault();
        // Reload this GET page, preserving view history; never resubmit the rejected POST.
        window.location.reload();
    });
    searchInput.addEventListener('input', () => searchChanged(true));
    programFilter?.addEventListener('change', () => searchChanged());
    yearFilter?.addEventListener('change', () => searchChanged());
    clearButton?.addEventListener('click', () => {
        if (writeBusy) return;
        [searchInput, programFilter, yearFilter].filter(Boolean).forEach(input => { input.value = ''; });
        return searchChanged();
    });
    retryButton?.addEventListener('click', performSearch);
    previousButton?.addEventListener('click', () => {
        if (previousButton.disabled) return;
        offset = Math.max(0, offset-limit); return performSearch();
    });
    nextButton?.addEventListener('click', () => {
        if (nextButton.disabled) return;
        offset += limit; return performSearch();
    });
    enrollmentsContainer.addEventListener('click', event => {
        const button = event.target.closest('.enrollment-action-btn');
        if (!button || button.disabled || writesLocked() || searchBusy) return;
        const row = button.closest('.student-row');
        const student = {id:row.dataset.studentId, name:row.dataset.studentName, student_id:row.dataset.studentPublicId};
        return changeRoster({action:button.dataset.action,student_id:student.id}, [student], button);
    });
    availableList.addEventListener('change', event => {
        if (event.target.classList.contains('batch-select')) updateBatchControls();
    });
    btnEnrollSelected?.addEventListener('click', () => {
        if (btnEnrollSelected.disabled || writesLocked() || searchBusy) return;
        const students = selectedAvailableRows().map(row => ({id:row.dataset.studentId,name:row.dataset.studentName,student_id:row.dataset.studentPublicId}));
        if (!students.length) return;
        return changeRoster({student_ids:students.map(student => student.id)}, students, btnEnrollSelected, true);
    });
    restoreView();
    updateEnrolledEmptyState();
    performSearch();
});
