/* Verification feedback belongs to its archive; a stalled response is not a result. */
(() => {
    const checks = [];
    document.querySelectorAll('.verify-backup').forEach(button => {
        const result = button.closest('td').querySelector('.backup-verification-result');
        if (!result) return;
        const label = button.textContent;
        let active = null;
        function show(message, state) {
            result.textContent = message;
            result.setAttribute('data-state', state);
            result.hidden = false;
        }
        checks.push(() => {
            if (active) active.cancel('Verification interrupted by navigation. Verify again for a current result.');
        });
        button.addEventListener('click', async () => {
            if (active || button.disabled) return;
            const controller = new AbortController();
            let timer;
            const operation = {};
            active = operation;
            const deadline = new Promise((_, reject) => {
                operation.cancel = message => {
                    reject(new Error(message));
                    controller.abort();
                };
                timer = window.setTimeout(() => operation.cancel('Verification timed out and was not confirmed. Verify again when the connection is available.'), 30000);
            });
            button.disabled = true;
            button.textContent = 'Verifying...';
            button.setAttribute('aria-busy', 'true');
            show('Verifying this archive...', 'pending');
            try {
                const request = (async () => {
                    const token = document.querySelector('meta[name="csrf-token"]')?.content || '';
                    const response = await fetch(`/system/backups/${encodeURIComponent(button.dataset.name)}/verify`, {
                        method: 'POST', signal: controller.signal,
                        headers: {'X-CSRFToken': token, 'Accept': 'application/json'}
                    });
                    // Redirect even if an expired-session response body stalls.
                    if (!controller.signal.aborted && AuthResponse.redirectIfUnauthorized(response)) return null;
                    return {response, payload: await AuthResponse.readJson(response)};
                })();
                const outcome = await Promise.race([request, deadline]);
                if (!outcome) return;
                const {response, payload} = outcome;
                const warning = typeof payload.warning === 'string' ? payload.warning.trim() : '';
                if (response.ok && typeof payload.message === 'string' && payload.message.trim() && !payload.error) {
                    show(warning ? `${payload.message} ${warning}` : payload.message, warning ? 'warning' : 'success');
                } else {
                    const messages = {
                        403: 'Your account cannot verify backups. Reload the page to check your access.',
                        404: 'This backup is no longer available. Refresh the page to see the current archives.',
                        409: 'Backup failed integrity or authentication verification. Choose another archive.'
                    };
                    const message = messages[response.status] || 'Verification could not be confirmed. Verify again, or refresh the page if the problem continues.';
                    show(response.status === 409 && warning ? `${message} ${warning}` : message, 'error');
                }
            } catch (error) {
                show(controller.signal.aborted ? error.message : 'Verification could not complete. Check the connection and verify again.', 'error');
            } finally {
                window.clearTimeout(timer);
                if (active === operation) active = null;
                button.disabled = false;
                button.textContent = label;
                button.removeAttribute('aria-busy');
            }
        });
    });
    window.addEventListener('pageshow', event => {
        if (event.persisted) checks.forEach(cancel => cancel());
    });
})();
