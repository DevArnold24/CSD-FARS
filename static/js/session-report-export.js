/* Download feedback stays beside the report actions without leaving the report. */
(() => {
    const links = document.querySelectorAll('[data-session-export]');
    const status = document.getElementById('session-export-status');
    if (!status || !links.length) return;
    let busy = false;
    links.forEach(button => button.addEventListener('click', async event => {
        if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        if (busy) return;
        busy = true;
        const label = button.textContent;
        links.forEach(link => link.setAttribute('aria-disabled', 'true'));
        button.textContent = 'Preparing Excel…';
        status.textContent = 'Preparing your attendance download…';
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 60000);
        try {
            const response = await fetch(button.href, {
                signal: controller.signal, cache: 'no-store',
                headers: {'Accept': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, application/json'}
            });
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
            const file = await response.blob();
            const url = URL.createObjectURL(file);
            const link = document.createElement('a');
            link.href = url;
            const disposition = response.headers.get('Content-Disposition') || '';
            const filename = disposition.match(/filename=(?:"([^"\r\n]+)"|([^;\r\n]+))/i);
            link.download = filename ? (filename[1] || filename[2].trim()).replace(/[\\/]/g, '_') : button.dataset.sessionExport;
            document.body.appendChild(link);
            try { link.click(); } finally {
                link.remove();
                setTimeout(() => URL.revokeObjectURL(url), 30000);
            }
            status.textContent = 'Your Excel download is ready. Check your browser downloads.';
        } catch (error) {
            status.textContent = error.name === 'AbortError'
                ? 'The download timed out. Please try again.'
                : error.message || 'The download failed. Check your connection and try again.';
        } finally {
            clearTimeout(timeout);
            busy = false;
            links.forEach(link => link.removeAttribute('aria-disabled'));
            button.textContent = label;
        }
    }));
})();
