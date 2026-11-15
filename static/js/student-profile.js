document.addEventListener('DOMContentLoaded', () => {
    const config = JSON.parse(document.getElementById('profile-config').textContent);
    const form = document.getElementById('edit-student-form');
    const save = document.getElementById('profile-save');
    const toggle = document.getElementById('btn-toggle-status');
    const remove = document.getElementById('btn-permanent-remove');
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
    const controls = [...form.querySelectorAll('input, select, button'), toggle, remove].filter(Boolean);
    let busy = false;
    let confirming = false;

    function feedback(kind, message, isError, uncertain = false) {
        const alert = document.getElementById(kind === 'metadata' ? 'profile-alert' : 'profile-action-alert');
        const reload = document.getElementById(kind === 'metadata' ? 'profile-reload' : 'profile-action-reload');
        alert.hidden = false;
        alert.className = `flash flash--${isError ? 'error' : 'success'}`;
        alert.setAttribute('role', isError ? 'alert' : 'status');
        alert.setAttribute('aria-live', isError ? 'assertive' : 'polite');
        alert.textContent = message;
        reload.hidden = !uncertain;
        if (isError) alert.focus();
    }

    async function write(kind, method, url, body, onSuccess) {
        if (busy) return;
        busy = true;
        const previous = controls.map(control => control.disabled);
        controls.forEach(control => { control.disabled = true; });
        form.setAttribute('aria-busy', 'true');
        const oldLabel = save?.textContent;
        if (kind === 'metadata' && save) save.textContent = 'Saving Changes...';
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 15000);
        try {
            const response = await fetch(url, {
                method, signal: controller.signal,
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf, 'Accept': 'application/json'},
                ...(body === undefined ? {} : {body: JSON.stringify(body)}),
            });
            const data = await AuthResponse.readJson(response);
            if (AuthResponse.redirectIfUnauthorized(response, data)) return;
            if (!response.ok) {
                feedback(kind, data.error || 'The change was rejected. Your entries have been kept.', true);
            } else if (!data.message || !onSuccess(data)) {
                throw new Error('Unconfirmed response');
            }
        } catch (error) {
            feedback(kind, 'The change could not be confirmed. Your entries have been kept. Reload Saved Profile to check the stored result before trying again; reloading replaces unsaved entries.', true, true);
        } finally {
            clearTimeout(timer);
            controls.forEach((control, index) => { control.disabled = previous[index]; });
            form.setAttribute('aria-busy', 'false');
            if (save) save.textContent = oldLabel;
            busy = false;
        }
    }

    if (save) form.addEventListener('submit', event => {
        event.preventDefault();
        if (busy || confirming) return;
        const fields = ['student_id', 'name', 'program_id', 'year_level'];
        const payload = Object.fromEntries(fields.map(id => [id, document.getElementById(id).value]));
        return write('metadata', 'PUT', config.updateUrl, payload, data => {
            const saved = data.student;
            if (!saved || !['student_id', 'name', 'program_name', 'year_level'].every(key => typeof saved[key] === 'string') || !Number.isInteger(saved.program_id)) return false;
            fields.forEach(id => { document.getElementById(id).value = saved[id]; });
            document.getElementById('profile-name').textContent = saved.name;
            document.getElementById('profile-summary').textContent = `Student ID: ${saved.student_id} · Program: ${saved.program_name} · Year: ${saved.year_level}`;
            document.title = `${saved.name} - Profile`;
            feedback('metadata', 'Profile updated successfully.', false);
            return true;
        });
    });

    function confirmChange(message, action) {
        if (busy || confirming) return;
        confirming = true;
        AppModal.confirm(message, () => { confirming = false; return action(); }, () => { confirming = false; });
    }
    if (toggle) toggle.addEventListener('click', () => {
        if (busy || confirming) return;
        const willBeActive = toggle.dataset.active !== 'true';
        const action = () => write('action', 'PUT', config.statusUrl, {is_active: willBeActive}, () => { window.location.reload(); return true; });
        if (willBeActive) return action();
        confirmChange('Deactivate this student? Their biometric data will be permanently purged. Historical attendance remains.', action);
    });
    if (remove) remove.addEventListener('click', () => {
        confirmChange('Permanently remove this student from active use? Biometrics will be deleted and the student will disappear from active rosters. Historical attendance and roster references remain. This cannot be undone.', () =>
            write('action', 'DELETE', config.updateUrl, undefined, () => { window.location.href = config.directoryUrl; return true; }));
    });
});
