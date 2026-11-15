/* Explicit, versioned saves. Uncertain outcomes reuse the same durable receipt. */
(() => {
    'use strict';
    const form = document.getElementById('opening-form');
    if (!form) return;
    const input = document.getElementById('opening-minutes');
    const save = document.getElementById('opening-save');
    const refresh = document.getElementById('opening-refresh');
    const status = document.getElementById('opening-status');
    const current = document.getElementById('opening-current');
    const history = document.getElementById('opening-history');
    const key = 'csd-opening-save';
    const preview = document.getElementById('opening-preview');
    const offsetText = form.getAttribute('data-school-offset');
    const schoolOffset = offsetText !== null && offsetText !== '' ? Number(offsetText) : NaN;
    const offsetValid = Number.isInteger(schoolOffset) && Math.abs(schoolOffset) <= 14 * 60;
    const timezoneLabel = offsetValid ? `School-local time (UTC${schoolOffset < 0 ? '−' : '+'}${String(Math.floor(Math.abs(schoolOffset) / 60)).padStart(2, '0')}:${String(Math.abs(schoolOffset) % 60).padStart(2, '0')}).` : 'Times shown in UTC; school timezone unavailable.';
    const timezone = document.getElementById('opening-timezone');
    if (timezone) timezone.textContent = timezoneLabel;
    let version = null, busy = false, pending = null, denied = false, savedMinutes = null;
    function historyTime(raw) {
        if (typeof raw !== 'string' || !/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(raw)) return 'Time unavailable';
        const utc = Date.parse(raw.replace(' ', 'T') + 'Z');
        if (!Number.isFinite(utc)) return 'Time unavailable';
        return new Intl.DateTimeFormat('en', {timeZone:'UTC', year:'numeric', month:'short', day:'numeric', hour:'numeric', minute:'2-digit', hour12:true}).format(new Date(utc + (offsetValid ? schoolOffset : 0) * 60000));
    }
    function showPreview() {
        if (!preview) return;
        const valid = /^(?:0|[1-9]|[12][0-9]|30)$/.test(input.value);
        if (!valid) { preview.textContent = 'Enter a whole number from 0 to 30 to see an example.'; return; }
        const minutes = Number(input.value);
        const example = minutes === 0 ? '9:00 AM' : `8:${String(60 - minutes).padStart(2, '0')} AM`;
        const label = pending ? 'Change awaiting confirmation' : minutes !== savedMinutes ? 'Unsaved preview' : 'Example';
        preview.textContent = `${label}: for a 9:00 AM class, teachers may start attendance at ${example}.`;
    }
    try { pending = JSON.parse(sessionStorage.getItem(key) || 'null'); } catch (_) { /* No stored command. */ }
    function remember(value) {
        pending = value;
        try { value ? sessionStorage.setItem(key, JSON.stringify(value)) : sessionStorage.removeItem(key); } catch (_) { /* Current page still retains its receipt. */ }
    }
    function controls() {
        input.disabled = busy || denied || Boolean(pending) || version === null;
        save.disabled = busy || denied || (!pending && version === null);
        save.textContent = pending ? 'Retry save' : 'Save changes';
        refresh.disabled = busy || denied;
        refresh.textContent = pending ? 'Check save status' : 'Check saved setting';
        showPreview();
    }
    async function exchange(options = {}) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 8000);
        try {
            const url = '/api/settings/attendance-opening' + (options.method ? '' : pending ? '?operation_id=' + encodeURIComponent(pending.operation_id) : '');
            const response = await fetch(url, {...options, signal:controller.signal, cache:'no-store'});
            const data = await response.json();
            if (response.status === 401 || response.status === 403) denied = true;
            return {response, data};
        } finally { clearTimeout(timer); }
    }
    function render(data) {
        if (!data.settings || !Number.isInteger(data.settings.minutes) || !Number.isInteger(data.settings.version) || !Array.isArray(data.history)) throw new Error('Incomplete saved-setting response');
        version = data.settings.version;
        savedMinutes = data.settings.minutes;
        current.textContent = savedMinutes === 0 ? 'Current saved setting: attendance may start at class time.' : `Current saved setting: ${savedMinutes} minutes before class.`;
        history.replaceChildren();
        for (const row of data.history) {
            const tr = document.createElement('tr');
            for (const value of [historyTime(row.created_at), row.actor_name, `${row.previous_minutes} min`, `${row.minutes} min`]) {
                const td = document.createElement('td'); td.textContent = value; td.style.padding = '8px'; tr.appendChild(td);
            }
            history.appendChild(tr);
        }
        if (!data.history.length) {
            const row = document.createElement('tr'), cell = document.createElement('td');
            cell.colSpan = 4; cell.textContent = 'No changes saved yet. The initial setting is 10 minutes.'; row.appendChild(cell); history.appendChild(row);
        }
        if (pending && data.receipt?.operation_id === pending.operation_id && data.receipt.minutes === pending.minutes && data.outcome === 'confirmed') {
            status.textContent = `Saved: ${data.receipt.minutes} minutes before class. Existing sessions keep their times.` + (data.settings.version !== data.receipt.version ? ' Someone saved a newer setting afterward; the current setting is shown above.' : '');
            remember(null);
        } else if (pending) {
            status.textContent = 'We could not confirm your change. Check save status, or select Retry save to safely retry this change.';
        } else status.textContent = 'Choose Save changes to apply an edit.';
        if (!pending) input.value = String(data.settings.minutes);
    }
    async function load() {
        busy = true; controls();
        try {
            const {response, data} = await exchange();
            if (!response.ok) throw new Error(data.error || 'Saved setting unavailable.');
            render(data);
        } catch (error) {
            version = null;
            status.textContent = `${error.message} ${pending ? 'We could not confirm your change. Check again before making another change.' : 'The saved setting could not be loaded. Check your connection and try again.'}`;
        } finally { busy = false; controls(); }
    }
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (busy || denied || (!pending && version === null)) return;
        if (!pending) {
            if (!/^(?:0|[1-9]|[12][0-9]|30)$/.test(input.value)) {
                status.textContent = 'Enter a whole number from 0 to 30 minutes. Nothing was submitted.'; input.focus(); return;
            }
            remember({minutes:Number(input.value), version, operation_id:crypto.randomUUID()});
        }
        busy = true; status.textContent = 'Saving changes. Waiting for confirmation…'; controls();
        try {
            const token = document.querySelector('meta[name="csrf-token"]').content;
            const {response, data} = await exchange({method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':token}, body:JSON.stringify(pending)});
            if (!response.ok) {
                if (data.outcome === 'not_accepted') { remember(null); version = null; }
                throw new Error(data.error || 'Save could not be confirmed.');
            }
            render(data);
        } catch (error) {
            status.textContent = `${error.message} ${pending ? 'We could not confirm your change. Check save status or select Retry save.' : 'No change was accepted. Check the saved setting and review it before saving again.'}`;
        } finally { busy = false; controls(); }
    });
    refresh.addEventListener('click', load);
    input.addEventListener('input', showPreview);
    load();
})();
