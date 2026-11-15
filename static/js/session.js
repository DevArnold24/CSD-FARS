const { classId, hasCamera, videoFeedUrl, startTimeStr, endTimeStr, lateThreshold, resumeGraceMins, availabilityMessage } = window.SESSION_CONFIG;
let { isSessionActive, actualStartedAtStr } = window.SESSION_CONFIG;
let { canStartSession, activeSessionId } = window.SESSION_CONFIG;
let { displayEnabled, displayUrl } = window.SESSION_CONFIG;
let runtimeState = window.SESSION_CONFIG.sessionState || 'inactive';
let runtimeMessage = window.SESSION_CONFIG.sessionMessage || '';
let occurrenceOpen = Boolean(window.SESSION_CONFIG.occurrenceOpen);
let lifecycleState = window.SESSION_CONFIG.lifecycle || null;
let pendingEnd = null;
let attendanceRows = null;

// Filters affect only visible rows, never the occurrence's saved counts or rules.
function refreshAttendanceRoster() {
    if (authExpired || !attendanceRows) return;
    const query = (document.getElementById('attendance-search')?.value || '').trim().toLocaleLowerCase();
    const selected = document.getElementById('attendance-filter')?.value || 'all';
    let visible = 0;
    const pendingLabel = lifecycleState?.collection_allowed || (!lifecycleState && isSessionActive)
        ? 'Not yet marked' : 'Pending · review needed';
    for (const [key, label] of [['pending', pendingLabel], ['present', 'Present / Late'], ['absent', 'Absent'], ['excused', 'Excused']]) {
        const students = attendanceRows[key] || [];
        const shown = students.filter(student => (selected === 'all' || selected === key) &&
            `${student.name} ${student.student_id}`.toLocaleLowerCase().includes(query));
        visible += shown.length;
        const group = document.getElementById(`attendance-group-${key}`);
        if (group) group.hidden = !shown.length;
        const heading = document.getElementById(`attendance-heading-${key}`);
        if (heading) heading.textContent = `${label} (${students.length})`;
        const option = document.getElementById(`attendance-filter-${key}`);
        if (option) option.textContent = `${label} (${students.length})`;
        renderAttendanceList(document.getElementById(`${key}-list`), shown, 'No matching students.', true);
    }
    const total = Object.values(attendanceRows).reduce((sum, rows) => sum + rows.length, 0);
    const recorded = total - attendanceRows.pending.length;
    const progress = document.getElementById('attendance-progress');
    const countUnconfirmed = !lifecycleState || lifecycleState.roster_consistent === false;
    if (progress) progress.textContent = countUnconfirmed
        ? `Showing ${recorded} recorded results. The complete student count cannot be confirmed.`
        : `Attendance recorded for ${recorded} of ${total} students`;
    const result = document.getElementById('attendance-filter-result');
    if (result) result.textContent = visible ? `Showing ${visible} of ${total} students. ${countUnconfirmed ? 'Counts describe the available results only.' : 'Counts above cover the whole session.'}` : 'No students match these filters.';
}

document.getElementById('attendance-search')?.addEventListener('input', refreshAttendanceRoster);
document.getElementById('attendance-filter')?.addEventListener('change', refreshAttendanceRoster);

function sessionCommand(extra = {}) {
    // Legacy Open occurrences must still finish through their original protocol
    // before the new policy can activate. They have no lifecycle version.
    if (!lifecycleState) return {};
    return {headers: {'Content-Type': 'application/json'}, body: JSON.stringify({session_id: activeSessionId, version: lifecycleState?.version, ...extra})};
}

function schoolTimeLabel(label) {
    if (typeof label !== 'string') return 'Not confirmed';
    const match = label.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/);
    if (!match) return label.replace(' (institutional time)', ' (school time)');
    const hour = Number(match[2]);
    return `${match[1]} · ${hour % 12 || 12}:${match[3]}${match[4] && match[4] !== '00' ? ':' + match[4] : ''} ${hour < 12 ? 'AM' : 'PM'} (school time)`;
}

function attendanceResultHeading(state) {
    if (state.roster_consistent === false) return 'The original student list needs review. Saved attendance is preserved.';
    if (state.clock_review_required) return 'Attendance is paused because the server time changed. Ask the server operator to check it.';
    if (state.exhausted) return 'Automatic recovery could not finish. Check the saved attendance and recovery details below.';
    if (state.closure_confirmed) return state.review_required ? `Attendance ended. ${state.counts?.Pending || 0} ${(state.counts?.Pending || 0) === 1 ? 'student needs' : 'students need'} review.` : 'Attendance ended. Review the saved results below.';
    if (state.collection === 'starting') return 'Preparing the camera. Attendance recording has not started yet.';
    if (state.collection === 'interrupted') return 'Attendance was interrupted. Saved results are preserved.';
    if (!state.collection_allowed) return 'Attendance has stopped. Session ending is not yet confirmed.';
    return 'Attendance is open until its scheduled end time.';
}

function attendanceResultDetail(state) {
    const camera = state.cleanup === 'pending'
        ? 'The camera stop is not confirmed. The camera remains reserved for this session.'
        : state.cleanup === 'confirmed' ? 'Camera stop confirmed.' : state.cleanup === 'not_applicable' ? 'No camera was used.' : 'Camera availability has not been confirmed.';
    const ending = state.closure_confirmed ? 'Session ending confirmed.' : 'Session ending is not confirmed.';
    return `${ending} ${camera} ${state.resume_ended_available ? 'You may review Resume until scheduled end below.' : state.resume_available ? 'You may resume before the original attendance end time.' : 'Resume is not currently available.'}`;
}

function applyLifecycle(state) {
    if (!state || authExpired) return;
    lifecycleState = state;
    const reopenButton = document.getElementById('session-reopen');
    if (reopenButton) { reopenButton.hidden = !(state.resume_ended_available || state.reopen_available); reopenButton.textContent = state.resume_ended_available ? 'Resume until scheduled end' : 'Review camera reopening'; reopenButton.disabled = controlsLocked(); }
    const reopenMessage = document.getElementById('session-reopen-message');
    if (reopenMessage) { const message = state.resume_ended_available ? state.resume_ended_message : state.reopen_message; reopenMessage.hidden = !message; reopenMessage.textContent = message; }
    const openingInfo = document.getElementById('session-accepted-opening');
    if (openingInfo) {
        openingInfo.hidden = !state?.startup;
        openingInfo.textContent = state?.startup ? `Attendance opens for this session: ${schoolTimeLabel(state.startup.opening_label || 'Not recorded')}. ${state.reopening ? 'Class and late times are unchanged; the reopening has a separate deadline.' : 'This session keeps its original times.'}` : '';
    }
    isSessionActive = state.collection_allowed;
    const reopen = state.reopening;
    if (reopen && state.collection_allowed) runtimeMessage = 'Reopened camera attendance. Only Pending students can receive new camera results.';
    occurrenceOpen = state.end_available;
    canStartSession = state.resume_available;
    runtimeMessage = state.message;
    const guidance = document.getElementById('manual-attendance-guidance');
    if (guidance) guidance.textContent = state.collection_allowed
        ? 'Mark students below. A first Present mark needs no explanation; other results and corrections require a reason.'
        : state.end_operation ? 'Attendance has ended. Open the saved attendance to review Pending students or make a correction.'
        : 'Review the attendance details and start the session before marking students.';
    const availability = document.getElementById('session-availability-message');
    if (availability) availability.textContent = state.end_operation
        ? 'Attendance recording has stopped. Check the confirmed ending and saved results above.'
        : state.message === 'Attendance collection is open until the scheduled cutoff.'
            ? `Attendance is open. Scheduled end: ${schoolTimeLabel(state.cutoff_label)}.` : state.message;
    const panel = document.getElementById('session-completion');
    if (!panel) return;
    panel.hidden = state.collection_allowed;
    document.getElementById('session-completion-title').textContent = attendanceResultHeading(state);
    const recorded = ['Present', 'Late', 'Absent', 'Excused'].reduce((sum, key) => sum + (state.counts?.[key] || 0), 0);
    const total = recorded + (state.counts?.Pending || 0);
    const countLabel = state.roster_consistent === false ? 'The complete student count cannot be confirmed.' : `Attendance recorded for ${recorded} of ${total} students.`;
    document.getElementById('session-completion-counts').textContent = countLabel + ' ' + Object.entries(state.counts || {}).map(([status, count]) => `${status}: ${count}`).join(' · ');
    document.getElementById('session-completion-detail').textContent = attendanceResultDetail(state);
    const context = document.getElementById('session-completion-context');
    if (context) context.textContent = `${state.message} Recovery attempts: ${state.attempts}/5. Last attempt: ${state.last_attempt_label || 'not confirmed'}. Attendance end time: ${state.cutoff_label || 'checking'}. Ending confirmed: ${state.closed_label || 'not confirmed'}. ${state.reason_label || ''}`;
    const link = document.getElementById('session-review-link');
    if (state.collection === 'starting') {
        document.getElementById('session-completion-detail').textContent = `The camera has up to 30 seconds to start, without extending attendance beyond ${schoolTimeLabel(state.cutoff_label)}. You may end this session. Marking attendance and the live display remain unavailable until recording starts.`;
    }
    link.hidden = state.collection === 'starting';
    link.href = `/classes/${classId}/sessions/${state.session_id}`;
    link.textContent = state.review_required ? 'Review Pending attendance' : 'View saved attendance';
    const retry = document.getElementById('session-retry-closure');
    retry.hidden = !state.retry_available;
    retry.disabled = controlsLocked();
    window.CsdSessionStart?.update(state);
    const clockReview = document.getElementById('session-clock-review');
    if (clockReview) clockReview.hidden = !state.clock_review_required;
}

const btnStart = document.getElementById('btn-start-session');
const btnEnd = document.getElementById('btn-end-session');
const videoImg = document.getElementById('video-feed');
const feedViewSelect = document.getElementById('session-feed-view');
let selectedFeedView = 'live';
let smoothPreviewFailed = false;
let viewingCapacity = null;
let captureUnstable = null;
const smoothPreviewFailureMessage = 'Smooth preview unavailable. Select Recognition boxes to try that view. The current attendance status is shown separately.';
if (feedViewSelect) feedViewSelect.value = selectedFeedView;
function selectedVideoFeedUrl() {
    return `${videoFeedUrl}${videoFeedUrl.includes('?') ? '&' : '?'}view=${selectedFeedView}`;
}
const videoPanel = document.getElementById('session-video-panel');
const videoPlaceholder = document.getElementById('video-placeholder');
const retryPreview = document.getElementById('session-retry-preview');
const captureNotice = document.getElementById('session-capture-notice');
const statusBadge = document.getElementById('session-status-badge');
const availabilityBadge = document.getElementById('session-availability-badge');
const countdownTimer = document.getElementById('countdown-timer');
const btnMaximizeFeed = document.getElementById('btn-maximize-feed');
const btnEnableDisplay = document.getElementById('btn-enable-display');
const btnCopyDisplay = document.getElementById('btn-copy-display');
const btnDeactivateDisplay = document.getElementById('btn-deactivate-display');
const linkOpenDisplay = document.getElementById('link-open-display');
const displayStatus = document.getElementById('attendance-display-status');
const displayMessage = document.getElementById('attendance-display-message');
const displayCopyFallback = document.getElementById('attendance-display-copy-fallback');
const displayCopyLink = document.getElementById('attendance-display-copy-link');
const recoveryBanner = document.getElementById('session-recovery-banner');
const connectionStatus = document.getElementById('attendance-connection-status');
const syncMessage = document.getElementById('attendance-sync-message');
const logSyncStatus = document.getElementById('attendance-log-sync-status');
const recheckButton = document.getElementById('attendance-recheck');
const blankFeedSrc = "data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=";
let automaticStopHandled = false;
let authExpired = false;
let attendancePoller = null;
let connectionHealthy = false;
let lastSuccessfulSync = null;
let feedNeedsReconnect = false;
let actionEpoch = 0;
let sessionReloadRequired = false;
// A page can load while capture cleanup is still changing the server grant.
// Discard its cached link once; explicit display responses can verify it later.
let displayStateUnverified = ['interrupted', 'stopping', 'unverified'].includes(runtimeState);
if (displayStateUnverified) displayUrl = null;
const videoFeedWatchdog = hasCamera && videoImg && window.CsdMjpegFeed
    ? new window.CsdMjpegFeed.MjpegFeedWatchdog(videoImg, {
        baseUrl: selectedVideoFeedUrl(),
        isActive: () => isSessionActive && !authExpired && connectionHealthy && !attendancePoller?.mutationUncertain
            && !(selectedFeedView === 'live' && smoothPreviewFailed),
        stallAfterMs: 15000,
        retryDelaysMs: [1000, 2000, 5000, 10000, 15000],
        onStateChange: state => {
            if (!videoPlaceholder || !isSessionActive || authExpired || !connectionHealthy) return;
            if (['connecting', 'retrying', 'reconnecting', 'paused', 'exhausted'].includes(state)) {
                const advice = viewingCapacity?.available === false ? `${viewingCapacity.message} ` : '';
                videoPlaceholder.textContent = advice + (state === 'exhausted'
                    ? 'The video could not reconnect after several attempts. Check your connection, then select Retry preview. This does not restart attendance.'
                    : state === 'paused' ? 'Video reconnection is paused while this tab or its connection is unavailable.'
                    : 'The video connection was lost. Trying to reconnect the view. Check the attendance status separately.');
                if (retryPreview) retryPreview.hidden = state !== 'exhausted';
                videoPlaceholder.style.display = 'block';
            } else if (state === 'connected') {
                if (retryPreview) retryPreview.hidden = true;
                videoPlaceholder.style.display = 'none';
            }
        }
    })
    : null;

retryPreview?.addEventListener('click', () => {
    if (controlsLocked() || !isSessionActive) return;
    videoFeedWatchdog?.start();
});

function showSmoothPreviewFailure() {
    videoFeedWatchdog?.stop();
    if (videoImg) videoImg.src = blankFeedSrc;
    if (videoPlaceholder) {
        videoPlaceholder.textContent = smoothPreviewFailureMessage;
        videoPlaceholder.style.display = 'block';
    }
}

feedViewSelect?.addEventListener('change', () => {
    const nextView = feedViewSelect.value === 'recognition' ? 'recognition' : 'live';
    if (nextView === selectedFeedView) return;
    selectedFeedView = nextView;
    videoFeedWatchdog?.stop();
    if (videoFeedWatchdog) videoFeedWatchdog.baseUrl = selectedVideoFeedUrl();
    if (!hasCamera || !videoImg || !isSessionActive || controlsLocked()) return;
    if (selectedFeedView === 'live' && smoothPreviewFailed) {
        showSmoothPreviewFailure();
        return;
    }
    if (videoFeedWatchdog) videoFeedWatchdog.start();
    else videoImg.src = selectedVideoFeedUrl();
});

function handleAccessFailure(status) {
    if (authExpired && status !== 403) return;
    authExpired = true;
    attendanceRows = null;
    actionEpoch++;
    connectionHealthy = false;
    displayEnabled = false;
    displayUrl = null;
    clearDisplayCopyFallback();
    videoFeedWatchdog?.stop();
    if (feedViewSelect) feedViewSelect.disabled = true;
    window.CsdSessionStart?.clear();
    if (status === 403) window.SessionAuth.handleAccessDenied(document);
    else window.SessionAuth.handleSessionExpired(document, {});
    if (connectionStatus) connectionStatus.textContent = status === 403 ? 'Access denied' : 'Login required';
    if (syncMessage) syncMessage.textContent = 'Private attendance content was cleared. This page no longer receives updates.';
    if (logSyncStatus) logSyncStatus.textContent = 'Access unavailable';
    if (recheckButton) recheckButton.hidden = true;
    if (recoveryBanner) recoveryBanner.hidden = true;
}

function handleAuthMetadata(payload) {
    if (!authExpired || attendancePoller?.authStatus !== 401) return;
    window.SessionAuth.handleSessionExpired(document, payload);
    if (connectionStatus) connectionStatus.textContent = 'Password reset required';
}

function controlsLocked() {
    return authExpired || sessionReloadRequired || !connectionHealthy || Boolean(attendancePoller?.mutating || attendancePoller?.mutationUncertain);
}

function clearDisplayCopyFallback() {
    if (displayCopyFallback) displayCopyFallback.hidden = true;
    if (displayCopyLink) displayCopyLink.value = '';
}

function updateConnectionControls() {
    if (authExpired) return;
    const locked = controlsLocked();
    if (locked || !displayUrl) clearDisplayCopyFallback();
    btnStart.disabled = locked || !canStartSession;
    btnEnd.disabled = locked || (!occurrenceOpen && !isSessionActive);
    btnEnableDisplay.disabled = locked || !activeSessionId || lifecycleState?.collection === 'starting';
    btnCopyDisplay.disabled = locked || !displayUrl;
    btnDeactivateDisplay.disabled = locked || !displayEnabled;
    if (btnMaximizeFeed) btnMaximizeFeed.disabled = locked || !isSessionActive;
    if (feedViewSelect) feedViewSelect.disabled = locked;
    if (retryPreview) retryPreview.disabled = locked || !isSessionActive;
    const submit = document.getElementById('btn-submit-override');
    if (submit) submit.disabled = locked || Boolean((window.SESSION_CONFIG.startProtocol && !lifecycleState?.collection_allowed) || (lifecycleState && !lifecycleState.collection_allowed));
    document.querySelectorAll?.('.btn-override').forEach(button => { button.disabled = locked || Boolean((window.SESSION_CONFIG.startProtocol && !lifecycleState?.collection_allowed) || (lifecycleState && !lifecycleState.collection_allowed)); });
    const retry = document.getElementById('session-retry-closure');
    if (retry) retry.disabled = locked;
    const reopenControl = document.getElementById('session-reopen');
    if (reopenControl) reopenControl.disabled = locked || !(lifecycleState?.resume_ended_available || lifecycleState?.reopen_available);
    if (linkOpenDisplay) {
        linkOpenDisplay.hidden = locked || !displayUrl;
        if (locked || !displayUrl) linkOpenDisplay.removeAttribute('href');
        else linkOpenDisplay.href = displayUrl;
    }
    if (displayStateUnverified && !sessionReloadRequired && !attendancePoller?.mutationUncertain) {
        // Polls describe capture state, not grants created in another tab.
        // Keep the uncertainty without implying that an inactive display had a link.
        displayStatus.textContent = displayEnabled ? 'Unverified' : 'Inactive (last known)';
        displayStatus.className = 'badge badge--warning';
        displayMessage.textContent = displayEnabled
            ? 'Camera attendance was interrupted. Reload to verify display status, deactivate the display, or resume attendance and create a replacement link.'
            : 'The display was inactive at the last check. Reload to verify its current status, or resume attendance and enable a display link.';
        btnCopyDisplay.hidden = true;
        btnEnableDisplay.textContent = displayEnabled ? 'Create Replacement Link' : 'Enable Display';
    }
    if (isSessionActive && !connectionHealthy) {
        statusBadge.textContent = 'Active (last known)';
        statusBadge.className = 'badge badge--warning';
        if (availabilityBadge) {
            availabilityBadge.textContent = 'Current session state unknown';
            availabilityBadge.className = 'badge badge--warning';
        }
    }
    if (attendancePoller?.mutationUncertain) {
        statusBadge.textContent = 'Action outcome unknown';
        statusBadge.className = 'badge badge--warning';
        if (availabilityBadge) {
            availabilityBadge.textContent = 'Reload to verify action outcome';
            availabilityBadge.className = 'badge badge--warning';
        }
    }
    if (sessionReloadRequired) {
        statusBadge.textContent = 'Reload required';
        statusBadge.className = 'badge badge--warning';
        if (availabilityBadge) {
            availabilityBadge.textContent = 'Attendance occurrence changed';
            availabilityBadge.className = 'badge badge--warning';
        }
    }
    window.CsdSessionStart?.controls();
}

function handleConnectionState(info) {
    if (authExpired) return;
    const wasHealthy = connectionHealthy;
    connectionHealthy = info.state === 'connected' && !sessionReloadRequired;
    lastSuccessfulSync = info.lastSuccess;
    if (connectionHealthy && !wasHealthy) feedNeedsReconnect = true;
    const labels = {connecting:'Connecting',connected:'Connected',disconnected:'Connection lost',reconnecting:'Reconnecting',paused:'Updates paused',updating:'Updating attendance'};
    if (connectionStatus) connectionStatus.textContent = sessionReloadRequired ? 'Reload required' : (labels[info.state] || 'Connection unknown');
    const lastSync = lastSuccessfulSync === null ? 'No attendance update received yet.'
        : `Last updated: ${new Date(lastSuccessfulSync).toLocaleTimeString()}.`;
    if (syncMessage) syncMessage.textContent = info.mutationUncertain
        ? `We could not confirm the last action. Reload to check attendance and display links before trying again. ${lastSync}`
        : sessionReloadRequired ? 'A different attendance session was opened in another tab or browser. Reload to view its current attendance, schedule and display controls.'
        : connectionHealthy ? `Attendance is up to date. ${lastSync}`
            : lastSuccessfulSync === null ? (info.state === 'connecting' ? 'Checking attendance. The current session status is not yet known.' : 'Attendance updates are unavailable. The current session status is unknown. Reconnect to check attendance.')
            : `The current attendance status is unknown. Results below are from the last successful update. ${lastSync}`;
    if (logSyncStatus) logSyncStatus.textContent = connectionHealthy && !info.mutationUncertain ? 'Up to date' : lastSuccessfulSync === null ? 'Waiting for attendance updates' : 'Last known attendance (out of date)';
    if (recheckButton) recheckButton.hidden = !info.mutationUncertain && !sessionReloadRequired;
    if (info.mutationUncertain || sessionReloadRequired) {
        displayUrl = null;
        displayEnabled = false;
        displayStatus.textContent = 'Unverified. Reload to check.';
        displayMessage.textContent = sessionReloadRequired
            ? 'The attendance session changed. Reload before opening or sharing a display link.'
            : 'Display link outcome is unknown. Reload or verify it before sharing another link.';
        btnCopyDisplay.hidden = true;
        btnDeactivateDisplay.hidden = true;
    }
    if (!connectionHealthy || info.mutationUncertain) {
        videoFeedWatchdog?.stop();
        if (videoImg) videoImg.src = blankFeedSrc;
        if (videoPlaceholder) {
            videoPlaceholder.style.display = 'block';
            videoPlaceholder.textContent = 'Live view paused until attendance status is verified.';
        }
    }
    updateUIState();
    if (connectionHealthy && !isSessionActive && !info.mutationUncertain && videoPlaceholder && runtimeState === 'inactive') {
        videoPlaceholder.textContent = canStartSession
            ? `The camera view is off. ${activeSessionId ? 'Check whether you can resume attendance.' : 'Choose an attendance method and check the details before starting.'}`
            : 'Camera feed is off. Attendance is not available at this time.';
    }
    if (connectionHealthy && feedNeedsReconnect && isSessionActive && !videoFeedWatchdog?.exhausted && !info.mutationUncertain
        && !(selectedFeedView === 'live' && smoothPreviewFailed)) {
        feedNeedsReconnect = false;
        videoFeedWatchdog?.start();
    }
    if (!connectionHealthy || info.mutationUncertain) {
        if (videoPlaceholder) videoPlaceholder.style.display = 'block';
    }
}

async function submitSessionAction(url, extra = {}, validate) {
    if (controlsLocked() || !attendancePoller) return null;
    actionEpoch++;
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
    const result = await attendancePoller.mutate(url, {
        method:'POST', ...extra, headers:{'X-CSRFToken':csrfToken, ...extra.headers},
    }, validate);
    if (!result || authExpired || !attendancePoller.isCurrent(result.generation)) return null;
    if (result.uncertain) {
        AppModal.alert('Action outcome unknown. Reload to verify attendance and display links before retrying. The action was not automatically retried.');
        return null;
    }
    return result;
}

function isCurrentAction(result) {
    return Boolean(result && !authExpired && attendancePoller?.isCurrent(result.generation));
}

function setDisplayState(enabled, url) {
    if (authExpired) return;
    if (displayEnabled !== Boolean(enabled) || displayUrl !== (url || null)) clearDisplayCopyFallback();
    displayStateUnverified = false;
    displayEnabled = Boolean(enabled);
    displayUrl = url || null;
    displayStatus.textContent = displayEnabled ? 'Enabled' : 'Inactive';
    displayStatus.className = displayEnabled ? 'badge badge--success' : 'badge badge--secondary';
    btnEnableDisplay.disabled = !activeSessionId || authExpired || lifecycleState?.collection === 'starting';
    btnEnableDisplay.textContent = displayEnabled ? 'Create Replacement Link' : 'Enable Display';
    linkOpenDisplay.hidden = !displayUrl;
    btnCopyDisplay.hidden = !displayUrl;
    btnDeactivateDisplay.hidden = !displayEnabled;
    if (displayUrl) linkOpenDisplay.href = displayUrl;
    else linkOpenDisplay.removeAttribute('href');
    if (!displayCopyFallback || displayCopyFallback.hidden) {
        displayMessage.textContent = displayEnabled && !displayUrl
            ? 'A display link is active, but it is not available in this browser session. Create a replacement link to open or copy it. The old link will stop working.'
            : 'Share a private display for this attendance session. Results remain after attendance ends, until you clear or replace the display. Video is shown only during live attendance.';
    }
    updateConnectionControls();
}

// --- API Interactions ---

btnStart.addEventListener('click', async () => {
    if (window.SESSION_CONFIG.startProtocol) return;
    if (controlsLocked()) return;
    if (!canStartSession) {
        AppModal.alert(availabilityMessage || "Live attendance is not available for this class yet.");
        return;
    }
    try {
        btnStart.disabled = true;
        const result = await submitSessionAction(`/api/classes/${classId}/session/start`, sessionCommand(), data => Number.isInteger(data.session_id) && data.session_id > 0);
        if (!isCurrentAction(result)) return;
        const {response:res, data} = result;
        if (res.ok) {
            isSessionActive = true;
            runtimeState = 'active';
            runtimeMessage = '';
            occurrenceOpen = true;
            activeSessionId = data.session_id || activeSessionId;
            actualStartedAtStr = data.actual_started_at || new Date().toISOString();
            automaticStopHandled = false;
            btnStart.textContent = 'Resume Attendance';
            updateUIState();
        } else {
            AppModal.alert(data.error || "Failed to start session.");
        }
    } catch (e) {
        console.error(e);
        if (!authExpired) AppModal.alert("Error starting session.");
    } finally {
        updateConnectionControls();
    }
});

async function endAttendanceSession() {
    if (controlsLocked()) return;
    try {
        btnEnd.disabled = true;
        pendingEnd = {session_id: activeSessionId, operation_id: window.crypto?.randomUUID?.() || `end-${Date.now()}-${Math.random().toString(36).slice(2)}`};
        const result = await submitSessionAction(`/api/classes/${classId}/session/stop`, sessionCommand(pendingEnd), data => typeof data.message === 'string' && (data.session_id === null || Number.isInteger(data.session_id)));
        if (!isCurrentAction(result)) return;
        const res = result.response;
        if (res.ok) {
            if (result.data.lifecycle) {
                pendingEnd = null;
                applyLifecycle(result.data.lifecycle);
                runtimeState = 'inactive';
                setDisplayState(displayEnabled, displayUrl);
                videoFeedWatchdog?.stop();
                if (videoImg) videoImg.src = blankFeedSrc;
                updateUIState();
                return;
            }
            isSessionActive = false;
            runtimeState = 'inactive';
            runtimeMessage = '';
            occurrenceOpen = false;
            // The next snapshot still identifies this closed occurrence.
            // Retain the confirmed ID so our own End is not mistaken for a
            // different tab opening an unfamiliar attendance occurrence.
            activeSessionId = result.data.session_id;
            setDisplayState(displayEnabled, displayUrl);
            videoFeedWatchdog?.stop();
            updateUIState();
            if (hasCamera && videoImg && videoPlaceholder) {
                videoImg.src = blankFeedSrc;
                videoPlaceholder.textContent = 'The camera view is off. Check the attendance details, then select Start camera attendance.';
            }
        } else {
            AppModal.alert(result.data.error || "Failed to end session.");
        }
    } catch (e) {
        console.error(e);
        if (!authExpired) AppModal.alert("Error ending session.");
    } finally {
        updateConnectionControls();
    }
}

btnEnd.addEventListener('click', () => {
    if (btnEnd.disabled || authExpired) return;
    AppModal.confirm(
        lifecycleState ? `End this attendance collection? If you end camera attendance early, you may review Resume until scheduled end with an explanation and confirmed camera availability. This does not add attendance time. Exceptional reopening is a separate, limited action. ${lifecycleState.counts?.Pending || 0} ${(lifecycleState.counts?.Pending || 0) === 1 ? 'student still needs' : 'students still need'} review. Their attendance will remain Pending until you review it. Attendance is scheduled to end at ${schoolTimeLabel(lifecycleState.cutoff_label)}.`
            : 'End this older attendance session and record absences? Under its original rules, it may still be resumed before its deadline.',
        endAttendanceSession,
        null,
        'End Attendance'
    );
});

function updateUIState() {
    updateEndNotice();
    if (authExpired) return;
    if (recoveryBanner) {
        recoveryBanner.hidden = !runtimeMessage || runtimeMessage === 'Attendance collection is open until the scheduled cutoff.' || Boolean((window.SESSION_CONFIG.startProtocol && !lifecycleState?.collection_allowed) || (lifecycleState && !lifecycleState.collection_allowed));
        recoveryBanner.textContent = runtimeMessage === 'Attendance collection is open until the scheduled cutoff.' ? 'Attendance is open until its scheduled end time.' : runtimeMessage;
    }
    if (isSessionActive) {
        btnStart.style.display = 'none';
        btnEnd.style.display = 'inline-block';
        if (hasCamera && videoPlaceholder) {
            // Attendance polling can succeed while this browser's image is
            // broken. Keep its recovery guidance until the feed recovers.
            const recovering = ['connecting', 'retrying', 'reconnecting', 'paused', 'exhausted']
                .includes(videoFeedWatchdog?.state);
            const previewUnavailable = selectedFeedView === 'live' && smoothPreviewFailed && connectionHealthy
                && !attendancePoller?.mutationUncertain;
            videoPlaceholder.style.display = recovering || previewUnavailable ? 'block' : 'none';
            if (previewUnavailable) videoPlaceholder.textContent = smoothPreviewFailureMessage;
            if (videoFeedWatchdog?.exhausted) {
                videoPlaceholder.style.display = 'block';
                videoPlaceholder.textContent = (viewingCapacity?.available === false ? viewingCapacity.message + ' ' : '') + 'The video could not reconnect. Check your connection, then select Retry preview. Check the attendance status separately.';
                if (retryPreview) retryPreview.hidden = false;
            }
        }
        statusBadge.textContent = 'Active';
        statusBadge.className = 'badge badge--success';
        if (availabilityBadge) {
            availabilityBadge.textContent = 'Session running';
            availabilityBadge.className = 'badge badge--success';
        }
        btnEnableDisplay.disabled = authExpired;
    } else {
        btnStart.style.display = lifecycleState?.end_operation ? 'none' : 'inline-block';
        btnStart.textContent = activeSessionId && canStartSession ? 'Resume Attendance' : 'Start Session';
        btnStart.disabled = !canStartSession;
        btnEnd.style.display = 'none';
        if (hasCamera && videoPlaceholder) {
            videoPlaceholder.style.display = 'block';
        }
        statusBadge.textContent = 'Inactive';
        countdownTimer.textContent = '';
        if (lifecycleState && lifecycleState.collection !== 'interrupted') {
            statusBadge.textContent = !lifecycleState.closure_confirmed ? 'Ending not confirmed'
                : lifecycleState.cleanup === 'pending' ? 'Camera stop not confirmed'
                : lifecycleState.review_required ? 'Ended · review needed' : 'Ended';
        }
        statusBadge.className = 'badge badge--secondary';
        if (availabilityBadge) {
            availabilityBadge.textContent = lifecycleState?.end_operation ? 'Collection ended' : activeSessionId && canStartSession
                ? 'Resume available'
                : (canStartSession ? 'Review details to start' : 'Start unavailable');
            availabilityBadge.className = canStartSession ? 'badge badge--success' : 'badge badge--warning';
        }
        btnEnableDisplay.disabled = true;
    }
    if (lifecycleState?.collection === 'starting') {
        runtimeState = 'starting';
        statusBadge.textContent = 'Starting camera attendance';
        availabilityBadge.textContent = 'Recording not yet enabled';
        btnStart.style.display = 'none';
        btnEnd.style.display = lifecycleState.end_available ? 'inline-block' : 'none';
    }
    if (['interrupted', 'stopping', 'unverified'].includes(runtimeState)) {
        statusBadge.textContent = runtimeState === 'interrupted' ? 'Interrupted'
            : runtimeState === 'stopping' ? 'Stop not confirmed' : 'Status unavailable';
        statusBadge.className = 'badge badge--warning';
        if (availabilityBadge && runtimeState !== 'interrupted') {
            availabilityBadge.textContent = 'Camera stop not confirmed';
            availabilityBadge.className = 'badge badge--warning';
        }
        btnStart.disabled = authExpired || !canStartSession;
    }
    // Interrupted capture is not an ended occurrence. Keep the deliberate
    // End action available while retaining its attendance and display grant.
    if (occurrenceOpen && !authExpired) btnEnd.style.display = 'inline-block';
    updateConnectionControls();
}

function handleInterruptedSession(data) {
    if (!['interrupted', 'stopping', 'unverified'].includes(runtimeState)) {
        // Orderly interruption can revoke a grant while abrupt exit can retain
        // it. Telemetry alone cannot verify the cached link in either case.
        displayStateUnverified = true;
        displayUrl = null;
        actionEpoch++;
    }
    isSessionActive = false;
    automaticStopHandled = false;
    runtimeState = data.session_state;
    runtimeMessage = data.session_message || 'Camera attendance is interrupted. Existing attendance is saved.';
    occurrenceOpen = Boolean(data.occurrence_open);
    activeSessionId = data.session_id || activeSessionId;
    canStartSession = Boolean(data.resume_available);
    videoFeedWatchdog?.stop();
    updateUIState();
    if (videoImg) videoImg.src = blankFeedSrc;
    if (videoPlaceholder) videoPlaceholder.textContent = runtimeMessage;
}

function handleAutomaticSessionStop(data) {
    if (automaticStopHandled) return;
    automaticStopHandled = true;
    isSessionActive = false;
    runtimeState = 'inactive';
    runtimeMessage = data.session_message || 'Attendance collection stopped. Review the saved session status.';
    occurrenceOpen = Boolean(data.occurrence_open);
    activeSessionId = data.session_id || null;
    if (data.session_id !== null && typeof data.resume_available === 'boolean') canStartSession = data.resume_available;
    if (data.session_id === null && typeof data.can_start_new === 'boolean') canStartSession = data.can_start_new;
    setDisplayState(displayEnabled, displayUrl);
    videoFeedWatchdog?.stop();
    if (data.session_end_reason === 'resume_window_elapsed') {
        canStartSession = false;
        activeSessionId = null;
    }
    updateUIState();
    if (hasCamera && videoImg && videoPlaceholder) {
        videoImg.src = blankFeedSrc;
        videoPlaceholder.textContent = data.session_message || "Live session stopped unexpectedly. Check the assigned camera and try again.";
        videoPlaceholder.style.display = 'block';
    }
}

btnEnableDisplay.addEventListener('click', async () => {
    if (!activeSessionId || controlsLocked()) return;
    btnEnableDisplay.disabled = true;
    try {
        const result = await submitSessionAction(`/api/classes/${classId}/session/display/enable`, {headers: {'Content-Type': 'application/json'}, body: JSON.stringify({session_id: activeSessionId})},
            data => data.display_enabled === true && typeof data.display_url === 'string'
                && data.display_url.startsWith('/') && !data.display_url.startsWith('//'));
        if (!isCurrentAction(result)) return;
        const {response, data:payload} = result;
        if (!response.ok) throw new Error(payload.error || 'Unable to enable Attendance Display.');
        setDisplayState(true, payload.display_url);
        displayMessage.textContent = 'Display link ready. Open it on the room screen or copy it to that device.';
    } catch (error) {
        if (!authExpired) AppModal.alert(error.message || 'Unable to enable Attendance Display.');
    } finally {
        updateConnectionControls();
    }
});

btnCopyDisplay.addEventListener('click', async () => {
    if (!displayUrl || controlsLocked()) return;
    const epoch = actionEpoch;
    const generation = attendancePoller?.generation;
    const link = displayUrl;
    const absoluteUrl = new URL(displayUrl, window.location.origin).href;
    try {
        await navigator.clipboard.writeText(absoluteUrl);
        if (controlsLocked() || epoch !== actionEpoch || link !== displayUrl || !attendancePoller?.isCurrent(generation)) return;
        clearDisplayCopyFallback();
        displayMessage.textContent = 'Display link copied.';
    } catch (error) {
        if (controlsLocked() || epoch !== actionEpoch || link !== displayUrl || !attendancePoller?.isCurrent(generation)) return;
        displayMessage.textContent = 'Automatic copying is unavailable. Select and copy the private display link below.';
        if (displayCopyFallback && displayCopyLink) {
            displayCopyLink.value = absoluteUrl;
            displayCopyFallback.hidden = false;
            displayCopyLink.focus();
            displayCopyLink.select();
        }
    }
});

async function deactivateAttendanceDisplay() {
    if (controlsLocked()) return;
    btnDeactivateDisplay.disabled = true;
    try {
        const result = await submitSessionAction(`/api/classes/${classId}/session/display/deactivate`, {headers: {'Content-Type': 'application/json'}, body: JSON.stringify({session_id: activeSessionId})}, data => data.display_enabled === false);
        if (!isCurrentAction(result)) return;
        const {response, data:payload} = result;
        if (!response.ok) throw new Error(payload.error || 'Unable to deactivate Attendance Display.');
        setDisplayState(false, null);
        displayMessage.textContent = 'Attendance Display deactivated. Its previous link no longer works.';
    } catch (error) {
        if (!authExpired) AppModal.alert(error.message || 'Unable to deactivate Attendance Display.');
    } finally {
        updateConnectionControls();
    }
}

btnDeactivateDisplay.addEventListener('click', () => {
    if (btnDeactivateDisplay.disabled || authExpired) return;
    AppModal.confirm(
        'Clear this Attendance Display? Its link will stop working. Saved attendance records will remain unchanged.',
        deactivateAttendanceDisplay,
        null,
        'Clear Attendance Display'
    );
});

// --- Feed Maximize Controls ---

function isFeedMaximized() {
    return document.fullscreenElement === videoPanel || videoPanel.classList.contains('session-video-card--expanded');
}

function syncMaximizeButton() {
    const maximized = isFeedMaximized();
    btnMaximizeFeed.textContent = maximized ? 'Exit Maximize' : 'Maximize Feed';
    btnMaximizeFeed.setAttribute('aria-pressed', maximized ? 'true' : 'false');
}

function enterFallbackMaximize() {
    videoPanel.classList.add('session-video-card--expanded');
    document.body.classList.add('feed-expanded');
    syncMaximizeButton();
}

async function exitFeedMaximize() {
    if (document.fullscreenElement) {
        try {
            await document.exitFullscreen();
        } catch (err) {
            console.warn('Unable to exit fullscreen mode.', err);
        }
    }
    videoPanel.classList.remove('session-video-card--expanded');
    document.body.classList.remove('feed-expanded');
    syncMaximizeButton();
}

async function enterFeedMaximize() {
    if (videoPanel.requestFullscreen) {
        try {
            await videoPanel.requestFullscreen();
            syncMaximizeButton();
            return;
        } catch (err) {
            console.warn('Fullscreen request was rejected. Using expanded view.', err);
        }
    }
    enterFallbackMaximize();
}

if (hasCamera && btnMaximizeFeed) {
    btnMaximizeFeed.addEventListener('click', async () => {
        if (isFeedMaximized()) {
            await exitFeedMaximize();
        } else {
            await enterFeedMaximize();
        }
    });

    document.addEventListener('fullscreenchange', () => {
        if (!document.fullscreenElement && videoPanel) {
            videoPanel.classList.remove('session-video-card--expanded');
            document.body.classList.remove('feed-expanded');
        }
        syncMaximizeButton();
    });

    document.addEventListener('keydown', async (event) => {
        if (event.key === 'Escape' && videoPanel && videoPanel.classList.contains('session-video-card--expanded')) {
            await exitFeedMaximize();
        }
    });

    syncMaximizeButton();
}

// --- AJAX Polling for Attendance Status ---

function appendAttendanceText(parent, tagName, value, styleText) {
    const element = document.createElement(tagName);
    element.style.cssText = styleText;
    element.textContent = value == null ? '' : String(value);
    parent.appendChild(element);
    return element;
}

function createOverrideButton(student) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn-override';
    button.dataset.sid = student.student_id == null ? '' : String(student.student_id);
    button.dataset.name = student.name == null ? '' : String(student.name);
    button.dataset.pending = String(student.status === 'Pending');
    button.dataset.status = student.status || 'Pending';
    button.setAttribute('aria-label', `Mark attendance for ${student.name}, ID ${student.student_id}`);
    button.textContent = 'Mark attendance';
    button.disabled = controlsLocked() || Boolean((window.SESSION_CONFIG.startProtocol && !lifecycleState?.collection_allowed) || (lifecycleState && !lifecycleState.collection_allowed));
    return button;
}

function createAttendanceRow(student, showStatus) {
    const item = document.createElement('li');
    item.style.cssText = 'display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid #f1f5f9;';

    const identity = document.createElement('div');
    appendAttendanceText(identity, 'div', student.name, 'font-weight: 500; color: #1e293b;');
    appendAttendanceText(identity, 'div', student.student_id, 'font-size: 12px; color: #64748b;');
    item.appendChild(identity);

    const actions = document.createElement('div');
    if (showStatus) {
        actions.style.cssText = 'text-align: right; display: flex; flex-direction: column; align-items: flex-end; gap: 4px;';
        const statusLine = document.createElement('div');
        const statusColor = student.status === 'Present'
            ? '#15803d'
            : (student.status === 'Excused' ? '#1d4ed8' : '#b45309');
        const statusLabel = student.status === 'Pending' && (lifecycleState?.collection_allowed || (!lifecycleState && isSessionActive)) ? 'Not yet marked' : student.status;
        appendAttendanceText(statusLine, 'span', statusLabel, `font-size: 12px; font-weight: 600; color: ${statusColor}; margin-right: 8px;`);
        appendAttendanceText(statusLine, 'span', student.time, 'font-size: 12px; color: #64748b;');
        actions.appendChild(statusLine);
        if (student.reason) {
            appendAttendanceText(actions, 'div', student.reason, 'font-size: 11px; color: #64748b;');
        }
    }
    actions.appendChild(createOverrideButton(student));
    item.appendChild(actions);
    return item;
}

function renderAttendanceList(list, students, emptyMessage, showStatus) {
    if (!list) return;
    list.replaceChildren();
    if (students.length === 0) {
        const emptyItem = document.createElement('li');
        emptyItem.style.cssText = 'color: #999; font-size: 14px; padding: 8px 0;';
        emptyItem.textContent = emptyMessage;
        list.appendChild(emptyItem);
        return;
    }
    students.forEach(student => list.appendChild(createAttendanceRow(student, showStatus)));
}

function applyAttendanceSnapshot(data) {
    if (authExpired) return;
    if (window.CsdSessionStart?.pending()) { window.CsdSessionStart.observe(data); return; }
    if (sessionReloadRequired) return;
    if (attendancePoller?.mutationUncertain) {
        if (pendingEnd && data.session_id === pendingEnd.session_id && data.lifecycle?.end_operation) {
            attendancePoller.mutationUncertain = false;
            pendingEnd = null;
        } else return;
    }
    const sameOccurrence = data.session_id !== null && activeSessionId !== null
        && String(data.session_id) === String(activeSessionId);
    if ((data.session_id !== null && !sameOccurrence)
        || (data.is_active && (!data.occurrence_open || data.session_id === null))) {
        // A status snapshot does not carry this new occurrence's frozen page
        // schedule or display grant. Keep polling for authorization changes,
        // but require a fresh page before taking actions against it.
        sessionReloadRequired = true;
        actionEpoch++;
        connectionHealthy = false;
        feedNeedsReconnect = false;
        setDisplayState(false, null);
        videoFeedWatchdog?.stop();
        if (videoImg) videoImg.src = blankFeedSrc;
        countdownTimer.textContent = '';
        return;
    }
    if (sameOccurrence && typeof data.display_enabled === 'boolean') setDisplayState(data.display_enabled, data.display_url);
    // Camera-worker state is authoritative only in a complete, current
    // snapshot; transport failures never enter this state transition.
    viewingCapacity = data.viewing_capacity || null;
    captureUnstable = typeof data.capture_unstable === 'boolean' ? data.capture_unstable : null;
    if (captureNotice) {
        captureNotice.hidden = !isSessionActive || !hasCamera;
        captureNotice.textContent = captureUnstable === true
            ? 'Camera capture has been unstable. Confirmed marks are preserved. Stability requires continuous fresh frames; check missing attendance with the teacher.'
            : captureUnstable === null ? 'Camera stability information is unavailable. Video appearance alone does not confirm recognition or saving.' : '';
        if (captureUnstable === false) captureNotice.hidden = true;
    }
    const previewWasFailed = smoothPreviewFailed;
    if (data.preview_state === 'failed') smoothPreviewFailed = true;
    else if (data.preview_state === 'running') smoothPreviewFailed = false;
    if (previewWasFailed && !smoothPreviewFailed && selectedFeedView === 'live') feedNeedsReconnect = true;
    if (['interrupted', 'stopping', 'unverified'].includes(data.session_state)) {
        handleInterruptedSession(data);
    } else if (data.is_active && data.occurrence_open && sameOccurrence && !isSessionActive) {
        // Another tab can resume this exact occurrence, or a worker can
        // become observable again. Reconnect only this viewer; never start
        // a worker or adopt a different occurrence here.
        isSessionActive = true;
        automaticStopHandled = false;
        activeSessionId = data.session_id;
        runtimeState = data.session_state;
        runtimeMessage = '';
        occurrenceOpen = Boolean(data.occurrence_open);
        updateUIState();
        feedNeedsReconnect = true;
    } else if ((isSessionActive || ['interrupted', 'stopping', 'unverified'].includes(runtimeState)) && !data.is_active) {
        handleAutomaticSessionStop(data);
    } else if (isSessionActive && data.is_active) {
        const sequence = selectedFeedView === 'live' ? data.preview_publication_sequence : data.publication_sequence;
        const frameAge = selectedFeedView === 'live' ? data.preview_current_frame_age_ms : data.current_frame_age_ms;
        if (Number.isInteger(sequence) && sequence >= 0 && Number.isFinite(frameAge) && frameAge >= 0) {
            videoFeedWatchdog?.observeProgress(sequence, frameAge);
        }
    }
    if (isSessionActive && data.is_active && selectedFeedView === 'live' && smoothPreviewFailed) {
        showSmoothPreviewFailure();
    }
    if (data.session_id !== null && typeof data.resume_available === 'boolean') canStartSession = data.resume_available;
    if (data.session_id === null && typeof data.can_start_new === 'boolean') canStartSession = data.can_start_new;
    if (data.session_message) runtimeMessage = data.session_message;
    if (data.lifecycle) applyLifecycle(data.lifecycle);
    updateUIState();
    const preview = document.getElementById('session-roster-preview');
    if (preview) {
        preview.hidden = data.session_id !== null;
        const list = document.getElementById('session-roster-preview-list');
        list?.replaceChildren();
        for (const student of data.roster_preview || []) {
            const item = document.createElement('li'); item.textContent = `${student.name} (${student.student_id})`; list?.appendChild(item);
        }
    }
    attendanceRows = {present: data.present || [], absent: data.absent || [], pending: data.pending || [], excused: data.excused || []};
    refreshAttendanceRoster();
}

function pollAttendance() { return attendancePoller?.poll(); }

updateUIState();
applyLifecycle(lifecycleState);

attendancePoller = new window.AttendancePolling.AttendancePoller({
    url: `/api/classes/${classId}/attendance_status`,
    onData: applyAttendanceSnapshot,
    onState: handleConnectionState,
    onAuth: handleAccessFailure,
    onAuthMetadata: handleAuthMetadata,
});
attendancePoller.start();
recheckButton?.addEventListener('click', () => { if (!authExpired) window.location.reload(); });
document.getElementById('session-retry-closure')?.addEventListener('click', () => {
    if (controlsLocked() || !lifecycleState?.retry_available) return;
    const clockReview = lifecycleState.clock_review_required;
    const extra = clockReview ? {clock_review_confirmed: document.getElementById('session-clock-reviewed').checked,
        clock_review_reason: document.getElementById('session-clock-reason').value.trim()} : {};
    if (clockReview && (!extra.clock_review_confirmed || !extra.clock_review_reason)) {
        AppModal.alert('Ask the server operator to check and correct the clock first. Confirm that review and enter a technical review note before Retry.');
        return;
    }
    const explanation = clockReview
        ? 'Record the operator clock review and retry recovery? This does not restart collection. The original cutoff remains unchanged; Resume requires confirmed cleanup and time remaining.'
        : 'Retry closure recovery? This records your request and starts at most five cleanup/save attempts. Collection remains stopped.';
    AppModal.confirm(explanation, async () => {
        const result = await submitSessionAction(`/api/classes/${classId}/session/retry`, sessionCommand(extra), data => Boolean(data.lifecycle));
        if (!isCurrentAction(result)) return;
        if (result.response.ok) { applyLifecycle(result.data.lifecycle); updateUIState(); }
        else AppModal.alert(result.data.error || 'Retry could not be confirmed. Refresh session status.');
    }, null, 'Retry closure');
});

// --- Countdown Timer ---
function updateEndNotice() {
    const notice = document.getElementById('session-end-notice');
    if (!notice) return;
    const state = lifecycleState;
    const remaining = state && Number.isFinite(state.cutoff) ? state.cutoff * 1000 - Date.now() : NaN;
    const visible = !authExpired && !sessionReloadRequired && state?.collection_allowed
        && !state.closure_confirmed && !state.end_operation && !state.clock_review_required
        && Number.isFinite(remaining) && remaining <= 300000;
    notice.hidden = !visible;
    if (!visible) return;
    const uncertain = !connectionHealthy || attendancePoller?.mutating || attendancePoller?.mutationUncertain;
    const title = uncertain ? 'Checking attendance status'
        : remaining <= 0 ? 'Attendance time has ended'
        : remaining < 60000 ? 'Less than a minute remaining'
        : `Attendance ends in ${Math.ceil(remaining / 60000)} ${remaining <= 60000 ? 'minute' : 'minutes'}`;
    const detail = uncertain
        ? `Last confirmed end time: ${schoolTimeLabel(state.cutoff_label)}. Check the connection message below.`
        : remaining <= 0 ? 'Checking session ending and saved attendance. Confirmation will appear below.'
        : `New attendance will stop at ${schoolTimeLabel(state.cutoff_label)}.`;
    // Avoid repeated screen-reader announcements from the one-second timer.
    const heading = document.getElementById('session-end-notice-title');
    const description = document.getElementById('session-end-notice-detail');
    if (heading.textContent !== title) heading.textContent = title;
    if (description.textContent !== detail) description.textContent = detail;
}

function updateCountdown() {
    updateEndNotice();
    if (authExpired) return;
    if (!isSessionActive || sessionReloadRequired) {
        countdownTimer.textContent = "";
        return;
    }
    const now = new Date();

    // Live Elapsed Duration
    let elapsedText = "";
    if (actualStartedAtStr) {
        let startDate;
        if (actualStartedAtStr.includes('T')) {
            startDate = new Date(actualStartedAtStr);
        } else {
            startDate = new Date(actualStartedAtStr.replace(' ', 'T'));
        }
        if (!isNaN(startDate.getTime())) {
            const elapsedMs = Math.max(0, now - startDate);
            const hrs = Math.floor(elapsedMs / 3600000);
            const mins = Math.floor((elapsedMs % 3600000) / 60000);
            const secs = Math.floor((elapsedMs % 60000) / 1000);
            if (hrs > 0) {
                elapsedText = `Elapsed: ${hrs}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
            } else {
                elapsedText = `Elapsed: ${mins}:${secs.toString().padStart(2, '0')}`;
            }
        }
    }

    const [hours, minutes] = startTimeStr.split(':').map(Number);

    // Late Cutoff Timer
    const frozenStart = window.SESSION_CONFIG.scheduledStartStr;
    const lateCutoff = frozenStart ? new Date(frozenStart) : new Date();
    if (!frozenStart) lateCutoff.setHours(hours, minutes, 0, 0);
    lateCutoff.setTime(lateCutoff.getTime() + lateThreshold * 60000);
    if (lifecycleState) lateCutoff.setTime(lifecycleState.late_at * 1000);

    // v2 freezes collection cutoff; only legacy occurrences retain resume grace.
    const [endHours, endMinutes, endSeconds = 0] = endTimeStr.split(':').map(Number);
    const frozenDeadline = window.SESSION_CONFIG.resumeDeadlineStr;
    const sessionEnd = frozenDeadline ? new Date(frozenDeadline) : new Date();
    if (lifecycleState) sessionEnd.setTime(lifecycleState.cutoff * 1000);
    if (!frozenDeadline && !lifecycleState) {
        sessionEnd.setHours(endHours, endMinutes, endSeconds, 0);
        sessionEnd.setMinutes(sessionEnd.getMinutes() + resumeGraceMins);
    }

    const diffLateMs = lateCutoff - now;
    const diffEndMs = sessionEnd - now;

    let timerText = elapsedText ? `${elapsedText} | ` : "";

    if (diffLateMs > 0) {
        const mins = Math.floor(diffLateMs / 60000);
        const secs = Math.floor((diffLateMs % 60000) / 1000);
        timerText += `Late in: ${mins}:${secs.toString().padStart(2, '0')} | `;
        countdownTimer.style.color = "var(--warning-color)";
    } else {
        timerText += "Late time reached | ";
        countdownTimer.style.color = "var(--color-error)";
    }

    if (diffEndMs > 0) {
        const mins = Math.floor(diffEndMs / 60000);
        const secs = Math.floor((diffEndMs % 60000) / 1000);
        timerText += `Attendance ends in: ${mins}:${secs.toString().padStart(2, '0')}`;
    } else {
        timerText += "Attendance time ended";
    }

    countdownTimer.textContent = timerText;
}

setInterval(updateCountdown, 1000);
updateCountdown();

// --- Modal Interactions ---
const overrideModal = document.getElementById('override-modal');
const overrideForm = document.getElementById('override-form');
const btnCancelOverride = document.getElementById('btn-cancel-override');
const overrideStudentName = document.getElementById('override-student-name');
const overrideStudentId = document.getElementById('override-student-id');
const overrideReason = document.getElementById('override-reason');
const overrideStatus = document.getElementById('override-status');
const btnSubmitOverride = document.getElementById('btn-submit-override');
let overrideReturnFocus = null;
let overrideInitialPending = false;
function updateOverrideReason() {
    const optional = overrideInitialPending && overrideStatus.value === 'Present' &&
        lifecycleState?.collection_allowed && lifecycleState?.startup?.mode === 'manual';
    overrideReason.required = !optional;
    const label = document.querySelector('label[for="override-reason"]');
    if (label) label.textContent = optional ? 'Note (optional)' : 'Reason (required)';
    overrideReason.placeholder = optional ? 'Your name and the recording time are saved automatically.' : 'Explain why you are recording this attendance status.';
}
overrideStatus.addEventListener('change', updateOverrideReason);

function closeOverrideModal() {
    overrideModal.style.display = 'none';
    overrideModal.setAttribute('aria-hidden', 'true');
    if (overrideReturnFocus?.isConnected) overrideReturnFocus.focus();
    overrideReturnFocus = null;
}

function openOverrideModal(button) {
    if (controlsLocked() || (window.SESSION_CONFIG.startProtocol && !lifecycleState?.collection_allowed)) return;
    overrideReturnFocus = button;
    overrideStudentId.value = button.dataset.sid;
    overrideStudentName.textContent = button.dataset.name;
    const identity = document.getElementById('override-student-context');
    if (identity) identity.textContent = `Student ID: ${button.dataset.sid} · Current result: ${button.dataset.status === 'Pending' ? 'Not yet marked (Pending)' : button.dataset.status}`;
    overrideReason.value = '';
    overrideStatus.value = 'Present';
    overrideInitialPending = button.dataset.pending === 'true';
    updateOverrideReason();
    overrideModal.style.display = 'flex';
    overrideModal.setAttribute('aria-hidden', 'false');
    overrideStatus.focus?.();
}

// Event delegation for override buttons
document.addEventListener('click', (e) => {
    const button = typeof e.target.closest === 'function'
        ? e.target.closest('.btn-override')
        : (e.target.classList.contains('btn-override') ? e.target : null);
    if (button) openOverrideModal(button);
});

btnCancelOverride.addEventListener('click', closeOverrideModal);
overrideModal.addEventListener('click', event => {
    if (event.target === overrideModal) closeOverrideModal();
});
overrideModal.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
        event.preventDefault();
        closeOverrideModal();
        return;
    }
    if (event.key !== 'Tab') return;
    const controls = Array.from(overrideModal.querySelectorAll('select, textarea, button:not([disabled])'));
    if (!controls.length) return;
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
    }
});

overrideForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (controlsLocked() || (window.SESSION_CONFIG.startProtocol && !lifecycleState?.collection_allowed)) return;

    btnSubmitOverride.disabled = true;
    btnSubmitOverride.innerText = 'Submitting...';

    try {
        const result = await submitSessionAction(`/api/classes/${classId}/override`, {
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                session_id: activeSessionId,
                version: lifecycleState?.version,
                student_id: overrideStudentId.value,
                new_status: overrideStatus.value,
                reason: overrideReason.value
            })
        }, data => typeof data.message === 'string');
        if (!isCurrentAction(result)) return;
        const {response:res, data} = result;
        if (res.ok) {
            closeOverrideModal();
            // Wait for the next poll to update the UI naturally
        } else {
            AppModal.alert(data.error || "Failed to submit override.");
        }
    } catch (err) {
        console.error(err);
        if (!authExpired) AppModal.alert("Error submitting override.");
    } finally {
        if (!authExpired) btnSubmitOverride.innerText = 'Save attendance';
        updateConnectionControls();
    }
});
