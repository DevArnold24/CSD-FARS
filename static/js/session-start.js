/* Reviewed Start UI. Recording authority remains in the shared lifecycle. */
(() => {
    if (!window.SESSION_CONFIG.startProtocol) return;
    const box = document.getElementById('session-start-readiness');
    const modeSelect = document.getElementById('session-start-mode');
    const summary = document.getElementById('session-start-summary');
    const explanation = document.getElementById('session-start-explanation');
    const ackLabel = document.getElementById('session-start-ack-label');
    const ack = document.getElementById('session-start-ack');
    const refresh = document.getElementById('session-start-refresh');
    const fallback = document.getElementById('session-start-fallback');
    const reason = document.getElementById('session-start-reason');
    const storageKey = `csd-pending-start-${classId}`;
    function readinessExplanation(message) {
        // Translate these known blockers without changing their conditions.
        // Other server explanations remain visible verbatim.
        const wording = {
            'Continuation is unavailable. Check the original cutoff, End receipt, clock review and confirmed cleanup.':
                'You cannot continue this session right now. It must still be before its original end time, attendance must not have been ended, the server time must be reliable, and the camera stop must be confirmed.',
            'Camera cleanup is unconfirmed. Its owner reservations must be resolved before continuation.':
                'The camera stop is not confirmed. The camera remains reserved. Ask the server operator to check it before continuing.'
        };
        return wording[message] || message;
    }
    let details = null, loading = false, checking = false, generation = 0;
    let mode = hasCamera ? 'camera' : 'manual';
    let action = activeSessionId ? 'resume' : 'start';
    let pending = null, lastVersion = lifecycleState?.version;
    try { pending = JSON.parse(window.sessionStorage?.getItem(storageKey) || 'null'); } catch (_) { /* Unavailable storage: keep current tab state. */ }
    if (pending && typeof pending.operation_id !== 'string') pending = null;
    function remember(value) {
        pending = value;
        try {
            if (value) window.sessionStorage?.setItem(storageKey, JSON.stringify(value));
            else window.sessionStorage?.removeItem(storageKey);
        } catch (_) { /* Server receipts remain authoritative if local storage is unavailable. */ }
    }
    async function exchange(url, options = {}) {
        const controller = new AbortController();
        let timeout;
        const bound = new Promise((_, reject) => { timeout = setTimeout(() => { controller.abort(); reject(new Error('The attendance check took too long. Check again.')); }, 8000); });
        try {
            const response = await Promise.race([fetch(url, {...options, signal: controller.signal, cache:'no-store'}), bound]);
            if (response.status === 401 || response.status === 403) {
                attendancePoller.deny(response.status);
                throw new Error('Access unavailable');
            }
            return {response, data: await Promise.race([response.json(), bound])};
        } finally { clearTimeout(timeout); }
    }
    function controls() {
        if (!box || authExpired) return;
        const state = lifecycleState;
        const frozen = Boolean(activeSessionId);
        const starting = state?.collection === 'starting';
        box.hidden = Boolean(starting || state?.collection_allowed || (state?.end_operation && !['reopen', 'resume_ended'].includes(action)));
        const attendanceLog = document.getElementById('session-attendance-log');
        if (attendanceLog) attendanceLog.hidden = !activeSessionId;
        document.getElementById('session-roster-preview').hidden = Boolean(activeSessionId || pending);
        modeSelect.value = mode;
        modeSelect.disabled = frozen || loading || Boolean(pending);
        fallback.hidden = !state?.startup?.manual_fallback_available || action === 'manual_fallback';
        fallback.disabled = controlsLocked() || loading || Boolean(pending);
        reason.hidden = !['manual_fallback', 'reopen', 'resume_ended'].includes(action);
        reason.setAttribute('aria-label', action === 'resume_ended' ? 'Reason for resuming until scheduled end' : action === 'reopen' ? 'Reason for reopening camera attendance' : 'Reason for manual continuation');
        reason.placeholder = action === 'resume_ended' ? 'Explain why attendance needs to resume (required)' : action === 'reopen' ? 'Explain why this session needs additional camera attendance (required)' : 'Reason for continuing manually (required)';
        refresh.disabled = loading || checking || authExpired;
        ackLabel.hidden = !details?.requires_acknowledgment;
        btnStart.disabled = controlsLocked() || loading || Boolean(pending) || !details?.can_confirm
            || (details.requires_acknowledgment && !ack.checked)
            || (['manual_fallback', 'reopen', 'resume_ended'].includes(action) && !reason.value?.trim());
        if (!starting && !state?.collection_allowed && (!state?.end_operation || ['reopen', 'resume_ended'].includes(action))) {
            btnStart.style.display = 'inline-block';
            btnStart.textContent = loading ? 'Checking attendance details…' : action === 'manual_fallback'
                ? 'Confirm manual continuation' : action === 'resume_ended' ? 'Confirm resume until scheduled end' : action === 'reopen' ? 'Confirm camera reopening' : action === 'resume' ? `Resume ${mode} attendance` : details?.early_start ? 'Start attendance early' : 'Start attendance';
        }
        if (starting) {
            summary.textContent = 'Preparing the camera. Attendance recording has not started yet.';
            explanation.textContent = state.message;
            ackLabel.hidden = true;
            btnStart.style.display = 'none';
            btnEnd.style.display = state.end_available ? 'inline-block' : 'none';
        }
    }
    async function check() {
        if (authExpired || loading || pending) { if (pending) reconcile(); return; }
        const requestGeneration = ++generation;
        loading = true;
        details = null;
        ack.checked = false;
        controls();
        summary.textContent = 'Checking current roster, schedule and selected mode…';
        explanation.textContent = 'Waiting for the current attendance details. Start is unavailable while this check is running.';
        document.getElementById('session-start-timing').textContent = 'Times will be shown when the check finishes.';
        document.getElementById('session-start-camera').textContent = 'Checking the selected attendance method…';
        try {
            const dateContext = window.SESSION_CONFIG.sessionDate ? `&session_date=${encodeURIComponent(window.SESSION_CONFIG.sessionDate)}` : '';
            const {response, data} = await exchange(`/api/classes/${classId}/session/readiness?mode=${mode}&action=${action}${dateContext}${['reopen', 'resume_ended'].includes(action) && activeSessionId ? `&session_id=${activeSessionId}` : ''}`);
            if (authExpired || requestGeneration !== generation) return;
            if (!response.ok) throw new Error(data.error || 'Attendance details could not be checked.');
            if (data.mode !== mode || data.action !== action || !Array.isArray(data.blockers)) throw new Error('Some attendance details are missing. Check again.');
            if (window.SESSION_CONFIG.sessionDate && data.date !== window.SESSION_CONFIG.sessionDate) throw new Error('The session date changed. Return to Sessions and choose the intended meeting.');
            details = data;
            summary.textContent = `${data.class_name} · ${data.date} · ${data.room} · ${data.eligible_count} students included · ${mode} attendance`;
            document.getElementById('session-start-timing').textContent = `Attendance opens: ${schoolTimeLabel(data.opening_label || "Unavailable")} · Class starts: ${schoolTimeLabel(data.scheduled_start.replace('T', ' '))} · Late after: ${schoolTimeLabel(data.late_label)} · Attendance ends: ${schoolTimeLabel(data.cutoff_label)}`;
            if (action === 'reopen' && data.reopening_end_label) document.getElementById('session-start-timing').textContent = `Original class start: ${schoolTimeLabel(data.scheduled_start.replace('T', ' '))} · Original late threshold: ${schoolTimeLabel(data.late_label)} · Expected end if confirmed now: ${schoolTimeLabel(data.reopening_end_label)}. Up to 10 minutes from confirmation; latest permitted end: ${schoolTimeLabel(data.cutoff_label)}.`;
            document.getElementById('session-start-camera').textContent = mode === 'manual'
                ? 'You will mark each student’s attendance. The assigned camera will not change.'
                : `${data.camera_name || 'No camera assigned'} · ${data.camera_readiness}`;
            if (action === 'resume_ended') document.getElementById('session-start-timing').textContent = `Class start: ${schoolTimeLabel(data.scheduled_start.replace('T', ' '))} · Late threshold: ${schoolTimeLabel(data.late_label)} · Attendance ends: ${schoolTimeLabel(data.cutoff_label)}. Resuming does not add attendance time.`;
            explanation.textContent = (action === 'resume_ended' ? `${Number.isInteger(data.pending_count) ? `${data.pending_count} ${data.pending_count === 1 ? 'student is' : 'students'} still Pending. ` : ''}Resume this same session until its scheduled end. Existing results are protected; an explanation is required. ` : action === 'reopen' ? `${data.pending_count} students still Pending. One reopening only, for up to 10 minutes and no later than the attendance end shown. Existing results are protected; only Pending students can receive new camera results. ` : '') + ([...data.blockers, ...data.warnings].map(readinessExplanation).join(' ') || 'Check these details, then confirm when ready.');
        } catch (error) {
            if (!authExpired) explanation.textContent = error.message || 'Attendance details could not be confirmed. Check again; nothing was started.';
        } finally { loading = false; controls(); }
    }
    function validStart(data) {
        const state = data?.lifecycle;
        return data.outcome === 'accepted' && Number.isInteger(data.session_id)
            && ['camera', 'manual'].includes(data.mode) && state?.session_id === data.session_id
            && Number.isInteger(state.version) && Number.isFinite(state.cutoff) && Number.isFinite(state.late_at)
            && typeof state.collection_allowed === 'boolean' && typeof state.end_available === 'boolean'
            && typeof state.resume_available === 'boolean' && typeof data.session_state === 'string';
    }
    function adopt(data) {
        if (!validStart(data) || authExpired) return false;
        // A template for a different mode is reloaded deliberately, before any
        // controls can act on a stale camera/manual layout.
        if ((data.mode === 'camera') !== hasCamera) {
            remember(null);
            window.location.reload();
            return true;
        }
        remember(null);
        attendancePoller.mutationUncertain = false;
        sessionReloadRequired = false;
        activeSessionId = data.session_id;
        actualStartedAtStr = data.actual_started_at;
        runtimeState = data.session_state;
        mode = data.mode;
        action = 'resume';
        details = null;
        automaticStopHandled = false;
        connectionHealthy = true;
        applyLifecycle(data.lifecycle);
        updateCountdown();
        handleConnectionState({state:'connected', lastSuccess:Date.now(), mutating:false, mutationUncertain:false});
        if (lifecycleState.collection === 'interrupted') check();
        controls();
        return true;
    }
    async function reconcile() {
        if (!pending || checking || authExpired) return;
        checking = true;
        controls();
        try {
            const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
            const {response, data} = await exchange(`/api/classes/${classId}/session/start/reconcile`, {
                method:'POST', headers:{'Content-Type':'application/json', 'X-CSRFToken':csrf}, body:JSON.stringify(pending)});
            if (response.ok && data.outcome === 'accepted') {
                if (!adopt(data)) throw new Error('The response is missing session details. Reload to check whether attendance started.');
            } else if (response.ok && data.outcome === 'not_accepted') {
                remember(null);
                attendancePoller.mutationUncertain = false;
                explanation.textContent = data.message;
                check();
            } else explanation.textContent = data.message || data.error || 'We could not confirm whether attendance started. Reload to check. No second Start was sent.';
        } catch (_) {
            if (!authExpired) explanation.textContent = 'We could not confirm whether attendance started. Reload before trying another action. No second Start was sent.';
        } finally { checking = false; controls(); }
    }
    btnStart.addEventListener('click', async () => {
        if (btnStart.disabled || controlsLocked() || !details || pending) return;
        const operation_id = window.crypto?.randomUUID?.() || `start-${Date.now()}-${Math.random().toString(36).slice(2)}`;
        const body = {operation_id, mode, action, review_token:details.review_token,
            acknowledge_partial:ack.checked, reason:['manual_fallback', 'reopen', 'resume_ended'].includes(action) ? reason.value.trim() : ''};
        remember({operation_id, review_token:details.review_token});
        explanation.textContent = 'Starting attendance. Waiting for confirmation…';
        controls();
        try {
            const result = await submitSessionAction(`/api/classes/${classId}/session/start`, {
                headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}, validStart);
            if (!isCurrentAction(result)) { reconcile(); return; }
            if (result.response.ok) {
                if (!adopt(result.data)) reconcile();
            } else {
                remember(null);
                details = null;
                explanation.textContent = result.data.error || 'Attendance did not start. Check the current details.';
                const cause = explanation.textContent;
                await check();
                if (!authExpired) explanation.textContent = `${cause} ${explanation.textContent}`;
            }
        } finally { controls(); }
    });
    modeSelect.addEventListener('change', () => {
        if (activeSessionId || pending) return;
        mode = modeSelect.value === 'manual' ? 'manual' : 'camera';
        check();
    });
    refresh.addEventListener('click', () => pending ? reconcile() : check());
    ack.addEventListener('change', controls);
    reason.addEventListener('input', controls);
    fallback.addEventListener('click', () => {
        if (fallback.disabled) return;
        mode = 'manual'; action = 'manual_fallback'; check();
    });
    window.CsdSessionStart = {controls, pending:() => Boolean(pending), reconcile, adopt,
        observe:() => { if (pending) reconcile(); },
        update:state => {
            if (!state || pending) return;
            if (lastVersion !== state.version) {
                lastVersion = state.version; details = null;
                if (state.startup) mode = state.startup.mode;
                action = 'resume';
                if (state.collection === 'interrupted') check();
            }
            controls();
        },
        clear:() => {
            generation++; details = null; remember(null); box.hidden = true;
            document.getElementById('session-reopen').hidden = true;
            document.getElementById('session-reopen-message').textContent = '';
            summary.textContent = ''; explanation.textContent = ''; reason.value = ''; ack.checked = false;
            document.getElementById('session-start-timing').textContent = '';
            document.getElementById('session-start-camera').textContent = '';
            document.getElementById('session-roster-preview-list').replaceChildren();
            document.getElementById('session-roster-preview').hidden = true;
        }};
    document.getElementById('session-reopen')?.addEventListener('click', () => {
        if (controlsLocked() || !(lifecycleState?.resume_ended_available || lifecycleState?.reopen_available) || pending) return;
        action = lifecycleState.resume_ended_available ? 'resume_ended' : 'reopen'; mode = 'camera'; reason.value = ''; details = null;
        check();
        box.scrollIntoView({block:'center', behavior:'smooth'});
    });
    if (pending) {
        attendancePoller.mutationUncertain = true;
        reconcile();
    } else if (!lifecycleState?.collection_allowed && !lifecycleState?.end_operation) check();
    controls();
})();
