/* Exact-occurrence controls; an uncertain mutation is never replayed. */
document.querySelectorAll('[data-results-controls]').forEach(panel => {
    const buttons = panel.querySelectorAll('button');
    const link = panel.querySelector('[data-results-link]');
    const message = panel.querySelector('[data-results-message]');
    buttons.forEach(button => button.addEventListener('click', async () => {
        buttons.forEach(item => { item.disabled = true; });
        link.hidden = true;
        link.removeAttribute('href');
        message.textContent = 'Checking the display request…';
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 8000);
        try {
            const response = await fetch(`/api/classes/${panel.dataset.classId}/session/display/${button.dataset.displayAction}`, {
                method: 'POST', credentials: 'same-origin', signal: controller.signal,
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content},
                body: JSON.stringify({session_id: Number(panel.dataset.sessionId)})
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || 'Display access could not be confirmed.');
            if (data.attendance_session_id !== Number(panel.dataset.sessionId) || typeof data.display_enabled !== 'boolean') throw new Error('Unexpected display response.');
            if (data.display_enabled) {
                if (typeof data.display_url !== 'string' || !data.display_url.startsWith('/display/session/')) throw new Error('Unexpected display link.');
                link.href = data.display_url; link.hidden = false;
            }
            panel.querySelector('[data-display-action="deactivate"]').hidden = !data.display_enabled;
            message.textContent = data.display_enabled ? 'Private results link ready. Previous links for this class no longer work.' : 'Display cleared. Attendance records are unchanged.';
            buttons.forEach(item => { item.disabled = false; });
        } catch (error) {
            message.textContent = `${error.message} Reload this report to verify display access before another request. The request was not repeated.`;
        } finally { clearTimeout(timeout); }
    }));
});
