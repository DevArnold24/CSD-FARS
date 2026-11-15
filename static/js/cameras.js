// The main toolbar owns these actions, including when HTML was cached before an upgrade.
for (const id of ['btn-new-camera', 'btn-refresh-camera-status']) document.getElementById(id)?.remove();
// Support a running server's cached template without resetting its practice data.
const removalRetireButton = document.getElementById('camera-removal-retire') || (() => {
    const button = document.createElement('button');
    button.id = 'camera-removal-retire';
    button.type = 'button';
    button.className = 'btn btn--primary';
    button.textContent = 'Retire camera…';
    button.hidden = true;
    button.disabled = true;
    document.getElementById('camera-removal-confirm').insertAdjacentElement('beforebegin', button);
    return button;
})();
const roomSelect = document.getElementById('wizard-room-select');
const cameraForm = document.getElementById('wizard-camera-form');
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
let rooms = Array.isArray(window.CSD_FARS_ROOMS) ? window.CSD_FARS_ROOMS : [];
let coverage = Array.isArray(window.CSD_FARS_ROOM_COVERAGE) ? window.CSD_FARS_ROOM_COVERAGE : [];
let cameras = [];
let inventoryPage = 1, coveragePage = 1;
let managementView = 'inventory';
let editorSnapshot = null;
const pageSize = 25;
let wizardBusy = false;
let selectedCameraRequiresPath = false;
let roomCategory = 'all';
let savedDataLoading = false;
let savedDataRequest = null;
let savedDataLoaded = false;
let testPreviewTimer = null;
let testPreviewStartedAt = null;
const cameraActionsPending = new Set();
let cameraRemovalGeneration = 0;
let cameraRemoval = null;
let managedCameraId = null;
let cameraWorkspaceSection = 'configuration';
let pendingCameraAvailability = null;
let cameraAvailabilityNeedsRefresh = false;
const connectionFields = ['camera-ip', 'camera-username', 'camera-password', 'camera-manual-rtsp', 'camera-local-index', 'camera-rtsp-port', 'camera-rtsp-path'];
const connectionKind = document.getElementById('camera-connection-kind');
const coverageFilters = ['room-filter-search', 'room-filter-building', 'room-filter-floor', 'room-filter-type', 'room-filter-health', 'room-filter-coverage', 'room-filter-assignment', 'room-group-by'];

function cameraEscape(value) {
    const element = document.createElement('span');
    element.textContent = value == null ? '' : String(value);
    return element.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function showStep(step, focus = true) {
    if (step !== 3) {
        clearTimeout(testPreviewTimer);
        testPreviewStartedAt = null;
        document.getElementById('test-frame').removeAttribute('src');
    }
    for (let index = 1; index <= 3; index += 1) {
        const nav = document.getElementById(`step-${index}-nav`);
        const content = document.getElementById(`wizard-step-${index}`);
        nav?.classList.toggle('active', index === step);
        if (index === step) nav?.setAttribute('aria-current', 'step');
        else nav?.removeAttribute('aria-current');
        if (content) content.style.display = index === step ? 'block' : 'none';
    }
    const focusTarget = {
        1: 'wizard-room-select',
        2: 'camera-name',
        3: 'wizard-verification-title'
    }[step];
    const focusElement = document.getElementById(focusTarget);
    if (focusElement && focus) {
        focusElement.focus({ preventScroll: true });
        focusElement.scrollIntoView({ block: 'center', behavior: 'instant' });
    }
}

function selectedRoom() {
    return rooms.find(room => Number(room.id) === Number(roomSelect.value));
}

function cameraPayload() {
    const id = document.getElementById('camera-id').value;
    const payload = {
        name: document.getElementById('camera-name').value,
        room_id: roomSelect.value,
        // These are not editor fields. Let PUT merge the current database
        // values so a stale editor cannot undo retirement/default replacement.
        ...(id ? {} : {placement_notes: '', lifecycle_status: 'Ready', is_default: false}),
        resolution_width: document.getElementById('camera-width').value,
        resolution_height: document.getElementById('camera-height').value,
        target_fps: document.getElementById('camera-fps').value,
        match_threshold: document.getElementById('camera-match-threshold').value
    };
    // Empty fields on an existing camera mean keep its encrypted connection.
    // Never read the masked source back into a credential or connection input.
    if (connectionKind.value !== 'keep') {
        payload.replace_connection = true;
        payload.camera_type = connectionKind.value === 'local' ? 'local' : 'rtsp';
        if (connectionKind.value === 'local') {
            payload.source = document.getElementById('camera-local-index').value;
        } else if (connectionKind.value === 'stream') {
            payload.network_url = document.getElementById('camera-manual-rtsp').value;
        } else {
            const fullUrl = document.getElementById('camera-manual-rtsp').value;
            if (fullUrl.trim()) payload.manual_rtsp_url = fullUrl;
            else Object.assign(payload, {
                ip: document.getElementById('camera-ip').value,
                username: document.getElementById('camera-username').value,
                password: document.getElementById('camera-password').value,
                rtsp_port: document.getElementById('camera-rtsp-port').value,
                rtsp_path: document.getElementById('camera-rtsp-path').value
            });
        }
    }
    return payload;
}

function clearConnectionFields(editing = false) {
    selectedCameraRequiresPath = false;
    connectionFields.forEach(id => { document.getElementById(id).value = ''; });
    connectionKind.value = editing ? 'keep' : 'rtsp';
    document.getElementById('camera-keep-connection').hidden = !editing;
    document.getElementById('camera-manual-settings').open = false;
    document.getElementById('camera-url-fields').open = false;
    document.getElementById('camera-selection-status').textContent = '';
    window.CameraDiscovery?.reset();
    updateConnectionUI();
    const discovered = document.getElementById('discover-results');
    discovered.replaceChildren();
    discovered.style.display = 'none';
}

function updateConnectionUI() {
    const kind = connectionKind.value;
    const local = kind === 'local', rtsp = kind === 'rtsp', stream = kind === 'stream';
    document.getElementById('camera-local-fields').style.display = local ? 'block' : 'none';
    document.getElementById('camera-rtsp-fields').style.display = rtsp ? 'block' : 'none';
    document.getElementById('camera-url-fields').style.display = rtsp || stream ? 'block' : 'none';
    const fullUrl = document.getElementById('camera-manual-rtsp').value.trim();
    const visibleFields = local ? ['camera-local-index'] : stream || (rtsp && fullUrl) ? ['camera-manual-rtsp'] : rtsp ? connectionFields.filter(id => id !== 'camera-local-index') : [];
    connectionFields.forEach(id => { document.getElementById(id).disabled = wizardBusy || !visibleFields.includes(id); });
    document.getElementById('btn-discover-cameras').disabled = wizardBusy || !rtsp;
    document.getElementById('camera-local-index').required = local;
    document.getElementById('camera-manual-rtsp').required = stream;
    document.getElementById('camera-rtsp-path').required = rtsp && selectedCameraRequiresPath && !fullUrl;
    if (stream) document.getElementById('camera-url-fields').open = true;
    document.getElementById('camera-manual-rtsp').placeholder = stream ? 'https://camera.example.invalid/video' : 'rtsp://camera.example.invalid/stream';
    document.getElementById('camera-source-help').textContent = local
        ? 'Connect a USB, built-in or virtual webcam to the computer running CSD FARS. This does not use browser webcam permissions.'
        : stream ? 'Use a direct RTSP, RTSPS, HTTP or HTTPS video stream, such as MJPEG. Camera login pages, cloud-viewer links and still-image URLs are not live video streams. Support depends on the device and video format; use Test saved camera to check for a readable frame.'
        : rtsp ? 'Power on the camera and connect it to the same router or switch as this system. Select Find cameras, then choose yours.'
        : 'The saved connection is encrypted and will be kept. To replace it, choose a connection type and enter its details.';
    document.getElementById('camera-url-help').textContent = stream
        ? 'Include credentials in the URL if required. The URL is masked because it may contain a password. Use the direct stream address from the camera or recorder settings.'
        : 'A full URL overrides the address and credential fields above. Include credentials in the URL if required. The URL is masked because it may contain a password.';
    window.CameraDiscovery?.syncControls();
}
connectionKind.addEventListener('change', () => {
    selectedCameraRequiresPath = false;
    connectionFields.forEach(id => { document.getElementById(id).value = ''; });
    document.getElementById('camera-manual-settings').open = false;
    document.getElementById('camera-url-fields').open = false;
    document.getElementById('camera-selection-status').textContent = '';
    window.CameraDiscovery?.reset();
    document.getElementById('discover-results').replaceChildren();
    document.getElementById('discover-results').style.display = 'none';
    updateConnectionUI();
});
document.getElementById('camera-manual-rtsp').addEventListener('input', updateConnectionUI);

function setWizardBusy(busy) {
    wizardBusy = busy;
    ['btn-cancel-camera', 'btn-test-saved-camera', 'btn-add-camera', 'btn-refresh-inventory', 'btn-next-to-2', 'btn-next-to-3', 'btn-back-to-1', 'btn-back-to-2', 'btn-retry-test', 'btn-discover-cameras', 'wizard-room-select', 'camera-name', 'camera-width', 'camera-height', 'camera-fps', 'camera-match-threshold', 'camera-connection-kind', ...connectionFields].forEach(id => {
        const control = document.getElementById(id);
        if (control) control.disabled = busy;
    });
    cameraForm.setAttribute('aria-busy', String(busy));
    updateConnectionUI();
    document.getElementById('btn-next-to-3').textContent = busy ? 'Please wait…' : 'Save changes';
    syncEditorControls();
    renderCoverage();
}

async function cameraRequest(url, options = {}) {
    const controller = new AbortController();
    const isCameraTest = /^\/api\/cameras\/\d+\/test$/.test(url);
    const testTimeoutMessage = 'The request timed out. The camera test outcome is unknown. Refresh saved camera status before retrying.';
    let timeout;
    const deadline = new Promise((_,reject) => {
        timeout = setTimeout(() => {
            controller.abort();
            reject(new Error(isCameraTest ? testTimeoutMessage : 'The request timed out. Refresh saved camera status before retrying.'));
        },30000);
    });
    try {
        const {response,data} = await Promise.race([(async () => {
            let response;
            try {
                response = await fetch(url, {...options, signal:controller.signal});
            } catch (error) {
                if (isCameraTest) {
                    // A missing HTTP response identifies browser/server trouble,
                    // not a failed camera stream. Never display a raw transport error.
                    throw new Error(error?.name === 'AbortError' ? testTimeoutMessage : 'The CSD FARS server could not be reached from this browser. The camera test outcome is unknown. Restore the browser connection to CSD FARS, then refresh saved camera status before retrying. This does not identify a camera stream fault.');
                }
                throw error;
            }
            return {response,data:await AuthResponse.readJson(response)};
        })(),deadline]);
        if (AuthResponse.redirectIfUnauthorized(response, data)) return null;
        if (!response.ok) {
            const error = new Error(typeof data?.error === 'string' && data.error ? data.error : 'The camera request failed. Please try again.');
            // Only camera tests expose these diagnostics. Other request errors
            // keep their existing presentation and recovery workflow.
            if (isCameraTest) {
                error.diagnostic = cameraTestDiagnostic(data?.diagnostic);
            }
            throw error;
        }
        return data;
    } catch (error) {
        if (error.name === 'AbortError') throw new Error('The request timed out. Check the camera coverage, then try again.');
        if (error instanceof TypeError || error.name === 'TypeError') throw new Error('The server could not be reached. Check your connection, then try again.');
        throw error;
    } finally {
        clearTimeout(timeout);
    }
}

function cameraTestDiagnostic(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    if (typeof value.code !== 'string' || !/^[A-Za-z0-9_-]{1,80}$/.test(value.code)) return null;
    if (['title', 'stage', 'summary'].some(key => typeof value[key] !== 'string' || !value[key].trim() || value[key].length > 1500)) return null;
    if (!Array.isArray(value.checks) || value.checks.length > 10 || value.checks.some(check => typeof check !== 'string' || !check.trim() || check.length > 1500)) return null;
    return {code: value.code, title: value.title, stage: value.stage, summary: value.summary, checks: [...value.checks]};
}

function clearCameraTestDiagnostic() {
    document.getElementById('test-error-checks').replaceChildren();
    document.getElementById('test-error-guidance').hidden = true;
    document.getElementById('test-error-reference').textContent = '';
    document.getElementById('test-error-reference').hidden = true;
}

function showCameraTestError(error) {
    clearCameraTestDiagnostic();
    const diagnostic = cameraTestDiagnostic(error.diagnostic);
    document.getElementById('test-error-title').textContent = diagnostic?.title || 'Camera test could not be completed';
    document.getElementById('test-error-msg').textContent = `${diagnostic?.summary || error.message} Your camera settings are saved. Review the guidance or edit the camera details.`;
    if (diagnostic) {
        const checks = document.getElementById('test-error-checks');
        diagnostic.checks.forEach(check => {
            const item = document.createElement('li');
            item.textContent = check;
            checks.appendChild(item);
        });
        document.getElementById('test-error-guidance').hidden = diagnostic.checks.length === 0;
        document.getElementById('test-error-reference').textContent = `Stage: ${diagnostic.stage} · Reference code: ${diagnostic.code}`;
        document.getElementById('test-error-reference').hidden = false;
    }
    document.getElementById('test-error').style.display = 'block';
    document.getElementById('btn-retry-test').style.display = 'inline-block';
}

async function saveCamera() {
    const id = document.getElementById('camera-id').value;
    const payload = cameraPayload();
    const data = await cameraRequest(id ? `/api/cameras/${id}` : '/api/cameras', {
        method: id ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify(payload)
    });
    if (data == null) return null;
    const savedId = data.id || id;
    if (!savedId) throw new Error('Camera save could not be confirmed. Refresh the coverage board before trying again.');
    // Keep the ID before testing: a failed test must never turn a retry into POST.
    document.getElementById('camera-id').value = savedId;
    const existing = cameras.find(camera => Number(camera.id) === Number(savedId));
    const { ip, username, password, manual_rtsp_url, network_url, source, rtsp_port, rtsp_path, ...metadata } = payload;
    if (existing) Object.assign(existing, metadata);
    else cameras.push({ ...metadata, id: savedId, is_active: true });
    clearConnectionFields(true);
    editorSnapshot = editorValues();
    document.getElementById('camera-save-status').textContent = 'Camera settings saved. No camera test was started.';
    document.getElementById('camera-editor-title').textContent = `Editing: ${payload.name}`;
    return savedId;
}

async function runCameraTest(cameraId) {
    clearTimeout(testPreviewTimer);
    testPreviewStartedAt = null;
    clearCameraTestDiagnostic();
    document.getElementById('test-loading').style.display = 'block';
    document.getElementById('test-result').style.display = 'none';
    document.getElementById('test-status').textContent = '';
    document.getElementById('test-error').style.display = 'none';
    document.getElementById('btn-finish').style.display = 'inline-block';
    document.getElementById('btn-retry-test').style.display = 'none';
    document.getElementById('test-frame').removeAttribute('src');
    try {
        const data = await cameraRequest(`/api/cameras/${cameraId}/test`, { method: 'POST', headers: { 'X-CSRFToken': csrfToken } });
        if (data == null) return;
        if (data.last_test_status !== 'ok' || typeof data.frame !== 'string' || !data.frame.trim()) {
            throw new Error('Camera test result could not be read. The test outcome is unknown. Refresh saved camera status before retrying.');
        }
        document.getElementById('test-result').style.display = 'block';
        document.getElementById('test-status').textContent = `Fresh-frame test passed${data.tested_at ? ` at ${data.tested_at}` : ''}. This is a saved test result, not a running attendance session.`;
        testPreviewStartedAt = performance.now();
        document.getElementById('test-frame').src = `/api/cameras/${cameraId}/preview?t=${Date.now()}`;
        testPreviewTimer = setTimeout(() => {
            document.getElementById('test-frame').removeAttribute('src');
            clearCameraTestDiagnostic();
            document.getElementById('test-error').style.display = 'block';
            document.getElementById('test-error-title').textContent = 'Preview ended';
            document.getElementById('test-error-msg').textContent = 'The separate 30-second preview has ended. The passed test remains saved; run a new test only when you need a fresh check.';
            document.getElementById('btn-retry-test').style.display = 'inline-block';
        }, 30000);
        document.getElementById('btn-finish').style.display = 'inline-block';
    } catch (error) {
        showCameraTestError(error);
    } finally {
        document.getElementById('test-loading').style.display = 'none';
        // A camera is already saved even when testing failed. Refresh the board
        // separately so a coverage request cannot hide the test's result.
        await refreshCoverage();
    }
}

document.getElementById('btn-next-to-2').addEventListener('click', () => {
    if (cameraControlsBusy()) return;
    if (!roomSelect.value) {
        AppModal.alert('Select an existing institutional Room.');
        return;
    }
    showStep(2);
});
document.getElementById('btn-back-to-1').addEventListener('click', () => { if (!cameraControlsBusy()) showStep(1); });
document.getElementById('btn-back-to-2').addEventListener('click', () => { if (!cameraControlsBusy()) showStep(2); });
async function submitCameraForm(event) {
    // The submit button also uses this handler so invalid fields inside closed
    // details can be revealed before the browser reports their validity.
    event.preventDefault();
    if (cameraControlsBusy() || cameraWorkspaceSection !== 'configuration') return;
    if ((!document.getElementById('camera-id').value && !roomSelect.value) || !cameraForm.checkValidity()) {
        document.getElementById('camera-manual-settings').open = true;
        document.getElementById('camera-performance-settings').open = true;
        if (document.getElementById('camera-manual-rtsp').value || connectionKind.value === 'stream') document.getElementById('camera-url-fields').open = true;
        cameraForm.reportValidity();
        return;
    }
    if (connectionKind.value === 'rtsp' && !document.getElementById('camera-ip').value.trim() && !document.getElementById('camera-manual-rtsp').value.trim()) {
        const guidance = document.getElementById('camera-selection-status');
        guidance.textContent = 'Select Find cameras and choose your camera, or enter its address in Connection details / manual setup.';
        document.getElementById('btn-discover-cameras').focus();
        return;
    }
    setWizardBusy(true);
    try {
        const id = await saveCamera();
        if (id == null) return;
        managedCameraId = Number(id);
        await refreshCoverage();
        syncEditorControls();
    } catch (error) {
        document.getElementById('camera-save-status').textContent = `${error.message} Saving is not confirmed. Refresh saved status and review inventory before retrying.`;
        AppModal.alert(error.message);
    } finally {
        setWizardBusy(false);
    }
}
document.getElementById('btn-next-to-3').addEventListener('click', submitCameraForm);
cameraForm.addEventListener('submit', submitCameraForm);
document.getElementById('btn-retry-test').addEventListener('click', async () => {
    const id = document.getElementById('camera-id').value;
    if (!id || cameraControlsBusy()) return;
    setWizardBusy(true);
    try { await runCameraTest(id); } finally { setWizardBusy(false); }
});
function editorValues() {
    return JSON.stringify([roomSelect.value, ...['camera-id', 'camera-name', 'camera-width', 'camera-height', 'camera-fps', 'camera-match-threshold', 'camera-connection-kind', ...connectionFields].map(id => document.getElementById(id).value)]);
}
function isDirty() { return editorSnapshot !== null && editorValues() !== editorSnapshot; }
function cameraControlsBusy() { return wizardBusy || cameraActionsPending.size > 0 || savedDataLoading; }
function syncCameraNavigation() {
    const editorOpen = !document.getElementById('camera-editor').hidden;
    const busy = cameraControlsBusy();
    for (const [id, view] of [['show-camera-inventory', 'inventory'], ['show-room-coverage', 'coverage']]) {
        const button = document.getElementById(id);
        button.setAttribute('aria-pressed', String(!editorOpen && managementView === view));
        button.disabled = busy;
    }
    const addButton = document.getElementById('btn-add-camera');
    addButton.disabled = busy || (editorOpen && managedCameraId === null);
    addButton.title = editorOpen && managedCameraId === null ? 'Finish or cancel the new camera setup below before adding another camera.' : 'Create a new camera configuration';
    for (const [id, label] of [['btn-refresh-inventory', 'Refresh saved status'], ['camera-availability-refresh', 'Refresh saved availability'], ['camera-removal-retry', 'Refresh and review again']]) {
        const button = document.getElementById(id);
        button.disabled = busy || (id === 'camera-removal-retry' && button.hidden);
        button.textContent = savedDataLoading ? 'Refreshing…' : label;
        button.setAttribute('aria-busy', String(savedDataLoading));
    }
    for (const id of ['btn-cancel-camera', 'btn-finish', 'btn-next-to-3', 'btn-back-to-1', 'btn-back-to-2', 'btn-retry-test']) document.getElementById(id).disabled = busy;
    document.getElementById('btn-cancel-camera').textContent = managementView === 'inventory' ? 'Back to camera inventory' : 'Back to room coverage';
    document.getElementById('btn-test-saved-camera').disabled = busy || !document.getElementById('camera-id').value || isDirty();
    document.getElementById('btn-next-to-2').disabled = busy || !roomSelect.value;
    document.getElementById('camera-removal-confirm').disabled = busy || !cameraRemoval?.review?.can_delete;
    removalRetireButton.disabled = busy || isDirty() || cameraAvailabilityNeedsRefresh || !cameraRemoval?.retirementAllowed;
}
function discardThen(action) {
    if (cameraControlsBusy()) return;
    if (isDirty()) return new Promise(resolve => AppModal.confirm('Discard unsaved camera changes?', () => { editorSnapshot = null; resolve(action()); }, () => resolve(), 'Unsaved changes'));
    return action();
}
function syncEditorControls() {
    document.getElementById('btn-test-saved-camera').disabled = wizardBusy || !document.getElementById('camera-id').value || isDirty();
    document.getElementById('btn-next-to-2').disabled = wizardBusy || !roomSelect.value;
    syncCameraWorkspace();
}
function syncCameraWorkspace() {
    syncCameraNavigation();
    const camera = cameras.find(item => Number(item.id) === managedCameraId);
    const editing = managedCameraId !== null;
    document.getElementById('camera-workspace-navigation').hidden = !editing;
    document.getElementById('camera-workspace-context').textContent = camera
        ? `${[camera.building, camera.floor, camera.room_code, camera.room_name].filter(Boolean).join(' / ') || 'Unassigned room'} · ${cameraState(camera)}${Number(camera.is_default) ? ' · Default camera' : ''}`
        : editing ? 'This camera is no longer in the saved inventory. Return to the list and refresh saved status.' : '';
    if (camera) document.getElementById('camera-editor-title').textContent = `Manage camera: ${camera.name}`;
    document.getElementById('camera-workspace-unsaved').textContent = isDirty()
        ? 'You have unsaved configuration changes. Switching sections keeps them. Save in Configuration, or return to the list and discard them, before changing availability or removal.' : '';
    const locked = !camera || cameraControlsBusy() || isDirty();
    ['camera-workspace-availability', 'camera-workspace-default', 'camera-workspace-review-removal'].forEach(id => {document.getElementById(id).disabled = locked || cameraAvailabilityNeedsRefresh;});
    ['configuration', 'availability', 'removal'].forEach(section => {document.getElementById(`camera-section-${section}`).disabled = cameraControlsBusy();});
    document.getElementById('camera-workspace-default').hidden = !camera || Boolean(Number(camera.is_default)) || cameraState(camera) !== 'Ready';
    document.getElementById('camera-availability-current').textContent = camera ? `Saved availability: ${cameraState(camera)}${Number(camera.is_default) ? '. This is the default camera.' : '.'}` : 'Camera unavailable.';
    document.getElementById('camera-availability-apply').disabled = locked || cameraAvailabilityNeedsRefresh || !pendingCameraAvailability;
    document.getElementById('camera-availability-cancel').disabled = cameraControlsBusy();
}
function showCameraSection(section, focus = true, force = false) {
    if (!['configuration', 'availability', 'removal'].includes(section) || cameraControlsBusy()) return;
    if (section !== 'configuration' && managedCameraId === null) return;
    if (!force && cameraWorkspaceSection === section) return;
    if (cameraRemoval) closeCameraRemoval();
    pendingCameraAvailability = null;
    document.getElementById('camera-availability-confirmation').hidden = true;
    document.getElementById('camera-workspace-availability').value = '';
    cameraWorkspaceSection = section;
    ['configuration', 'availability', 'removal'].forEach(item => {
        document.getElementById(`camera-${item}-section`).hidden = item !== section;
        document.getElementById(`camera-section-${item}`).setAttribute('aria-pressed', String(item === section));
    });
    if (section !== 'configuration') {
        clearTimeout(testPreviewTimer);
        document.getElementById('test-frame').removeAttribute('src');
    } else if (managedCameraId !== null) {
        showStep(2);
    }
    syncCameraWorkspace();
    if (focus) document.getElementById({configuration:'camera-name', availability:'camera-availability-title', removal:'camera-removal-section-title'}[section]).focus({preventScroll:true});
}
['configuration', 'availability', 'removal'].forEach(section => document.getElementById(`camera-section-${section}`).addEventListener('click', () => showCameraSection(section)));
function showManagement(view = managementView) {
    if (document.getElementById('camera-editor').hidden && managementView === view) return;
    if (cameraRemoval) closeCameraRemoval();
    pendingCameraAvailability = null;
    managedCameraId = null;
    managementView = view;
    showStep(1, false); // closes the separate preview without focusing a hidden field
    document.getElementById('camera-editor').hidden = true;
    document.getElementById('camera-inventory').hidden = view !== 'inventory';
    document.getElementById('camera-room-board').hidden = view !== 'coverage';
    document.getElementById('show-camera-inventory').setAttribute('aria-pressed', String(view === 'inventory'));
    document.getElementById('show-room-coverage').setAttribute('aria-pressed', String(view === 'coverage'));
    editorSnapshot = null;
    clearConnectionFields();
    syncCameraNavigation();
    document.getElementById(view === 'inventory' ? 'camera-inventory-title' : 'room-filter-search').focus();
}
function openEditor() {
    document.getElementById('camera-editor').hidden = false;
    document.getElementById('camera-inventory').hidden = true;
    document.getElementById('camera-room-board').hidden = true;
    document.getElementById('camera-save-status').textContent = '';
    syncCameraNavigation();
}
function newCamera() {
    if (!document.getElementById('camera-editor').hidden && managedCameraId === null) return;
    discardThen(() => {
        managedCameraId = null;
        cameraForm.reset();
        document.getElementById('camera-id').value = '';
        roomSelect.value = '';
        clearConnectionFields();
        document.getElementById('camera-editor-title').textContent = 'Set up camera';
        document.getElementById('camera-match-threshold-val').textContent = document.getElementById('camera-match-threshold').value;
        openEditor();
        showCameraSection('configuration', false, true);
        showStep(1);
        editorSnapshot = editorValues();
        syncEditorControls();
    });
}
document.getElementById('btn-add-camera').addEventListener('click', newCamera);
document.getElementById('btn-cancel-camera').addEventListener('click', () => discardThen(() => showManagement()));
document.getElementById('btn-finish').addEventListener('click', () => discardThen(() => showManagement()));
document.getElementById('show-camera-inventory').addEventListener('click', () => discardThen(() => showManagement('inventory')));
document.getElementById('show-room-coverage').addEventListener('click', () => discardThen(() => showManagement('coverage')));
document.getElementById('btn-test-saved-camera').addEventListener('click', async () => {
    const id = document.getElementById('camera-id').value;
    if (!id || cameraControlsBusy() || isDirty()) return;
    setWizardBusy(true);
    showStep(3);
    try { await runCameraTest(id); } finally { setWizardBusy(false); }
});
cameraForm.addEventListener('input', syncEditorControls);
cameraForm.addEventListener('change', syncEditorControls);
roomSelect.addEventListener('change', syncEditorControls);
window.addEventListener('beforeunload', event => {
    if (isDirty() || wizardBusy) { event.preventDefault(); event.returnValue = ''; }
});

document.getElementById('test-frame').addEventListener('error', () => {
    if (!document.getElementById('test-frame').getAttribute('src')) return;
    clearTimeout(testPreviewTimer);
    document.getElementById('test-frame').removeAttribute('src');
    clearCameraTestDiagnostic();
    document.getElementById('test-error').style.display = 'block';
    document.getElementById('test-error-title').textContent = testPreviewStartedAt !== null && performance.now() - testPreviewStartedAt >= 30000 ? 'Preview ended' : 'Preview unavailable';
    document.getElementById('test-error-msg').textContent = testPreviewStartedAt !== null && performance.now() - testPreviewStartedAt >= 30000
        ? 'The separate 30-second preview has ended. The passed test remains saved.'
        : 'The fresh-frame test passed, but the separate preview is unavailable. Its viewing connection or capacity may need attention; this does not identify a camera fault. The test result remains saved.';
    document.getElementById('btn-retry-test').style.display = 'inline-block';
});

const capacityForm = document.getElementById('camera-capacity-form');
if (capacityForm) {
    const notice = document.getElementById('camera-capacity-message');
    const showLimits = data => {
        for (const key of ['recognition', 'viewers', 'secondary']) capacityForm.elements[key].value = data.limits[key];
        notice.textContent = data.message;
    };
    cameraRequest('/api/cameras/capacity-limits').then(data => { if (data) showLimits(data); }).catch(error => { notice.textContent = error.message; });
    capacityForm.addEventListener('submit', async event => {
        event.preventDefault();
        if (!capacityForm.reportValidity()) return;
        const button = capacityForm.querySelector('button');
        button.disabled = true;
        try {
            const body = Object.fromEntries(['recognition', 'viewers', 'secondary'].map(key => [key, Number(capacityForm.elements[key].value)]));
            const data = await cameraRequest('/api/cameras/capacity-limits', {method: 'POST', headers: {'Content-Type':'application/json', 'X-CSRFToken':csrfToken}, body:JSON.stringify(body)});
            if (data) showLimits(data);
        } catch (error) { notice.textContent = `${error.message} Saving is not confirmed; reload the saved limits before trying again.`; }
        finally { button.disabled = false; }
    });
}
document.getElementById('camera-match-threshold').addEventListener('input', event => {
    document.getElementById('camera-match-threshold-val').textContent = event.target.value;
});

async function cameraAction(id, action, desiredActive) {
    const key = String(id);
    if (cameraControlsBusy()) return;
    cameraActionsPending.add(key);
    renderCoverage();
    let refresh = action === 'test';
    try {
        const data = await cameraRequest(`/api/cameras/${id}/${action}`, {
            method: 'POST', headers: { 'X-CSRFToken': csrfToken, 'Content-Type': 'application/json' },
            ...(action === 'toggle' ? {body: JSON.stringify({is_active: desiredActive})} : {})
        });
        refresh = refresh || data != null;
    } finally {
        // Failed tests still persist their latest health status on the server.
        if (refresh) await refreshCoverage();
        cameraActionsPending.delete(key);
        renderCoverage();
    }
}

window.editCamera = function editCamera(id) {
    if (cameraControlsBusy()) return;
    const camera = cameras.find(item => Number(item.id) === Number(id));
    if (!camera) return;
    return discardThen(() => {
    managedCameraId = Number(id);
    document.getElementById('camera-availability-message').textContent = '';
    openEditor();
    document.getElementById('camera-editor-title').textContent = `Editing: ${camera.name} (${cameraState(camera)})`;
    document.getElementById('camera-id').value = camera.id;
    refreshRoomChoices(camera.room_id || '');
    roomSelect.value = camera.room_id || '';
    document.getElementById('camera-name').value = camera.name;
    clearConnectionFields(true);
    document.getElementById('camera-width').value = camera.resolution_width;
    document.getElementById('camera-height').value = camera.resolution_height;
    document.getElementById('camera-fps').value = camera.target_fps;
    document.getElementById('camera-match-threshold').value = camera.match_threshold;
    document.getElementById('camera-match-threshold-val').textContent = camera.match_threshold;
    showStep(2);
    editorSnapshot = editorValues();
    showCameraSection('configuration', false, true);
    syncEditorControls();
    });
};
window.testCamera = id => {
    if (cameraControlsBusy()) return;
    return discardThen(async () => {
        window.editCamera(id);
        setWizardBusy(true); showStep(3);
        try { await runCameraTest(id); } finally { setWizardBusy(false); }
    });
};
window.toggleCamera = (id, desiredActive) => {
    const camera = cameras.find(item => Number(item.id) === Number(id));
    if (!camera) return;
    const target = typeof desiredActive === 'boolean' ? desiredActive : !Boolean(Number(camera.is_active));
    return cameraAction(id, 'toggle', target).catch(error => AppModal.alert(error.message));
};

function healthState(room) {
    return room.health_state || 'No Camera';
}

function matchesCategory(room) {
    const health = healthState(room);
    switch (roomCategory) {
        case 'needs_setup': return health !== 'Healthy';
        case 'no_camera': return !Number(room.camera_count);
        case 'failed_maintenance': return health === 'Failed' || health === 'Maintenance' || health === 'Retired';
        case 'stale': return health === 'Stale';
        case 'not_tested': return health === 'Not Tested';
        case 'ready': return health === 'Healthy';
        default: return true;
    }
}

function selectRoomCategory(category) {
    roomCategory = category;
    document.querySelectorAll('[data-room-category]').forEach(button => {
        const selected = button.dataset.roomCategory === category;
        button.classList.toggle('is-active', selected);
        button.setAttribute('aria-pressed', String(selected));
    });
}

function filteredCoverage() {
    const search = document.getElementById('room-filter-search').value.trim().toLowerCase();
    const building = document.getElementById('room-filter-building').value;
    const floor = document.getElementById('room-filter-floor').value;
    const type = document.getElementById('room-filter-type').value;
    const health = document.getElementById('room-filter-health').value;
    const cameraFilter = document.getElementById('room-filter-coverage').value;
    const assignment = document.getElementById('room-filter-assignment').value;
    return coverage.filter(room => {
        if (!matchesCategory(room)) return false;
        const haystack = [room.room_code, room.room_name, room.building, room.floor, ...cameras.filter(camera => Number(camera.room_id) === Number(room.id)).map(camera => camera.name)].join(' ').toLowerCase();
        if (search && !haystack.includes(search)) return false;
        if (building && room.building !== building) return false;
        if (floor && room.floor !== floor) return false;
        if (type && room.room_type !== type) return false;
        if (health && healthState(room) !== health) return false;
        if (cameraFilter === 'has_camera' && !Number(room.camera_count)) return false;
        if (cameraFilter === 'no_camera' && Number(room.camera_count)) return false;
        if (cameraFilter === 'inactive_camera' && !cameras.some(camera => Number(camera.room_id) === Number(room.id) && !Number(camera.is_active))) return false;
        if (assignment === 'has_classes' && !room.assigned_class_count) return false;
        if (assignment === 'no_classes' && room.assigned_class_count) return false;
        return true;
    });
}

function renderRoom(room) {
    const roomCameras = cameras.filter(camera => Number(camera.room_id) === Number(room.id));
    const activeCamera = roomCameras.find(camera => Number(camera.is_active) && camera.lifecycle_status === 'Ready');
    const cards = roomCameras.filter(camera => Number(camera.is_active) && camera.lifecycle_status !== 'Retired').map(camera => {
        const id = Number(camera.id);
        if (!Number.isSafeInteger(id)) return '';
        const disabled = cameraControlsBusy() ? ' disabled aria-busy="true"' : '';
        return `<div class="camera-room-section__title"><div><strong>${cameraEscape(camera.name)}</strong> <span>${cameraEscape(cameraState(camera))}</span></div><button type="button" class="btn btn--secondary btn--sm" onclick="editCamera(${id})"${disabled}>Manage camera</button></div>`;
    }).join('');
    return `<section class="camera-room-section" aria-labelledby="coverage-room-${Number(room.id)}"><div class="camera-room-section__title"><div><h4 id="coverage-room-${Number(room.id)}">${cameraEscape(room.hierarchy_display || room.display_name)}</h4><span>${cameraEscape(room.room_type)} · ${cameraEscape(healthState(room) === 'Healthy' ? 'Recent pass at refresh' : healthState(room))}</span><p class="form-hint">${cameraEscape(room.health_detail || 'Saved check details unavailable.')}</p></div>${!activeCamera ? '<span class="text-muted">No active camera; Manual Mode remains available.</span>' : ''}</div>${cards}</section>`;
}

function renderCoverage() {
    syncCameraWorkspace();
    renderInventory();
    if (!savedDataLoaded) {
        document.getElementById('room-filter-count').textContent = savedDataLoading ? 'Loading rooms…' : 'Room count unavailable';
        document.getElementById('camera-room-groups').textContent = savedDataLoading ? 'Loading saved room coverage…' : 'Room coverage has not loaded. Use Refresh saved status to retry.';
        return;
    }
    const matches = filteredCoverage();
    coveragePage = Math.min(coveragePage, Math.max(1, Math.ceil(matches.length / pageSize)));
    const visible = matches.slice((coveragePage - 1) * pageSize, coveragePage * pageSize);
    document.getElementById('room-filter-count').textContent = `${matches.length} ${matches.length === 1 ? 'room' : 'rooms'} shown`;
    const target = document.getElementById('camera-room-groups');
    if (!visible.length) {
        target.innerHTML = `<div class="camera-empty-state">${coverage.length ? 'No rooms match the selected filters.' : 'No active rooms are available. Ask the Dean to add or activate rooms in Locations, then refresh saved status.'}</div>`;
        return;
    }
    const paging = `<div class="camera-inventory-pagination"><button type="button" class="btn btn--secondary" onclick="changeCoveragePage(-1)" ${coveragePage === 1 ? 'disabled' : ''}>Previous rooms</button><span>Page ${coveragePage} of ${Math.max(1, Math.ceil(matches.length / pageSize))}</span><button type="button" class="btn btn--secondary" onclick="changeCoveragePage(1)" ${coveragePage * pageSize >= matches.length ? 'disabled' : ''}>Next rooms</button></div>`;
    const groupBy = document.getElementById('room-group-by').value;
    const labels = { building: 'Building', floor: 'Floor', room_type: 'Room Type' };
    if (!labels[groupBy]) {
        target.innerHTML = visible.map(renderRoom).join('') + paging;
        return;
    }
    const groups = new Map();
    visible.forEach(room => {
        // Floor names repeat across buildings; keep each building/floor separate.
        const label = groupBy === 'floor'
            ? `${room.building || 'Unspecified building'} / ${room.floor || 'Unspecified floor'}`
            : room[groupBy] || 'Unspecified';
        if (!groups.has(label)) groups.set(label, []);
        groups.get(label).push(room);
    });
    target.innerHTML = [...groups.entries()].sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true })).map(([label, groupedRooms], index) => `<section class="camera-room-group" aria-labelledby="coverage-group-${index}"><div class="camera-room-group__title"><h4 id="coverage-group-${index}">${labels[groupBy]}: ${cameraEscape(label)}</h4><span>${groupedRooms.length} rooms</span></div>${groupedRooms.map(renderRoom).join('')}</section>`).join('') + paging;
}

function cameraState(camera) {
    if (camera.lifecycle_status === 'Retired') return 'Retired';
    if (camera.lifecycle_status && camera.lifecycle_status !== 'Ready') return camera.lifecycle_status;
    return Number(camera.is_active) ? 'Ready' : 'Inactive';
}
function refreshRoomChoices(selected = roomSelect.value) {
    const current = String(selected || '');
    const saved = cameras.find(camera => String(camera.room_id || '') === current);
    roomSelect.innerHTML = '<option value="">Select room…</option>' + rooms.map(room => `<option value="${Number(room.id)}">${cameraEscape(room.hierarchy_display || room.display_name || room.room_code)}</option>`).join('');
    if (current && !rooms.some(room => String(room.id) === current)) {
        roomSelect.innerHTML += `<option value="${cameraEscape(current)}">${cameraEscape(saved?.room_code || 'Previously selected room')} (inactive / unavailable)</option>`;
    }
    roomSelect.value = current;
    syncEditorControls();
}
function inventoryMatches() {
    const view = document.getElementById('camera-inventory-view').value || 'current';
    const query = document.getElementById('camera-inventory-search').value.trim().toLowerCase();
    return cameras.filter(camera => {
        const historical = !Number(camera.is_active) || camera.lifecycle_status === 'Retired';
        if (view === 'current' && historical || view === 'inactive' && !historical) return false;
        return !query || [camera.name, camera.room_code, camera.room_name, camera.building, camera.floor,
            camera.room_id ? '' : 'Unassigned'].join(' ').toLowerCase().includes(query);
    });
}
function renderInventory() {
    if (!savedDataLoaded) {
        document.getElementById('camera-inventory-count').textContent = savedDataLoading ? 'Loading camera inventory…' : 'Camera inventory has not loaded. Use Refresh saved status to retry.';
        document.getElementById('camera-inventory-list').textContent = savedDataLoading ? 'Loading saved cameras…' : 'Saved cameras are unavailable until the connection is restored.';
        document.getElementById('camera-page-label').textContent = '';
        document.getElementById('camera-page-prev').disabled = true;
        document.getElementById('camera-page-next').disabled = true;
        return;
    }
    const matches = inventoryMatches();
    const pages = Math.max(1, Math.ceil(matches.length / pageSize));
    inventoryPage = Math.min(Math.max(1, inventoryPage), pages);
    const visible = matches.slice((inventoryPage - 1) * pageSize, inventoryPage * pageSize);
    const inactive = cameras.filter(camera => !Number(camera.is_active) || camera.lifecycle_status === 'Retired').length;
    document.getElementById('camera-inventory-count').textContent = `${matches.length} matching cameras · ${cameras.length - inactive} current · ${inactive} inactive / retired · ${cameras.length} total`;
    document.getElementById('camera-page-label').textContent = `Page ${inventoryPage} of ${pages} · up to ${pageSize} cameras per page`;
    document.getElementById('camera-page-prev').disabled = inventoryPage <= 1;
    document.getElementById('camera-page-next').disabled = inventoryPage >= pages;
    document.getElementById('camera-inventory-list').innerHTML = visible.length ? visible.map(camera => {
        const id = Number(camera.id);
        if (!Number.isSafeInteger(id)) return '';
        const state = cameraState(camera);
        const disabled = cameraControlsBusy() ? ' disabled' : '';
        const location = camera.room_id ? [camera.building, camera.floor, camera.room_code, camera.room_name].filter(Boolean).join(' / ') : 'Unassigned';
        const availability = state === 'Ready' && camera.room_id && camera.location_active === 0 ? 'Unavailable location' : state;
        return `<article class="camera-inventory-card" aria-labelledby="camera-title-${id}">
            <h4 id="camera-title-${id}">${cameraEscape(camera.name)}</h4>
            <p>${cameraEscape(availability)}${Number(camera.is_default) ? ' · Default camera' : ''} · ${cameraEscape(location)}${camera.room_id && !Number(camera.location_active) ? ' · Inactive / unavailable location' : ''}</p>
            <p class="form-hint">${cameraEscape(camera.health_detail || 'No saved check details. Testing is a separate action.')}</p>
            <div class="camera-room-section__actions">
                <button type="button" id="camera-manage-${id}" class="btn btn--secondary btn--sm" onclick="editCamera(${id})"${disabled}>Manage camera</button>
            </div></article>`;
    }).join('') : `<div class="camera-empty-state">${cameras.length ? 'No cameras match this view. Clear the search or choose All cameras to include inactive and retired devices.' : 'No cameras have been saved. Use Add camera after the Dean creates an active room in Locations. Manual attendance remains available.'}</div>`;
}
async function mutateCamera(id, suffix, body, method = 'POST') {
    if (cameraControlsBusy()) return;
    cameraActionsPending.add(String(id));
    renderCoverage();
    let message = '';
    try {
        const data = await cameraRequest(`/api/cameras/${id}${suffix}`, {method,
            headers: {'Content-Type':'application/json', 'X-CSRFToken':csrfToken}, body: JSON.stringify(body)});
        if (data) message = data.message;
    } catch (error) {
        message = `${error.message} Refresh saved status before retrying if the outcome is uncertain.`;
        AppModal.alert(message);
    } finally {
        await refreshCoverage();
        cameraActionsPending.delete(String(id));
        renderCoverage();
        document.getElementById('camera-management-message').textContent = message;
    }
}
async function prepareCameraAvailability(id, state, makeDefault = false, returnToRemoval = false) {
    id = Number(id);
    if (cameraControlsBusy() || (!makeDefault && !['Ready','Inactive','Maintenance','Retired'].includes(state))) return;
    if (managedCameraId !== id || document.getElementById('camera-editor').hidden) await window.editCamera(id);
    const camera = cameras.find(item => Number(item.id) === id);
    if (!camera || managedCameraId !== id || isDirty() || cameraAvailabilityNeedsRefresh) return;
    showCameraSection('availability', false);
    pendingCameraAvailability = {id, state, makeDefault, name:camera.name, returnToRemoval};
    if (returnToRemoval) document.getElementById('camera-workspace-availability').value = state;
    document.getElementById('camera-availability-impact').textContent = makeDefault
        ? `Use “${camera.name}” as the default camera? The previous default stays in inventory with its history. Current use and room availability will be checked again.`
        : returnToRemoval ? `Retire “${camera.name}”? This removes it from active use and keeps its saved assignments and attendance history under Inactive / Retired. Current use and default-camera rules will be checked again. Afterwards, a fresh review will show whether permanent deletion is allowed; retiring does not delete the camera.`
        : `Change “${camera.name}” from ${cameraState(camera)} to ${state}? ${state === 'Ready' ? 'This allows new camera use; run a separate camera test to check its picture.' : 'This prevents new attendance and registration use. Saved attendance history stays available.'} Current use and default-camera rules will be checked again.`;
    document.getElementById('camera-availability-apply').textContent = returnToRemoval ? 'Retire camera' : 'Apply this change';
    document.getElementById('camera-availability-confirmation').hidden = false;
    document.getElementById('camera-availability-message').textContent = '';
    syncCameraWorkspace();
    document.getElementById('camera-availability-cancel').focus({preventScroll:true});
    document.getElementById('camera-availability-confirmation').scrollIntoView({block:'center', behavior:'instant'});
}
window.changeCameraAvailability = (id, state) => prepareCameraAvailability(id, state);
window.replaceDefaultCamera = id => prepareCameraAvailability(id, null, true);
document.getElementById('camera-workspace-availability').addEventListener('change', event => prepareCameraAvailability(managedCameraId, event.target.value));
document.getElementById('camera-workspace-default').addEventListener('click', () => prepareCameraAvailability(managedCameraId, null, true));
document.getElementById('camera-availability-cancel').addEventListener('click', async () => {
    if (cameraControlsBusy()) return;
    const change = pendingCameraAvailability;
    pendingCameraAvailability = null;
    document.getElementById('camera-availability-confirmation').hidden = true;
    document.getElementById('camera-workspace-availability').value = '';
    syncCameraWorkspace();
    if (change?.returnToRemoval && change.id === managedCameraId) return window.reviewCameraRemoval(change.id);
    document.getElementById('camera-workspace-availability').focus({preventScroll:true});
});
document.getElementById('camera-availability-refresh').addEventListener('click', async () => {
    if (cameraControlsBusy()) return;
    pendingCameraAvailability = null;
    document.getElementById('camera-availability-confirmation').hidden = true;
    document.getElementById('camera-workspace-availability').value = '';
    const refreshed = await refreshCoverage();
    document.getElementById('camera-availability-message').textContent = refreshed ? 'Saved availability refreshed. Review the current state before choosing another change.' : 'Saved availability could not be refreshed. The result of an earlier change may still be unknown. Restore the connection to CSD FARS and refresh again.';
});
document.getElementById('camera-availability-apply').addEventListener('click', async () => {
    const change = pendingCameraAvailability;
    if (!change || change.id !== managedCameraId || isDirty() || cameraAvailabilityNeedsRefresh || cameraControlsBusy()) return;
    pendingCameraAvailability = null;
    document.getElementById('camera-availability-confirmation').hidden = true;
    cameraActionsPending.add(String(change.id)); renderCoverage();
    document.getElementById('camera-availability-message').textContent = 'Saving this camera change. Please wait for the result.';
    let message = '', confirmed = false, refreshed = false;
    try {
        const {response, data} = await removalRequest(change.id, 'POST', null, change.makeDefault ? '/default' : '/availability', change.makeDefault ? {} : {state:change.state});
        if (response.ok) {
            const expectedLifecycle = change.state === 'Inactive' ? 'Ready' : change.state;
            confirmed = data?.saved === true && data.id === change.id && Number.isSafeInteger(data.id) && (change.makeDefault ? data.is_default === true :
                data.lifecycle_status === expectedLifecycle && data.is_active === (change.state === 'Ready' ? 1 : 0));
            message = confirmed ? `“${change.name}”: ${change.makeDefault ? 'default camera saved' : `${change.state} saved`}. Attendance history was preserved.` : 'The result is unknown because the server did not confirm this camera change. Refresh saved availability before trying again.';
        } else if (response.status === 401 || response.status === 403) {
            message = 'This change was not allowed. Sign in again or ask the Dean to check camera-management permission. Refresh saved availability after access is restored.';
            AuthResponse.redirectIfUnauthorized(response, data);
        } else {
            const guidance = {
                camera_busy:'This camera is still in use or its stop is unconfirmed. Ask the operator to finish previews or tests, or ask the owning teacher to finish attendance and wait for confirmed cleanup. Then refresh saved availability.',
                camera_default:'Replace this camera’s default role first. Open another eligible camera’s Availability section and choose Make default, then review this camera again.',
                camera_location:'An active room is required. Ask the Dean to check the room, floor and building in Locations, then review the camera’s Configuration.',
                camera_room_conflict:'Another active camera already serves this room. Review that camera in inventory before choosing which device should be available.',
                camera_default_unavailable:'The replacement default must be Ready in an available location. Review Configuration and Availability before trying again.',
                camera_not_found:'This camera is no longer available. Return to the list, refresh saved status and check All cameras.',
                camera_state_invalid:'The availability choice is no longer valid. Refresh saved availability and select a current choice.',
                camera_audit_failed:'The required change record could not be saved, so the change was rolled back. Ask the system administrator to check server storage and the change-record service, then refresh saved availability.'
            };
            message = guidance[data?.code] || 'The result of this change is unknown. Refresh saved availability before trying again. Ask the system administrator for help if this continues.';
        }
    } catch (_) { message = 'The browser lost contact with CSD FARS or the request timed out. The change may already have been saved. Restore the connection and refresh saved availability before trying again.'; }
    finally {
        cameraAvailabilityNeedsRefresh = true;
        if (confirmed) {
            refreshed = await refreshCoverage();
            if (!refreshed) message += ' The list could not be refreshed and may be out of date. Refresh saved availability before making another change.';
        }
        cameraActionsPending.delete(String(change.id)); renderCoverage();
        document.getElementById('camera-availability-message').textContent = message;
    }
    if (change.returnToRemoval && confirmed && refreshed && managedCameraId === change.id && cameraWorkspaceSection === 'availability') {
        document.getElementById('camera-save-status').textContent = message;
        await window.reviewCameraRemoval(change.id);
    }
});
function removalIsCurrent(state) {
    return cameraRemoval === state && state.generation === cameraRemovalGeneration;
}
function resetRemovalActions() {
    if (cameraRemoval) { cameraRemoval.review = null; cameraRemoval.retirementAllowed = false; }
    ['camera-removal-confirm', 'camera-removal-retry', 'camera-removal-retire'].forEach(id => {
        const button = document.getElementById(id);
        button.hidden = true;
        button.disabled = true;
    });
    document.getElementById('camera-removal-blockers').replaceChildren();
    document.getElementById('camera-removal-reference').hidden = true;
    document.getElementById('camera-removal-reference').textContent = '';
}
function removalMessage(title, message, code, retry = true) {
    resetRemovalActions();
    document.getElementById('camera-removal-title').textContent = title;
    document.getElementById('camera-removal-summary').textContent = message;
    if (code) {
        document.getElementById('camera-removal-reference').textContent = `Reference code: ${code}`;
        document.getElementById('camera-removal-reference').hidden = false;
    }
    document.getElementById('camera-removal-retry').hidden = !retry;
    document.getElementById('camera-removal-retry').disabled = !retry;
}
function closeCameraRemoval() {
    if (!cameraRemoval) return;
    const {id, deleting} = cameraRemoval;
    resetRemovalActions();
    cameraRemoval = null;
    cameraRemovalGeneration += 1;
    document.getElementById('camera-removal-panel').hidden = true;
    if (deleting) document.getElementById('camera-management-message').textContent = 'The deletion request may still be processing. Refresh saved status and review the camera again before retrying.';
    const trigger = managedCameraId === id ? document.getElementById('camera-workspace-review-removal') : document.getElementById(`camera-manage-${id}`);
    (trigger && !trigger.disabled ? trigger : document.getElementById('camera-inventory-title')).focus({preventScroll: true});
}
function cameraRemovalReview(value, id) {
    if (!value || typeof value !== 'object' || value.id !== id || !Number.isSafeInteger(value.id)) return null;
    if (typeof value.name !== 'string' || !value.name.trim() || value.name.length > 500 || typeof value.can_delete !== 'boolean') return null;
    if (!Array.isArray(value.blockers) || value.blockers.length > 20 || value.blockers.some(item => typeof item !== 'string' || !item.trim() || item.length > 1500)) return null;
    if (value.can_delete !== (value.blockers.length === 0)) return null;
    if (value.blocker_details != null && (!Array.isArray(value.blocker_details) || value.blocker_details.length !== value.blockers.length)) return null;
    const details = value.blocker_details || [];
    if (details.some(item => !item || !/^[a-z0-9_]{1,80}$/.test(item.code) ||
        ['title', 'reason'].some(key => typeof item[key] !== 'string' || !item[key].trim() || item[key].length > 1500) ||
        !Array.isArray(item.steps) || item.steps.length < 1 || item.steps.length > 10 || item.steps.some(step => typeof step !== 'string' || !step.trim() || step.length > 1500))) return null;
    return {id, name: value.name, can_delete: value.can_delete, blockers: value.blockers, details};
}
function showRemovalReview(state, review, changed = false) {
    resetRemovalActions();
    state.name = review.name;
    const camera = cameras.find(item => Number(item.id) === state.id);
    const retired = camera?.lifecycle_status === 'Retired';
    const recordCodes = ['class_reference', 'attendance_history', 'start_history', 'related_record'];
    const onlyRecords = review.details.length > 0 && review.details.length === review.blockers.length && review.details.every(item => recordCodes.includes(item.code));
    const hasClass = review.details.some(item => item.code === 'class_reference');
    state.retirementAllowed = Boolean(camera && !retired && !Number(camera.is_default) && (review.can_delete || onlyRecords));
    document.getElementById('camera-removal-name').textContent = `Camera: ${review.name}`;
    document.getElementById('camera-removal-title').textContent = review.can_delete ? 'Delete this unused camera?'
        : retired && onlyRecords ? 'Retired camera: saved records are retained'
        : state.retirementAllowed ? 'Retire this camera to remove it from active use' : 'This camera cannot be deleted';
    document.getElementById('camera-removal-summary').textContent = review.can_delete
        ? `No blocking references or current use were found. Permanent deletion removes this camera and its saved connection and cannot be undone. The server will check again when you confirm.${state.retirementAllowed ? ' You can choose Retire camera instead to keep its record.' : ''}`
        : `${changed ? 'The camera’s use changed after the review. No camera was deleted. ' : 'No deletion has been requested. '}${hasClass ? 'This camera is linked to a class through a saved assignment; this does not mean attendance is running. ' : ''}${retired && onlyRecords ? 'It is already retired and unavailable for new use. Permanent deletion remains blocked while these protected references exist.' : state.retirementAllowed ? 'Choose Retire camera below to remove it from active use while preserving its records. Permanent deletion remains blocked while protected references exist.' : 'Follow the blockers below. Resolve default-camera, current-use or unfinished-cleanup blockers before retiring. Protected record references must be kept; retirement does not erase them.'}`;
    const list = document.getElementById('camera-removal-blockers');
    review.blockers.forEach((reason, index) => {
        const item = document.createElement('li');
        const detail = review.details[index];
        if (detail) {
            const heading = document.createElement('strong'); heading.textContent = detail.title; item.appendChild(heading);
            const why = document.createElement('p');
            why.textContent = detail.code === 'class_reference' ? 'An active or inactive class has a saved assignment to this camera. That link may come from an earlier classroom setup. Retiring the camera preserves the assignment and attendance history.' : detail.reason;
            item.appendChild(why);
            const steps = document.createElement('ol');
            const guidance = recordCodes.includes(detail.code) && (retired || state.retirementAllowed)
                ? [retired ? 'This camera is already retired. Find its retained record under Inactive / Retired or All cameras.' : 'Choose Retire camera below, then confirm the named camera. No records are deleted by retirement.', 'Keep class and attendance records. Permanent deletion is available only when a new review finds no protected references or current use.']
                : detail.steps;
            guidance.forEach(step => { const entry = document.createElement('li'); entry.textContent = step; steps.appendChild(entry); });
            item.appendChild(steps);
        } else {
            item.textContent = `${reason} Keep the camera until this is resolved. Ask the Dean or the owning teacher if you cannot change the related item, then refresh and review again.`;
        }
        list.appendChild(item);
    });
    removalRetireButton.hidden = !state.retirementAllowed;
    if (review.can_delete) {
        state.review = review;
        document.getElementById('camera-removal-confirm').hidden = false;
        document.getElementById('camera-removal-confirm').disabled = false;
    } else {
        document.getElementById('camera-removal-retry').hidden = false;
        document.getElementById('camera-removal-retry').disabled = false;
    }
}
async function removalRequest(id, method = 'GET', name, suffix, payload) {
    const controller = new AbortController();
    let timer;
    try {
        return await Promise.race([
            (async () => {
                const response = await fetch(`/api/cameras/${id}${suffix || (method === 'GET' ? '/removal' : '')}`, {
                    method, signal: controller.signal,
                    ...(method !== 'GET' ? {headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken}, body: JSON.stringify(method === 'DELETE' ? {confirm_name: name} : payload)} : {})
                });
                const data = await AuthResponse.readJson(response);
                return {response, data};
            })(),
            new Promise((_, reject) => { timer = setTimeout(() => { controller.abort(); reject(new Error('removal_timeout')); }, 30000); })
        ]);
    } finally { clearTimeout(timer); }
}
function showRemovalResponseError(response, data, deleting) {
    const prefix = deleting ? 'No deletion was completed by this request. ' : 'No deletion has been requested. ';
    if (response.status === 401 || response.status === 403) {
        removalMessage('Sign-in or permission required', `${prefix}Your session or camera-management permission needs attention. Sign in again, or ask the Dean to check your access. Start a new removal review after access is restored.`, response.status === 401 ? 'sign_in_required' : 'permission_required', false);
        AuthResponse.redirectIfUnauthorized(response, data);
    } else if (response.status === 404 && data?.code === 'camera_not_found') {
        removalMessage('Camera is no longer available', `${deleting ? 'This request did not find the camera. ' : prefix}It may already have been removed by another operator. Refresh saved status and check All cameras; do not create a replacement just to retry deletion.`, 'camera_not_found');
    } else if (data?.code === 'camera_audit_failed') {
        removalMessage('Deletion could not be recorded', `${prefix}The required change record could not be saved, so the removal was rolled back. Ask the system administrator to check the server’s storage and change-record service, then refresh and review again.`, 'camera_audit_failed');
    } else if (['camera_confirmation_required', 'camera_name_changed'].includes(data?.code)) {
        removalMessage('Review the camera again', `${prefix}The camera name or confirmation no longer matches. Refresh and review the current camera before confirming removal.`, 'camera_confirmation_required');
    } else {
        removalMessage(deleting ? 'Deletion result is unknown' : 'Removal review unavailable', deleting
            ? 'The server did not confirm the deletion result. Refresh saved status and review this camera again before retrying. If it is absent from All cameras, it may already have been removed. Ask the system administrator for help if this continues.'
            : 'The server could not complete the review. No deletion has been requested. Refresh saved status and review again. Ask the system administrator for help if this continues.',
        deleting ? 'camera_delete_unconfirmed' : 'camera_removal_review_failed');
    }
}
window.reviewCameraRemoval = async id => {
    id = Number(id);
    if (!Number.isSafeInteger(id) || id < 1 || cameraControlsBusy()) return;
    if (managedCameraId !== id || document.getElementById('camera-editor').hidden) await window.editCamera(id);
    if (managedCameraId !== id || isDirty()) return;
    showCameraSection('removal', false);
    const camera = cameras.find(item => Number(item.id) === id);
    const state = {id, name: camera?.name || 'Selected camera', generation: ++cameraRemovalGeneration, review: null, deleting: false};
    cameraRemoval = state;
    resetRemovalActions();
    const panel = document.getElementById('camera-removal-panel');
    panel.hidden = false;
    document.getElementById('camera-removal-name').textContent = `Camera: ${state.name}`;
    document.getElementById('camera-removal-title').textContent = 'Review camera removal';
    document.getElementById('camera-removal-summary').textContent = 'Checking camera use and saved references. No deletion has been requested.';
    document.getElementById('camera-removal-cancel').focus({preventScroll: true});
    panel.scrollIntoView({block: 'nearest', behavior: 'instant'});
    cameraActionsPending.add(String(id)); renderCoverage();
    try {
        const {response, data} = await removalRequest(id);
        if (!removalIsCurrent(state)) return;
        if (!response.ok) return showRemovalResponseError(response, data, false);
        const review = cameraRemovalReview(data, id);
        if (review) showRemovalReview(state, review);
        else removalMessage('Removal review unavailable', 'The server returned an incomplete removal review. No deletion has been requested. Refresh saved status and review again.', 'camera_removal_review_invalid');
    } catch (_) {
        if (removalIsCurrent(state)) removalMessage('Removal review unavailable', 'The browser could not finish the removal review. No deletion has been requested. Check your connection to CSD FARS, then refresh and review again.', 'camera_removal_review_unreachable');
    } finally { cameraActionsPending.delete(String(id)); renderCoverage(); }
};
document.getElementById('camera-workspace-review-removal').addEventListener('click', () => window.reviewCameraRemoval(managedCameraId));
removalRetireButton.addEventListener('click', () => {
    const state = cameraRemoval;
    const camera = cameras.find(item => Number(item.id) === state?.id);
    if (!state?.retirementAllowed || state.id !== managedCameraId || !camera || camera.lifecycle_status === 'Retired' || Number(camera.is_default) || cameraControlsBusy() || isDirty() || cameraAvailabilityNeedsRefresh) return;
    return prepareCameraAvailability(state.id, 'Retired', false, true);
});
document.getElementById('camera-removal-cancel').addEventListener('click', closeCameraRemoval);
window.addEventListener('keydown', event => { if (event.key === 'Escape' && cameraRemoval) { event.preventDefault(); closeCameraRemoval(); } });
document.getElementById('camera-removal-retry').addEventListener('click', async () => {
    if (!cameraRemoval || cameraControlsBusy()) return;
    const state = cameraRemoval;
    resetRemovalActions();
    document.getElementById('camera-removal-summary').textContent = 'Refreshing saved information before a new removal review. No deletion is being requested.';
    await refreshCoverage();
    if (removalIsCurrent(state)) await window.reviewCameraRemoval(state.id);
});
document.getElementById('camera-removal-confirm').addEventListener('click', async () => {
    const state = cameraRemoval;
    if (!state?.review?.can_delete || cameraControlsBusy()) return;
    const review = state.review;
    resetRemovalActions(); state.deleting = true;
    document.getElementById('camera-removal-summary').textContent = 'Requesting permanent deletion. Current use and references are being checked again.';
    cameraActionsPending.add(String(state.id)); renderCoverage();
    try {
        const {response, data} = await removalRequest(state.id, 'DELETE', review.name);
        if (!removalIsCurrent(state)) return;
        if (!response.ok) {
            if (response.status === 409 && data?.code === 'camera_delete_blocked') {
                const blocked = cameraRemovalReview({...data, id: state.id, name: state.name, can_delete: false}, state.id);
                if (blocked) return showRemovalReview(state, blocked, true);
            }
            return showRemovalResponseError(response, data, true);
        }
        if (data?.deleted !== true || data.id !== state.id || !Number.isSafeInteger(data.id)) {
            return removalMessage('Deletion result is unknown', 'The server response did not confirm which camera was deleted. Refresh saved status and review this camera again before retrying. Check All cameras if it is no longer listed.', 'camera_delete_unconfirmed');
        }
        const refreshed = await refreshCoverage();
        if (removalIsCurrent(state)) removalMessage(data.already_absent === true ? 'Camera is already absent' : 'Camera deleted',
            (data.already_absent === true ? `“${review.name}” was already absent when the server checked. This request did not delete it again.` : `“${review.name}” and its saved connection were permanently removed.`) +
            (refreshed ? '' : ' The saved list could not be refreshed and may be out of date. Use Refresh saved status; do not repeat the deletion.'), null, false);
    } catch (_) {
        if (removalIsCurrent(state)) removalMessage('Deletion result is unknown', 'The browser lost contact with CSD FARS or the request timed out. The camera may already have been removed. Restore the connection, refresh saved status and review again before retrying. Check All cameras if it is no longer listed.', 'camera_delete_unconfirmed');
    } finally { state.deleting = false; cameraActionsPending.delete(String(state.id)); renderCoverage(); }
});
window.changeCoveragePage = delta => { coveragePage += delta; renderCoverage(); document.getElementById('room-filter-search').focus(); };
['camera-inventory-search', 'camera-inventory-view'].forEach(id => {
    document.getElementById(id).addEventListener('input', () => { inventoryPage = 1; renderInventory(); });
    document.getElementById(id).addEventListener('change', () => { inventoryPage = 1; renderInventory(); });
});
document.getElementById('camera-inventory-clear').addEventListener('click', () => {
    document.getElementById('camera-inventory-search').value = '';
    document.getElementById('camera-inventory-view').value = 'all';
    inventoryPage = 1; renderInventory();
});
document.getElementById('camera-page-prev').addEventListener('click', () => { inventoryPage -= 1; renderInventory(); document.getElementById('camera-inventory-title').focus(); });
document.getElementById('camera-page-next').addEventListener('click', () => { inventoryPage += 1; renderInventory(); document.getElementById('camera-inventory-title').focus(); });
async function refreshSavedInformation() {
    if (cameraControlsBusy()) return;
    if (cameraRemoval?.review || cameraRemoval?.retirementAllowed) removalMessage('Review removal again', 'Saved information is being refreshed. Review current camera use again before choosing retirement or confirming deletion.', null);
    return refreshCoverage();
}
document.getElementById('btn-refresh-inventory').addEventListener('click', refreshSavedInformation);

function setOptions(id, values, label) {
    const select = document.getElementById(id);
    const current = select.value;
    select.innerHTML = `<option value="">${label}</option>` + [...new Set(values.filter(Boolean))].sort().map(value => `<option value="${cameraEscape(value)}">${cameraEscape(value)}</option>`).join('');
    select.value = values.includes(current) ? current : '';
}

function loadData() {
    // One atomic metadata read at a time. Camera writes are blocked until it
    // settles; a post-write refresh can never reuse an older in-flight read.
    if (savedDataRequest) return savedDataRequest;
    savedDataLoading = true;
    const wasLoaded = savedDataLoaded;
    const notice = document.getElementById('camera-refresh-message');
    notice.textContent = wasLoaded ? 'Refreshing saved camera information…' : 'Loading saved camera information…';
    renderCoverage();
    savedDataRequest = (async () => {
        try {
            const [cameraData, roomData, coverageData] = await Promise.all([
                cameraRequest('/api/cameras'),
                cameraRequest('/api/rooms'),
                cameraRequest('/api/rooms/coverage')
            ]);
            if (cameraData == null || roomData == null || coverageData == null) return false;
            if (!Array.isArray(cameraData) || !Array.isArray(roomData) || !Array.isArray(coverageData)) throw new Error('Saved camera information is incomplete.');
            cameraAvailabilityNeedsRefresh = false;
            if (pendingCameraAvailability) {
                pendingCameraAvailability = null;
                document.getElementById('camera-availability-confirmation').hidden = true;
                document.getElementById('camera-workspace-availability').value = '';
                document.getElementById('camera-availability-message').textContent = 'Saved information changed or was refreshed. Review the current availability and choose your change again.';
            }
            cameras = cameraData;
            rooms = roomData;
            coverage = coverageData;
            refreshRoomChoices();
            setOptions('room-filter-building', coverage.map(room => room.building), 'All Buildings');
            setOptions('room-filter-floor', coverage.map(room => room.floor), 'All Floors');
            setOptions('room-filter-type', coverage.map(room => room.room_type), 'All Types');
            document.getElementById('camera-coverage-status').textContent = '';
            const timestamp = new Date().toLocaleString();
            notice.textContent = `Saved information ${wasLoaded ? 'refreshed' : 'loaded'} at ${timestamp}. No camera was opened.`;
            document.getElementById('camera-status-refreshed-at').textContent = `Saved information loaded at ${timestamp}. This is not the time of a camera test.`;
            savedDataLoaded = true;
            return true;
        } catch (_) {
            notice.textContent = wasLoaded ? 'Could not refresh saved information. The displayed list may be out of date. Retry Refresh saved status.' : 'Camera information could not be loaded. Check the connection to CSD FARS, then use Refresh saved status to retry.';
            document.getElementById('camera-coverage-status').textContent = notice.textContent;
            return false;
        } finally {
            savedDataLoading = false;
            savedDataRequest = null;
            renderCoverage();
        }
    })();
    return savedDataRequest;
}
function refreshCoverage() { return loadData(); }


coverageFilters.forEach(id => {
    document.getElementById(id)?.addEventListener('input', () => { coveragePage = 1; renderCoverage(); });
    document.getElementById(id)?.addEventListener('change', () => { coveragePage = 1; renderCoverage(); });
});
document.getElementById('room-filter-clear').addEventListener('click', () => {
    coveragePage = 1;
    coverageFilters.forEach(id => { document.getElementById(id).value = ''; });
    selectRoomCategory('all');
    renderCoverage();
});
document.querySelectorAll('[data-room-category]').forEach(button => {
    button.addEventListener('click', () => {
        coveragePage = 1;
        selectRoomCategory(button.dataset.roomCategory);
        renderCoverage();
    });
});
selectRoomCategory('all');
clearConnectionFields();

loadData();

window.CameraDiscovery.init({
    getMode: () => connectionKind.value,
    isBusy: () => wizardBusy,
    onSelected(camera, help) {
        connectionFields.forEach(id => { document.getElementById(id).value = ''; });
        const hikvision = camera.protocols.includes('Hikvision');
        selectedCameraRequiresPath = !hikvision;
        document.getElementById('camera-ip').value = camera.ip;
        if (!document.getElementById('camera-name').value.trim()) document.getElementById('camera-name').value = camera.model || camera.name || 'Room Camera';
        document.getElementById('camera-rtsp-port').value = '554';
        document.getElementById('camera-rtsp-path').value = hikvision ? '/Streaming/Channels/101' : '';
        document.getElementById('camera-manual-settings').open = !hikvision;
        document.getElementById('camera-url-fields').open = false;
        document.getElementById('camera-selection-status').textContent = `${camera.model || 'Camera'} selected. ${help} ` + (hikvision
            ? "Hikvision's default first-camera stream has been filled in. Recorder channels or a changed port can be adjusted in Connection details."
            : "The address is filled in. Add the stream path supplied by your installer in Connection details before testing.");
        updateConnectionUI();
        document.getElementById('camera-username').focus();
    }
});
