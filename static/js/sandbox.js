/**
 * CSD FARS -- Recognition Sandbox Dashboard Client Logic
 */
document.addEventListener('DOMContentLoaded', () => {
    // DOM Elements
    const btnStart = document.getElementById('btn-start-sandbox');
    const btnStop = document.getElementById('btn-stop-sandbox');
    const cameraSelect = document.getElementById('camera-select');
    const feedImg = document.getElementById('sandbox-feed-img');
    const feedViewSelect = document.getElementById('sandbox-feed-view');
    let selectedFeedView = 'live';
    let smoothPreviewFailed = false;
    if (feedViewSelect) feedViewSelect.value = selectedFeedView;
    const selectedFeedUrl = () => `/api/diagnostics/sandbox/feed?view=${selectedFeedView}`;
    const feedPlaceholder = document.getElementById('feed-placeholder');
    const statusBadge = document.getElementById('sandbox-status-badge');
    const toastContainer = document.getElementById('sandbox-toast-container');
    const cameraSelectionStatus = document.getElementById('tuner-camera-selection-status');
    let sandboxRunning = Boolean(statusBadge?.classList.contains('badge--success'));
    let runningCameraId = cameraSelect?.getAttribute('data-running-camera-id') || '';
    let startPending = false;
    let cameraStateUncertain = false;

    // Tuning Control Elements
    const sliderPasses = document.getElementById('slider-confirmation-passes');
    const sliderThreshold = document.getElementById('slider-match-threshold');
    const useNormalMatchLimit = document.getElementById('use-normal-match-limit');
    const sliderMargin = document.getElementById('match_margin');
    const sliderDownscale = document.getElementById('downscale_height');
    const sliderSkip = document.getElementById('slider-skip-interval');
    const requireLiveness = document.getElementById('require-liveness');
    const requireLivenessStatus = document.getElementById('require-liveness-status');

    const valPasses = document.getElementById('val-confirmation-passes');
    const valThreshold = document.getElementById('val-match-threshold');
    const valMargin = document.getElementById('match_margin_val');
    const valDownscale = document.getElementById('downscale_height_val');
    const valSkip = document.getElementById('val-skip-interval');
    const matchLimitsStatus = document.getElementById('tuner-match-limits');
    const cameraLimitsMessage = document.getElementById('tuner-camera-limits-message');
    let cameraLimitsCurrent = true;
    let normalMatchLimits = new Map(Array.from(cameraSelect?.options || [], option => {
        let limit = null;
        try { limit = JSON.parse(option.getAttribute('data-normal-match-threshold')); }
        catch { /* A missing or invalid normal limit must never become a test value. */ }
        return [String(option.value), validNormalMatchLimit(limit)];
    }));

    function validNormalMatchLimit(value) {
        return typeof value === 'number' && Number.isFinite(value) ? value : null;
    }

    function renderMatchLimits() {
        if (!matchLimitsStatus) return;
        const cameraId = String(sandboxRunning ? runningCameraId : cameraSelect?.value || '');
        const testLimit = parseFloat(sliderThreshold?.value);
        const testText = `Test slider: ${Number.isFinite(testLimit) ? testLimit.toFixed(2) : 'unavailable'}. ${useNormalMatchLimit?.checked ? 'Normal matching selected; this slider is not applied.' : 'Manual override selected.'}`;
        if (!cameraLimitsCurrent) {
            matchLimitsStatus.textContent = `Normal attendance limits could not be refreshed. Reload this page to check them. ${testText}`;
        } else if (!cameraId || !normalMatchLimits.has(cameraId)) {
            matchLimitsStatus.textContent = `Normal attendance limit unavailable for this selection. Choose a saved camera or reload the page. ${testText}`;
        } else if (normalMatchLimits.get(cameraId) === null) {
            matchLimitsStatus.textContent = `Normal attendance cannot start with this camera's invalid saved match limit. Correct its camera or system settings. ${testText}`;
        } else {
            const normalLimit = normalMatchLimits.get(cameraId);
            const normalText = Number(normalLimit.toFixed(2)) === normalLimit ? normalLimit.toFixed(2) : String(normalLimit);
            matchLimitsStatus.textContent = `Selected camera's normal attendance limit: ${normalText}. ${testText} Lower is stricter.`;
        }
    }

    function refreshNormalMatchLimits(data) {
        const limits = data.camera_match_limits;
        cameraLimitsCurrent = Boolean(limits && typeof limits === 'object' && !Array.isArray(limits));
        normalMatchLimits = new Map(cameraLimitsCurrent
            ? Object.entries(limits).map(([cameraId, limit]) => [cameraId, validNormalMatchLimit(limit)])
            : []);
        if (cameraLimitsMessage) {
            cameraLimitsMessage.textContent = typeof data.camera_match_limits_message === 'string'
                ? data.camera_match_limits_message : '';
        }
        renderMatchLimits();
    }

    function syncCameraSelection(cameraId) {
        if (cameraId !== undefined) {
            const value = String(cameraId ?? '');
            runningCameraId = /^[1-9]\d*$/.test(value) ? value : '';
        }
        if (cameraSelect) {
            cameraSelect.disabled = sandboxRunning || startPending || cameraStateUncertain;
            if (sandboxRunning) cameraSelect.value = runningCameraId;
        }
        if (cameraSelectionStatus) {
            cameraSelectionStatus.textContent = cameraStateUncertain
                ? 'Test state could not be confirmed. Reload this page or stop the test before choosing another camera.'
                : sandboxRunning ? 'Stop the running test before choosing another camera.'
                : startPending ? 'Checking this camera. Please wait before choosing another camera.' : '';
        }
        renderMatchLimits();
    }

    cameraSelect?.addEventListener('change', () => {
        if (sandboxRunning) syncCameraSelection();
        else {
            if (useNormalMatchLimit) useNormalMatchLimit.checked = true;
            renderMatchLimits();
        }
    });
    sliderThreshold?.addEventListener('input', () => {
        if (useNormalMatchLimit) useNormalMatchLimit.checked = false;
        renderMatchLimits();
    });
    sliderThreshold?.addEventListener('change', renderMatchLimits);
    syncCameraSelection();

    // Save Modal Elements
    const btnOpenSaveModal = document.getElementById('btn-open-save-modal');
    const btnCancelSaveModal = document.getElementById('btn-cancel-save-modal');
    const btnConfirmSaveConfig = document.getElementById('btn-confirm-save-config');
    const btnApplyLive = document.getElementById('btn-apply-live');
    const saveModal = document.getElementById('save-config-modal');
    let saveModalReturnFocus = null;
    let saveConfigPending = false;

    // Metrics Elements
    const metricFps = document.getElementById('metric-fps');
    const metricCaptureFps = document.getElementById('metric-capture-fps');
    const metricPreviewFps = document.getElementById('metric-preview-fps');
    const metricPreviewAge = document.getElementById('metric-preview-age');
    const metricsStatus = document.getElementById('sandbox-metrics-status');
    const metricDetection = document.getElementById('metric-detection');
    const metricTrackerInit = document.getElementById('metric-tracker-init');
    const metricTrackerUpdate = document.getElementById('metric-tracker-update');
    const metricLiveness = document.getElementById('metric-liveness');
    const metricEmbedding = document.getElementById('metric-embedding');
    const metricMatching = document.getElementById('metric-matching');
    const metricTotal = document.getElementById('metric-total');

    const barDetection = document.getElementById('bar-detection');
    const barTrackerInit = document.getElementById('bar-tracker-init');
    const barTrackerUpdate = document.getElementById('bar-tracker-update');
    const barLiveness = document.getElementById('bar-liveness');
    const barEmbedding = document.getElementById('bar-embedding');
    const barMatching = document.getElementById('bar-matching');

    const metricStatus = document.getElementById('metric-match-status');
    const metricBestDist = document.getElementById('metric-best-distance');
    const metricSecondDist = document.getElementById('metric-second-distance');
    const metricMargin = document.getElementById('metric-margin');
    const metricMatchedStudent = document.getElementById('metric-matched-student');

    let statsPollInterval = null;
    let statsPollEpoch = 0;
    let statsPollPending = null;
    const btnTestSettings = document.getElementById('btn-test-settings');
    const btnResetSettings = document.getElementById('btn-reset-settings');
    const settingsStatus = document.getElementById('tuner-settings-status');
    const tuningControls = [sliderPasses, sliderThreshold, useNormalMatchLimit, sliderMargin, sliderDownscale, sliderSkip, requireLiveness].filter(Boolean);
    let savedControlValues = getControlValues();
    let testSettingsPending = false;
    let mutationPending = false;
    let stopPending = false;
    let settingsUncertain = false;
    let authorizationLost = false;
    let pageRetired = false;
    let mutationController = null;
    let viewingSlotsFull = false;
    const feedPlaceholderLines = feedPlaceholder
        ? feedPlaceholder.querySelectorAll('p')
        : [];

    function renderFeedConnectionState(state) {
        if (!feedPlaceholder || !sandboxRunning || (selectedFeedView === 'live' && smoothPreviewFailed)) return;
        if (['connecting', 'retrying', 'reconnecting', 'paused', 'exhausted'].includes(state)) {
            feedPlaceholder.style.display = 'flex';
            // Full server slots explain a waiting view, not a failure of the camera.
            // A connected view must remain visible even when it occupies the last slot.
            const waitingForSlot = viewingSlotsFull && ['retrying', 'reconnecting'].includes(state);
            if (feedPlaceholderLines[0]) {
                feedPlaceholderLines[0].textContent = state === 'exhausted' ? 'Preview recovery paused'
                    : state === 'paused'
                    ? 'Stream Recovery Paused'
                    : waitingForSlot ? 'Video viewing slots are full' : 'Reconnecting Camera Stream';
            }
            if (feedPlaceholderLines[1]) {
                feedPlaceholderLines[1].textContent = state === 'exhausted'
                    ? 'Automatic preview recovery reached its limit. Select another viewing format to retry deliberately; this does not restart the tuner or release its camera.'
                    : state === 'paused'
                    ? 'Recovery resumes when this tab and network are available.'
                    : waitingForSlot
                    ? 'Close an unused camera view to free a slot. This view retries automatically.'
                    : 'The tuner session is still running; only the image connection is restarting.';
            }
        } else if (state === 'connected') {
            feedPlaceholder.style.display = 'none';
        }
    }

    const feedWatchdog = feedImg && window.CsdMjpegFeed
        ? new window.CsdMjpegFeed.MjpegFeedWatchdog(feedImg, {
            baseUrl: selectedFeedUrl(),
            isActive: () => sandboxRunning && !(selectedFeedView === 'live' && smoothPreviewFailed),
            stallAfterMs: 15000,
            retryDelaysMs: [1000, 2000, 5000, 10000, 15000],
            onStateChange: renderFeedConnectionState
        })
        : null;

    function showSmoothPreviewFailure() {
        feedWatchdog?.stop();
        if (feedImg) {
            feedImg.src = 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=';
            feedImg.style.display = 'none';
        }
        if (feedPlaceholder) feedPlaceholder.style.display = 'flex';
        if (feedPlaceholderLines[0]) feedPlaceholderLines[0].textContent = 'Smooth preview unavailable';
        if (feedPlaceholderLines[1]) feedPlaceholderLines[1].textContent = 'Select Recognition boxes to continue viewing; recognition is still running.';
    }

    feedViewSelect?.addEventListener('change', () => {
        const nextView = feedViewSelect.value === 'recognition' ? 'recognition' : 'live';
        if (nextView === selectedFeedView) return;
        selectedFeedView = nextView;
        feedWatchdog?.stop();
        if (feedWatchdog) feedWatchdog.baseUrl = selectedFeedUrl();
        if (!sandboxRunning || !feedImg) return;
        if (selectedFeedView === 'live' && smoothPreviewFailed) {
            showSmoothPreviewFailure();
            return;
        }
        feedImg.style.display = 'block';
        if (feedWatchdog) feedWatchdog.start();
        else feedImg.src = `${selectedFeedUrl()}&t=${Date.now()}`;
    });

    // Helper: CSRF Header
    let renewedCsrfToken = '';
    function getCsrfToken() {
        return renewedCsrfToken || document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
    }

    function actionError(data, fallback) {
        if (data?.error === 'CSRF token missing or expired.') {
            return 'This page’s security token expired. Reload the page and try again.';
        }
        return data?.message || data?.error || fallback;
    }

    // Helper: Toast Notifications
    function showToast(message, type = 'info') {
        if (!toastContainer) return;
        const toast = document.createElement('div');
        toast.className = `flash flash--${type}`;
        toast.style.boxShadow = 'var(--shadow-md)';
        toast.style.minWidth = '240px';
        toast.textContent = message;
        toastContainer.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    }

    function syncMutationControls() {
        const locked = mutationPending || settingsUncertain || authorizationLost || pageRetired;
        for (const control of [...tuningControls, btnTestSettings, btnResetSettings,
            btnOpenSaveModal, btnConfirmSaveConfig, btnApplyLive]) {
            if (control) control.disabled = locked;
        }
        if (btnStart) btnStart.disabled = locked || startPending || cameraStateUncertain;
        // Stopping the camera remains available after an uncertain settings write.
        if (btnStop) btnStop.disabled = stopPending || authorizationLost || pageRetired;
        if (settingsUncertain && settingsStatus) settingsStatus.textContent = 'The settings change was not confirmed. It may have been applied. Reload this page to check saved values; stop and restart the tuner before comparing results.';
    }

    function denyActions(response) {
        authorizationLost = true;
        mutationController?.abort();
        setSessionState(false);
        if (feedViewSelect) feedViewSelect.disabled = true;
        syncMutationControls();
        AuthResponse.redirectIfUnauthorized(response, {});
    }

    async function requestAction(url, options) {
        // Stop may supersede a pending settings request. Aborting only retires
        // its browser result; it cannot undo a write already received by Flask.
        mutationController?.abort();
        const controller = new AbortController();
        mutationController = controller;
        mutationPending = true;
        stopPending = url.endsWith('/stop');
        syncMutationControls();
        let timer;
        try {
            const deadline = new Promise((_, reject) => {
                controller.signal.addEventListener('abort', () => reject(new Error('Request retired.')), {once: true});
                timer = setTimeout(() => {
                    controller.abort();
                    reject(new Error('The request timed out.'));
                }, 15000);
            });
            const request = (async () => {
                const res = await fetch(url, {...options, signal: controller.signal});
                if (controller.signal.aborted || pageRetired || authorizationLost) throw new Error('Request retired.');
                if (res.status === 401 || res.status === 403) {
                    denyActions(res);
                    return {res, data: {}};
                }
                const data = await AuthResponse.readJson(res);
                if (controller.signal.aborted || pageRetired || authorizationLost) throw new Error('Request retired.');
                if (res.status >= 500 || (res.ok && typeof data?.success !== 'boolean')) throw new Error('The result could not be verified.');
                return {res, data};
            })();
            return await Promise.race([request, deadline]);
        } catch (error) {
            if (!authorizationLost && !pageRetired && !url.endsWith('/start') && !url.endsWith('/stop')) {
                settingsUncertain = true;
            }
            throw error;
        } finally {
            clearTimeout(timer);
            if (mutationController === controller) {
                mutationController = null;
                mutationPending = false;
                stopPending = false;
            }
            syncMutationControls();
        }
    }

    // Read current control values
    function getControlValues() {
        return {
            REQUIRE_LIVENESS: requireLiveness ? requireLiveness.checked : false,
            MATCH_CONFIRMATION_PASSES: sliderPasses ? parseInt(sliderPasses.value, 10) : 2,
            MATCH_THRESHOLD: sliderThreshold ? parseFloat(sliderThreshold.value) : 0.45,
            FRAME_SKIP_INTERVAL: sliderSkip ? parseInt(sliderSkip.value, 10) : 5,
            MATCH_MARGIN: sliderMargin ? parseFloat(sliderMargin.value) : 0.08,
            DOWNSCALE_HEIGHT: sliderDownscale ? parseInt(sliderDownscale.value, 10) : 480
        };
    }

    function getTestValues() {
        const values = getControlValues();
        if (useNormalMatchLimit?.checked) {
            delete values.MATCH_THRESHOLD;
            values.use_normal_match_limit = true;
        }
        return values;
    }

    useNormalMatchLimit?.addEventListener('change', renderMatchLimits);

    // UI Value Label Updates
    if (sliderPasses && valPasses) {
        sliderPasses.addEventListener('input', () => valPasses.textContent = sliderPasses.value);
    }
    if (sliderThreshold && valThreshold) {
        sliderThreshold.addEventListener('input', () => valThreshold.textContent = parseFloat(sliderThreshold.value).toFixed(2));
    }
    if (sliderMargin && valMargin) {
        sliderMargin.addEventListener('input', () => valMargin.textContent = parseFloat(sliderMargin.value).toFixed(2));
    }
    if (sliderDownscale && valDownscale) {
        sliderDownscale.addEventListener('input', () => valDownscale.textContent = sliderDownscale.value);
    }
    if (sliderSkip && valSkip) {
        sliderSkip.addEventListener('input', () => valSkip.textContent = sliderSkip.value);
    }
    if (requireLiveness) {
        requireLiveness.addEventListener('change', () => {
            if (requireLivenessStatus) {
                requireLivenessStatus.textContent = requireLiveness.checked ? 'Required' : 'Advisory';
                requireLivenessStatus.className = requireLiveness.checked ? 'badge badge--danger' : 'badge badge--warning';
            }
            markUntestedChanges();
        });
    }

    // Moving a control never submits settings. The operator explicitly tests them.
    function markUntestedChanges() {
        if (settingsUncertain || authorizationLost || pageRetired) return;
        if (settingsStatus) settingsStatus.textContent = 'Untested changes. Start a test or click Test these settings. Saved attendance defaults are unchanged.';
    }

    function setTestSettingsPending(pending) {
        testSettingsPending = pending;
        if (btnTestSettings) btnTestSettings.disabled = pending;
        if (btnResetSettings) btnResetSettings.disabled = pending;
        if (btnStart) btnStart.disabled = pending;
        for (const control of tuningControls) control.disabled = pending;
        syncMutationControls();
    }

    for (const control of tuningControls) {
        control.addEventListener('input', markUntestedChanges);
        control.addEventListener('change', markUntestedChanges);
    }

    btnTestSettings?.addEventListener('click', async () => {
        if (testSettingsPending || mutationPending || settingsUncertain || authorizationLost || pageRetired) return;
        if (!sandboxRunning) {
            if (settingsStatus) settingsStatus.textContent = 'Choose your camera and click Start Tuner to test these values.';
            btnStart?.focus();
            return;
        }
        const overrides = getTestValues();
        setTestSettingsPending(true);
        try {
            const {res, data} = await requestAction('/api/diagnostics/sandbox/update_overrides', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
                body: JSON.stringify(overrides)
            });
            if (AuthResponse.redirectIfUnauthorized(res, data)) return;
            if (res.ok && data.success) {
                if (settingsStatus) settingsStatus.textContent = sandboxRunning
                    ? 'Settings applied to this test only. Check recognition before saving them for attendance.'
                    : 'The test has stopped. Saved attendance defaults are unchanged.';
            } else {
                showToast(actionError(data, 'Could not apply test settings.'), 'error');
                if (settingsStatus) settingsStatus.textContent = 'Test settings were not confirmed. Stop and restart the test before comparing results.';
            }
        } catch (err) {
            showToast('Could not confirm test settings. Stop and restart the test before comparing results.', 'error');
            if (settingsStatus && !settingsUncertain) settingsStatus.textContent = 'Test settings were not confirmed. Saved attendance defaults are unchanged.';
        } finally {
            setTestSettingsPending(false);
        }
    });

    btnResetSettings?.addEventListener('click', () => {
        if (testSettingsPending || mutationPending || settingsUncertain || authorizationLost || pageRetired) return;
        const fields = [
            [sliderPasses, valPasses, 'MATCH_CONFIRMATION_PASSES'],
            [sliderThreshold, valThreshold, 'MATCH_THRESHOLD'],
            [sliderMargin, valMargin, 'MATCH_MARGIN'],
            [sliderDownscale, valDownscale, 'DOWNSCALE_HEIGHT'],
            [sliderSkip, valSkip, 'FRAME_SKIP_INTERVAL']
        ];
        for (const [control, label, key] of fields) {
            if (control) control.value = String(savedControlValues[key]);
            if (label) label.textContent = String(savedControlValues[key]);
        }
        if (requireLiveness) requireLiveness.checked = savedControlValues.REQUIRE_LIVENESS;
        if (useNormalMatchLimit) useNormalMatchLimit.checked = true;
        if (requireLivenessStatus) {
            requireLivenessStatus.textContent = savedControlValues.REQUIRE_LIVENESS ? 'Required' : 'Advisory';
            requireLivenessStatus.className = savedControlValues.REQUIRE_LIVENESS ? 'badge badge--danger' : 'badge badge--warning';
        }
        renderMatchLimits();
        if (settingsStatus) settingsStatus.textContent = 'Sliders reset to the saved values loaded on this page. Start a test or click Test these settings to use them. This reset did not change a running session.';
    });

    document.getElementById('sandbox-overrides-form')?.addEventListener('submit', event => event.preventDefault());

    // Start Sandbox
    async function confirmSessionAfterStartFailure() {
        if (authorizationLost || pageRetired) return;
        cameraStateUncertain = true;
        syncCameraSelection();
        const controller = new AbortController();
        const epoch = statsPollEpoch;
        let timer;
        try {
            const request = (async () => {
                const res = await fetch('/api/diagnostics/sandbox/stats', {signal: controller.signal, cache: 'no-store'});
                if (controller.signal.aborted || pageRetired || authorizationLost || epoch !== statsPollEpoch) return null;
                if (res.status === 401 || res.status === 403) {
                    denyActions(res);
                    return null;
                }
                return {res, data: await AuthResponse.readJson(res)};
            })();
            const deadline = new Promise((_, reject) => {
                timer = setTimeout(() => { controller.abort(); reject(new Error('Status timed out.')); }, 4500);
            });
            const result = await Promise.race([request, deadline]);
            if (!result || controller.signal.aborted || pageRetired || authorizationLost || epoch !== statsPollEpoch) return;
            const {res, data} = result;
            if (AuthResponse.redirectIfUnauthorized(res, data)) return;
            if (!res.ok || typeof data?.active !== 'boolean') throw new Error('Status unavailable.');
            setSessionState(data.active, data.camera_id);
            if (data.active && settingsStatus) settingsStatus.textContent = 'A test is running. These sliders were not confirmed. Click Test these settings to apply them to this test.';
        } catch {
            if (pageRetired || authorizationLost || epoch !== statsPollEpoch) return;
            if (btnStop) btnStop.style.display = 'inline-block';
            if (settingsStatus) settingsStatus.textContent = 'Test state could not be confirmed. Stop the test or reload this page before continuing.';
        } finally {
            clearTimeout(timer);
            controller.abort();
        }
    }

    if (btnStart) {
        btnStart.addEventListener('click', async () => {
            if (testSettingsPending || startPending || sandboxRunning || cameraStateUncertain || mutationPending || settingsUncertain || authorizationLost || pageRetired) return;
            const cameraId = (cameraSelect && cameraSelect.value) ? cameraSelect.value : null;
            const overrides = getTestValues();

            try {
                startPending = true;
                syncCameraSelection();
                btnStart.disabled = true;
                const {res, data} = await requestAction('/api/diagnostics/sandbox/start', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': getCsrfToken()
                    },
                    body: JSON.stringify({ camera_id: cameraId, overrides: overrides })
                });
                if (AuthResponse.redirectIfUnauthorized(res, data)) return;
                if (res.ok && data.success) {
                    setSessionState(true, data.session?.camera_id);
                    if (data.created === true) {
                        showToast('Test started with the submitted settings.', 'success');
                        if (settingsStatus) settingsStatus.textContent = 'Test started with the submitted settings. Later slider changes need Test these settings before they affect this test.';
                    } else {
                        showToast('The running test kept its existing settings.', 'info');
                        if (settingsStatus) settingsStatus.textContent = 'An existing test is already running and kept its settings. These sliders were not applied. Click Test these settings to apply them to this test.';
                    }
                } else {
                    showToast(actionError(data, 'Failed to start the recognition tuner.'), 'error');
                    await confirmSessionAfterStartFailure();
                }
            } catch (err) {
                console.error(err);
                showToast('Network error starting the recognition tuner.', 'error');
                await confirmSessionAfterStartFailure();
            } finally {
                startPending = false;
                btnStart.disabled = cameraStateUncertain;
                syncCameraSelection();
                syncMutationControls();
            }
        });
    }

    // Stop Sandbox
    if (btnStop) {
        btnStop.addEventListener('click', async () => {
            if (stopPending || authorizationLost || pageRetired) return;
            try {
                btnStop.disabled = true;
                const {res, data} = await requestAction('/api/diagnostics/sandbox/stop', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': getCsrfToken()
                    }
                });
                if (AuthResponse.redirectIfUnauthorized(res, data)) return;
                if (res.ok && data.success) {
                    showToast('Recognition tuner stopped.', 'info');
                    setSessionState(false);
                } else {
                    showToast(actionError(data, 'Failed to stop the recognition tuner.'), 'error');
                }
            } catch (err) {
                console.error(err);
                cameraStateUncertain = true;
                syncCameraSelection();
                showToast('Could not confirm that the tuner stopped. Check the connection and try Stop again.', 'error');
            } finally {
                btnStop.disabled = false;
                syncMutationControls();
            }
        });
    }

    // Set UI Session State
    function setSessionState(isRunning, cameraId) {
        if (isRunning && (authorizationLost || pageRetired)) return;
        sandboxRunning = isRunning;
        cameraStateUncertain = false;
        if (!isRunning) runningCameraId = '';
        syncCameraSelection(cameraId);
        if (btnStart) btnStart.disabled = startPending;
        viewingSlotsFull = false;
        if (isRunning) {
            if (btnStart) btnStart.style.display = 'none';
            if (btnStop) btnStop.style.display = 'inline-block';
            if (feedPlaceholder) feedPlaceholder.style.display = 'none';
            if (feedImg) {
                feedImg.style.display = 'block';
                if (feedWatchdog) {
                    feedWatchdog.start();
                } else {
                    feedImg.src = `${selectedFeedUrl()}&t=${Date.now()}`;
                }
            }
            if (statusBadge) {
                statusBadge.textContent = 'Session Active';
                statusBadge.className = 'badge badge--success';
            }
            if (metricsStatus) metricsStatus.textContent = 'Measuring camera and recognition performance…';
            startStatsPolling();
        } else {
            feedWatchdog?.stop();
            if (btnStart) btnStart.style.display = 'inline-block';
            if (btnStop) btnStop.style.display = 'none';
            if (feedImg) {
                feedImg.style.display = 'none';
                feedImg.src = 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=';
            }
            if (feedPlaceholder) feedPlaceholder.style.display = 'flex';
            if (feedPlaceholderLines[0]) feedPlaceholderLines[0].textContent = 'Tuner Stream Inactive';
            if (feedPlaceholderLines[1]) {
                feedPlaceholderLines[1].textContent = 'Select a camera device and click "Start Tuner" to begin real-time tuning.';
            }
            if (statusBadge) {
                statusBadge.textContent = 'Session Stopped';
                statusBadge.className = 'badge badge--warning';
            }
            stopStatsPolling();
            resetMetricsDisplay();
            if (settingsStatus) settingsStatus.textContent = 'Test stopped. Saved attendance defaults are unchanged unless you explicitly saved them. The sliders are ready for the next test.';
        }
        syncMutationControls();
    }

    // Stats Polling Loop
    function startStatsPolling() {
        stopStatsPolling();
        pollStats();
        statsPollInterval = setInterval(pollStats, 1000);
    }

    function stopStatsPolling() {
        statsPollEpoch++;
        statsPollPending?.abort();
        statsPollPending = null;
        if (statsPollInterval) {
            clearInterval(statsPollInterval);
            statsPollInterval = null;
        }
    }

    async function pollStats() {
        if (!sandboxRunning || statsPollPending) return;
        const epoch = statsPollEpoch;
        const controller = new AbortController();
        statsPollPending = controller;
        let timeout;
        try {
            const deadline = new Promise((_, reject) => {
                timeout = setTimeout(() => {
                    controller.abort();
                    reject(new Error('Performance readings timed out.'));
                }, 4500);
            });
            const request = (async () => {
                const res = await fetch('/api/diagnostics/sandbox/stats', {signal: controller.signal, cache: 'no-store'});
                if (epoch !== statsPollEpoch || controller.signal.aborted) return null;
                // Clear the viewer immediately even if an unauthorized response body stalls.
                if (res.status === 401 || res.status === 403) {
                    denyActions(res);
                    return null;
                }
                return {res, data: await AuthResponse.readJson(res)};
            })();
            const result = await Promise.race([request, deadline]);
            if (!result || epoch !== statsPollEpoch || !sandboxRunning) return;
            const {res, data} = result;
            if (!res.ok) throw new Error('Performance readings are unavailable.');
            if (!data || typeof data.active !== 'boolean') throw new Error('Performance readings are invalid.');

            // Signed tokens expire even while authenticated polling keeps the login active.
            // Refresh from an authorized response; never replay a rejected write.
            if (typeof data.csrf_token === 'string' && data.csrf_token) renewedCsrfToken = data.csrf_token;

            if (!data.active) {
                setSessionState(false);
                return;
            }
            syncCameraSelection(data.camera_id);

            const capacity = data.video_capacity;
            viewingSlotsFull = capacity?.available !== false
                && Number.isInteger(capacity?.active_streams) && capacity.active_streams >= 0
                && Number.isInteger(capacity?.max_streams) && capacity.max_streams >= 0
                && capacity.active_streams >= capacity.max_streams;

            const previewWasFailed = smoothPreviewFailed;
            if (data.preview_state === 'failed') smoothPreviewFailed = true;
            else if (data.preview_state === 'running') smoothPreviewFailed = false;
            if (selectedFeedView === 'live' && smoothPreviewFailed) {
                showSmoothPreviewFailure();
            } else if (selectedFeedView === 'live' && previewWasFailed && !smoothPreviewFailed && feedImg) {
                feedImg.style.display = 'block';
                if (feedWatchdog) feedWatchdog.start();
                else feedImg.src = `${selectedFeedUrl()}&t=${Date.now()}`;
            }

            const sequence = selectedFeedView === 'live' ? data.preview_publication_sequence : data.publication_sequence;
            const frameAge = selectedFeedView === 'live' ? data.preview_current_frame_age_ms : data.current_frame_age_ms;
            if (Number.isInteger(sequence) && sequence >= 0 && Number.isFinite(frameAge) && frameAge >= 0) {
                feedWatchdog?.observeProgress(sequence, frameAge);
            }
            renderFeedConnectionState(feedWatchdog?.state);

            // Update Latencies & FPS
            const measured = value => Number.isFinite(value) && value >= 0 ? value : null;
            const format = (value, digits, unit) => `${measured(value) === null ? '--' : value.toFixed(digits)} ${unit}`;
            const detectMs = measured(data.detect_ms);
            const trackerInitMs = measured(data.tracker_init_ms);
            const trackerUpdateMs = measured(data.tracker_update_ms);
            const livenessMs = measured(data.rolling_liveness_ms);
            const embeddingMs = measured(data.encode_face_ms);
            const matchingMs = measured(data.match_ms);
            const totalMs = measured(data.loop_ms);

            if (metricFps) metricFps.textContent = format(data.fps, 1, 'FPS');
            if (metricCaptureFps) metricCaptureFps.textContent = format(data.capture_fps, 1, 'FPS');
            if (metricPreviewFps) metricPreviewFps.textContent = format(data.preview_fps, 1, 'FPS');
            if (metricPreviewAge) metricPreviewAge.textContent = format(data.preview_current_frame_age_ms, 0, 'ms');
            if (metricDetection) metricDetection.textContent = format(detectMs, 2, 'ms');
            if (metricTrackerInit) metricTrackerInit.textContent = format(trackerInitMs, 2, 'ms');
            if (metricTrackerUpdate) metricTrackerUpdate.textContent = format(trackerUpdateMs, 2, 'ms');
            if (metricLiveness) metricLiveness.textContent = format(livenessMs, 2, 'ms');
            if (metricEmbedding) metricEmbedding.textContent = format(embeddingMs, 2, 'ms');
            if (metricMatching) metricMatching.textContent = format(matchingMs, 2, 'ms');
            if (metricTotal) metricTotal.textContent = format(totalMs, 2, 'ms');
            if (metricsStatus) metricsStatus.textContent = smoothPreviewFailed
                ? 'Smooth preview unavailable. Recognition-box view remains available.'
                : data.capture_state === 'reconnecting'
                ? 'Camera reconnecting. These are the latest measured processing times.'
                : 'Performance readings updated. Stage averages retain the latest measured work.';

            // Per-operation averages are not shares of a cycle: detection can be skipped.
            const maxVal = Math.max(detectMs || 0, trackerInitMs || 0, trackerUpdateMs || 0,
                livenessMs || 0, embeddingMs || 0, matchingMs || 0, 1);
            if (barDetection) barDetection.style.width = `${((detectMs || 0) / maxVal) * 100}%`;
            if (barTrackerInit) barTrackerInit.style.width = `${((trackerInitMs || 0) / maxVal) * 100}%`;
            if (barTrackerUpdate) barTrackerUpdate.style.width = `${((trackerUpdateMs || 0) / maxVal) * 100}%`;
            if (barLiveness) barLiveness.style.width = `${((livenessMs || 0) / maxVal) * 100}%`;
            if (barEmbedding) barEmbedding.style.width = `${((embeddingMs || 0) / maxVal) * 100}%`;
            if (barMatching) barMatching.style.width = `${((matchingMs || 0) / maxVal) * 100}%`;

            // Update Biometric Match Stats
            const info = data.match_info || {};
            const best = info.best_distance ?? data.best_distance;
            const second = info.second_distance ?? data.second_distance;
            const margin = info.margin ?? data.margin;

            if (metricBestDist) metricBestDist.textContent = Number.isFinite(best) ? best.toFixed(4) : 'N/A';
            if (metricSecondDist) metricSecondDist.textContent = Number.isFinite(second) ? second.toFixed(4) : 'N/A';
            if (metricMargin) metricMargin.textContent = Number.isFinite(margin) ? margin.toFixed(4) : 'N/A';

            if (metricMatchedStudent) {
                if (info.confirmed === true && info.accepted && info.student_name) {
                    metricMatchedStudent.textContent = `${info.student_name} (#${info.student_id || '?'})`;
                } else {
                    metricMatchedStudent.textContent = 'None';
                }
            }

            if (metricStatus) {
                if (info.confirmed === true && info.accepted) {
                    metricStatus.textContent = 'MATCH CONFIRMED';
                    metricStatus.className = 'badge badge--success';
                } else if (info.accepted) {
                    metricStatus.textContent = Number.isInteger(info.confirmation_count) && Number.isInteger(info.confirmations_required)
                        ? `VERIFYING (${info.confirmation_count}/${info.confirmations_required})` : 'CANDIDATE ONLY';
                    metricStatus.className = 'badge badge--warning';
                } else if (best !== null && best !== undefined) {
                    metricStatus.textContent = info.reason === 'duplicate_face_candidate'
                        ? 'UNCERTAIN (SAME PROFILE ON TWO FACES)'
                        : info.reason === 'ambiguous_second_best'
                        ? 'UNCERTAIN (SIMILAR CANDIDATES)'
                        : info.reason === 'above_threshold' ? 'NO MATCH (DISTANCE)' : 'NO MATCH';
                    metricStatus.className = 'badge badge--danger';
                } else {
                    metricStatus.textContent = 'SEARCHING...';
                    metricStatus.className = 'badge badge--primary';
                }
            }
            const history = document.getElementById('match-history');
            const matchContext = document.getElementById('metric-match-context');
            const faceDescription = item => {
                const position = ['left', 'center', 'right'].includes(item.position) ? item.position : 'unspecified';
                return Number.isInteger(item.face_number) && Number.isInteger(item.face_count)
                    && item.face_number > 0 && item.face_number <= item.face_count
                    ? `Face ${item.face_number} of ${item.face_count} (${position})` : `${position} face`;
            };
            if (matchContext) {
                matchContext.textContent = Number.isFinite(info.evaluated_at)
                    ? `Last comparison: ${new Date(info.evaluated_at * 1000).toLocaleTimeString()} · ${faceDescription(info)} · frame ${info.source_frame}. This is a recorded check, not a current identity.`
                    : 'No timed comparison available.';
            }
            if (history) {
                const rows = Array.isArray(data.match_history) ? data.match_history.slice(-20).reverse() : [];
                history.textContent = rows.map(item => {
                    const time = Number.isFinite(item.evaluated_at) ? new Date(item.evaluated_at * 1000).toLocaleTimeString() : 'Time unavailable';
                    const result = item.confirmed === true && item.accepted && item.student_name
                        ? `Confirmed: ${item.student_name}`
                        : item.reason === 'duplicate_face_candidate' ? 'Uncertain: same profile on two faces'
                        : item.accepted ? `Verifying identity (${item.confirmation_count}/${item.confirmations_required})` : 'No match';
                    const distance = Number.isFinite(item.best_distance) ? item.best_distance.toFixed(4) : 'unavailable';
                    const frame = Number.isInteger(item.source_frame) ? ` · frame ${item.source_frame}` : '';
                    return `${time} · ${faceDescription(item)}${frame} · ${result} · distance ${distance}`;
                }).join('\n') || 'No comparisons yet.';
            }
        } catch (err) {
            if (epoch !== statsPollEpoch || !sandboxRunning) return;
            console.error('Failed polling sandbox stats:', err);
            resetMetricsDisplay();
            if (metricsStatus) metricsStatus.textContent = 'Performance readings unavailable. Retrying automatically.';
        } finally {
            clearTimeout(timeout);
            if (statsPollPending === controller) statsPollPending = null;
        }
    }

    function resetMetricsDisplay() {
        const history = document.getElementById('match-history');
        if (history) history.textContent = 'No comparisons yet.';
        const matchContext = document.getElementById('metric-match-context');
        if (matchContext) matchContext.textContent = 'No comparisons yet.';
        viewingSlotsFull = false;
        renderFeedConnectionState(feedWatchdog?.state);
        if (metricFps) metricFps.textContent = '-- FPS';
        if (metricCaptureFps) metricCaptureFps.textContent = '-- FPS';
        if (metricPreviewFps) metricPreviewFps.textContent = '-- FPS';
        if (metricPreviewAge) metricPreviewAge.textContent = '-- ms';
        if (metricDetection) metricDetection.textContent = '-- ms';
        if (metricTrackerInit) metricTrackerInit.textContent = '-- ms';
        if (metricTrackerUpdate) metricTrackerUpdate.textContent = '-- ms';
        if (metricLiveness) metricLiveness.textContent = '-- ms';
        if (metricEmbedding) metricEmbedding.textContent = '-- ms';
        if (metricMatching) metricMatching.textContent = '-- ms';
        if (metricTotal) metricTotal.textContent = '-- ms';
        if (metricsStatus) metricsStatus.textContent = sandboxRunning
            ? 'Performance readings unavailable.' : 'Start the tuner to measure performance.';

        if (barDetection) barDetection.style.width = '0%';
        if (barTrackerInit) barTrackerInit.style.width = '0%';
        if (barTrackerUpdate) barTrackerUpdate.style.width = '0%';
        if (barLiveness) barLiveness.style.width = '0%';
        if (barEmbedding) barEmbedding.style.width = '0%';
        if (barMatching) barMatching.style.width = '0%';

        if (metricStatus) {
            metricStatus.textContent = sandboxRunning ? 'Reading unavailable' : 'No Active Stream';
            metricStatus.className = 'badge badge--warning';
        }
        if (metricBestDist) metricBestDist.textContent = 'N/A';
        if (metricSecondDist) metricSecondDist.textContent = 'N/A';
        if (metricMargin) metricMargin.textContent = 'N/A';
        if (metricMatchedStudent) metricMatchedStudent.textContent = 'N/A';
    }

    function getSaveModalFocusableElements() {
        if (!saveModal) return [];
        return Array.from(saveModal.querySelectorAll(
            'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'
        )).filter(element => element.offsetParent !== null);
    }

    function closeSaveModal() {
        if (!saveModal || saveConfigPending) return;
        saveModal.style.display = 'none';
        saveModal.setAttribute('aria-hidden', 'true');
        if (saveModalReturnFocus && document.contains(saveModalReturnFocus)) {
            saveModalReturnFocus.focus();
        }
        saveModalReturnFocus = null;
    }

    function setSaveConfigPending(pending) {
        saveConfigPending = pending;
        if (btnConfirmSaveConfig) btnConfirmSaveConfig.disabled = pending;
        if (btnCancelSaveModal) btnCancelSaveModal.disabled = pending;
        const dialog = saveModal?.querySelector('[role="dialog"]');
        if (pending) {
            dialog?.setAttribute('aria-busy', 'true');
        } else {
            dialog?.removeAttribute('aria-busy');
        }
        syncMutationControls();
    }

    // Save Modal Dialog Handlers
    if (btnOpenSaveModal && saveModal) {
        btnOpenSaveModal.addEventListener('click', event => {
            if (saveConfigPending || mutationPending || settingsUncertain || authorizationLost || pageRetired) return;
            const vals = getControlValues();
            const confirmLiveness = document.getElementById('confirm-val-liveness');
            const confirmPasses = document.getElementById('confirm-val-passes');
            const confirmThreshold = document.getElementById('confirm-val-threshold');
            const confirmMargin = document.getElementById('confirm-val-margin');
            const confirmDownscale = document.getElementById('confirm-val-downscale');
            const confirmSkip = document.getElementById('confirm-val-skip');

            if (confirmLiveness) confirmLiveness.textContent = vals.REQUIRE_LIVENESS ? 'Required' : 'Advisory';
            if (confirmPasses) confirmPasses.textContent = vals.MATCH_CONFIRMATION_PASSES;
            if (confirmThreshold) confirmThreshold.textContent = useNormalMatchLimit?.checked ? 'Unchanged; normal camera limits continue to apply' : vals.MATCH_THRESHOLD.toFixed(2);
            if (confirmMargin) confirmMargin.textContent = vals.MATCH_MARGIN.toFixed(2);
            if (confirmDownscale) confirmDownscale.textContent = vals.DOWNSCALE_HEIGHT;
            if (confirmSkip) confirmSkip.textContent = vals.FRAME_SKIP_INTERVAL;

            saveModalReturnFocus = event.currentTarget;
            saveModal.style.display = 'flex';
            saveModal.setAttribute('aria-hidden', 'false');
            requestAnimationFrame(() => btnCancelSaveModal?.focus());
        });
    }

    if (btnCancelSaveModal && saveModal) {
        btnCancelSaveModal.addEventListener('click', closeSaveModal);
    }

    if (saveModal) {
        saveModal.addEventListener('click', event => {
            if (event.target === saveModal) closeSaveModal();
        });

        document.addEventListener('keydown', event => {
            if (saveModal.style.display === 'none') return;
            if (event.key === 'Escape') {
                event.preventDefault();
                closeSaveModal();
                return;
            }
            if (event.key !== 'Tab') return;

            const focusable = getSaveModalFocusableElements();
            if (!focusable.length) {
                event.preventDefault();
                saveModal.querySelector('[role="dialog"]')?.focus();
                return;
            }
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (!focusable.includes(document.activeElement)) {
                event.preventDefault();
                (event.shiftKey ? last : first).focus();
            } else if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        });
    }

    if (btnConfirmSaveConfig) {
        btnConfirmSaveConfig.addEventListener('click', async () => {
            if (saveConfigPending || mutationPending || settingsUncertain || authorizationLost || pageRetired) return;
            const configData = getControlValues();
            if (useNormalMatchLimit?.checked) delete configData.MATCH_THRESHOLD;
            setSaveConfigPending(true);
            try {
                const {res, data} = await requestAction('/api/diagnostics/sandbox/save_config', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': getCsrfToken()
                    },
                    body: JSON.stringify(configData)
                });
                if (AuthResponse.redirectIfUnauthorized(res, data)) return;
                if (res.ok && data.success) {
                    savedControlValues = {...savedControlValues, ...configData, ...data.config};
                    refreshNormalMatchLimits(data);
                    showToast('System defaults saved for future sessions. Stricter camera limits still apply.', 'success');
                    setSaveConfigPending(false);
                    closeSaveModal();
                } else {
                    showToast(actionError(data, 'Failed to save configuration.'), 'error');
                }
            } catch (err) {
                console.error(err);
                showToast('Save could not be confirmed. Reload to check the saved values before trying again.', 'error');
            } finally {
                setSaveConfigPending(false);
            }
        });
    }

    if (btnApplyLive) {
        btnApplyLive.addEventListener('click', async () => {
            if (mutationPending || settingsUncertain || authorizationLost || pageRetired) return;
            const configData = getTestValues();
            try {
                btnApplyLive.disabled = true;
                const originalText = btnApplyLive.textContent;
                btnApplyLive.textContent = 'Applying...';

                const {res, data} = await requestAction('/api/diagnostics/sandbox/apply_to_live', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': getCsrfToken()
                    },
                    body: JSON.stringify(configData)
                });
                if (AuthResponse.redirectIfUnauthorized(res, data)) return;

                if (res.ok && data.success) {
                    showToast(data.message, 'success');
                } else {
                    showToast(actionError(data, 'Failed to apply configuration to live session.'), 'error');
                }
                btnApplyLive.textContent = originalText;
            } catch (err) {
                console.error(err);
                showToast('Apply could not be confirmed. Stop affected attendance sessions before starting again with checked settings.', 'error');
                btnApplyLive.textContent = 'Apply to ongoing attendance';
            } finally {
                btnApplyLive.disabled = false;
                syncMutationControls();
            }
        });
    }

    // Auto-start polling if session was already running on initial page load
    if (statusBadge && statusBadge.classList.contains('badge--success')) {
        setSessionState(true);
    }

    // Auto-stop camera when leaving the page
    function retirePage() {
        if (pageRetired) return;
        pageRetired = true;
        mutationController?.abort();
        syncMutationControls();
        stopStatsPolling();
        feedWatchdog?.destroy();
        if (btnStop && btnStop.style.display !== 'none') {
            fetch('/api/diagnostics/sandbox/stop', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                keepalive: true
            }).catch(console.error);
        }
    }
    window.addEventListener('beforeunload', retirePage);
    window.addEventListener('pagehide', retirePage);
});
