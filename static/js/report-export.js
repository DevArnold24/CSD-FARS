/* Keep export validation and download errors beside the user's filters. */
(() => {
    const form = document.getElementById('export-form');
    const status = document.getElementById('export-status');
    if (!form || !status) return;
    const button = form.querySelector('button[type="submit"]');
    let busy = false;
    form.addEventListener('submit', async event => {
        event.preventDefault();
        if (busy || form.dataset?.filtersDirty === 'true' || !form.reportValidity()) return;
        const start = form.elements.start_date.value;
        const end = form.elements.end_date.value;
        if (start > end) {
            status.textContent = 'Start date must be on or before end date.';
            form.elements.end_date.focus();
            return;
        }
        busy = true;
        button.disabled = true;
        button.textContent = 'Preparing Excel…';
        status.textContent = 'Preparing your attendance download…';
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 60000);
        try {
            const query = new URLSearchParams(new FormData(form));
            const response = await fetch(`${form.action}?${query}`, {signal: controller.signal, cache: 'no-store'});
            if (!response.ok) {
                const data = await AuthResponse.readJson(response);
                if (AuthResponse.redirectIfUnauthorized(response, data)) return;
                throw new Error(data.error || (response.status === 403
                    ? 'You do not have permission to download this report.'
                    : 'The report could not be prepared. Please try again.'));
            }
            const mime = response.headers.get('Content-Type') || '';
            if (!mime.includes('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')) {
                throw new Error('The server returned an unexpected response. Reload this page and try again.');
            }
            if (response.headers.get('X-Attendance-Record-Count') === '0') {
                status.textContent = 'No attendance records match these dates and filters. Change your selections and try again.';
                return;
            }
            const file = await response.blob();
            const url = URL.createObjectURL(file);
            const link = document.createElement('a');
            link.href = url;
            link.download = start ? `attendance_export_${start}_to_${end}.xlsx` : 'attendance_export_all_dates.xlsx';
            document.body.appendChild(link);
            try { link.click(); } finally {
                link.remove();
                setTimeout(() => URL.revokeObjectURL(url), 30000);
            }
            status.textContent = 'Your Excel download is ready. Check your browser downloads.';
        } catch (error) {
            status.textContent = error.name === 'AbortError'
                ? 'The download timed out. Try a shorter date range or try again.'
                : error.message || 'The download failed. Check your connection and try again.';
        } finally {
            clearTimeout(timeout);
            busy = false;
            button.disabled = form.dataset?.filtersDirty === 'true';
            button.textContent = 'Download Excel';
        }
    });
})();
