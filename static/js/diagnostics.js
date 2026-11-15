const cpuBar = document.getElementById('cpu-bar');
const cpuText = document.getElementById('cpu-text');
const ramBar = document.getElementById('ram-bar');
const ramText = document.getElementById('ram-text');
const logTerminal = document.getElementById('log-terminal');
const pollStatus = document.getElementById('diagnostics-poll-status');
let diagnosticsPollInFlight = false;

function showDiagnosticsError(message) {
    if (typeof CapacityMonitor !== 'undefined') CapacityMonitor.update(null);
    pollStatus.hidden = false;
    pollStatus.setAttribute('role', 'alert');
    pollStatus.textContent = message;
    const cameraBadge = document.getElementById('camera-status');
    cameraBadge.textContent = 'Status Unavailable';
    cameraBadge.className = 'badge badge--warning';
    cameraBadge.setAttribute('title', 'Current camera observations are unavailable.');
    document.getElementById('camera-health-detail').textContent = 'Current observations are unavailable. Retrying automatically.';
    for (const id of ['cpu-text', 'ram-text', 'fps-text', 'resolution-text', 'camera-device-text',
        'camera-room-text', 'latency-text', 'coverage-total-rooms', 'coverage-no-camera',
        'coverage-healthy', 'coverage-failed', 'coverage-stale', 'coverage-not-tested']) {
        document.getElementById(id).textContent = '--';
    }
    for (const bar of [cpuBar, ramBar]) {
        bar.style.width = '0%';
        bar.style.backgroundColor = 'var(--color-border)';
        bar.setAttribute('aria-valuenow', '0');
        bar.setAttribute('aria-valuetext', 'Unavailable');
    }
    updatePipelineLatency({});
    logTerminal.textContent = message;
}

function clearDiagnosticsError() {
    pollStatus.hidden = true;
    pollStatus.setAttribute('role', 'status');
    pollStatus.textContent = '';
}

function getColorForUsage(percent) {
    if (percent < 60) return "var(--color-success)"; // green
    if (percent < 85) return "#b45309";
    return "var(--color-error)"; // red
}

function formatLatencyPair(latency, key) {
    const format = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value.toFixed(2) : '--';
    const avg = format(latency && latency[`${key}_ms`]);
    const max = format(latency && latency[`${key}_max_ms`]);
    return `${avg} / ${max} ms`;
}

function updatePipelineLatency(latency) {
    const mappings = {
        read: 'latency-read',
        preprocess: 'latency-preprocess',
        detect: 'latency-detect',
        liveness: 'latency-liveness',
        encode_face: 'latency-encode-face',
        match: 'latency-match',
        db_write: 'latency-db-write',
        jpeg_encode: 'latency-jpeg-encode',
        loop: 'latency-loop',
    };
    Object.entries(mappings).forEach(([key, elementId]) => {
        const el = document.getElementById(elementId);
        if (el) el.textContent = formatLatencyPair(latency || {}, key);
    });
}

async function pollDiagnostics() {
    if (diagnosticsPollInFlight) return;
    diagnosticsPollInFlight = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4500);
    try {
        const startReq = performance.now();
        const res = await fetch('/api/diagnostics/stats', { signal: controller.signal });
        const endReq = performance.now();
        const latency = Math.round(endReq - startReq);
        document.getElementById('latency-text').textContent = `${latency} ms`;

        const data = await AuthResponse.readJson(res);
        if (AuthResponse.redirectIfUnauthorized(res, data)) return;
        if (!res.ok) throw new Error(data.error || `Diagnostics request failed (${res.status}).`);
        const cpu = data.cpu;
        const ram = data.ram;
        if (![cpu, ram].every(value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100)) {
            throw new Error('Diagnostics returned invalid metrics.');
        }
        clearDiagnosticsError();
        if (typeof CapacityMonitor !== 'undefined') CapacityMonitor.update(data.capacity);
            
            // Update CPU
            cpuText.textContent = `${cpu.toFixed(1)}%`;
            cpuBar.style.width = `${cpu}%`;
            cpuBar.style.backgroundColor = getColorForUsage(cpu);
            cpuBar.setAttribute('aria-valuenow', String(cpu));
            cpuBar.setAttribute('aria-valuetext', `${cpu.toFixed(1)}%`);

            // Update RAM
            ramText.textContent = `${ram.toFixed(1)}%`;
            ramBar.style.width = `${ram}%`;
            ramBar.style.backgroundColor = getColorForUsage(ram);
            ramBar.setAttribute('aria-valuenow', String(ram));
            ramBar.setAttribute('aria-valuetext', `${ram.toFixed(1)}%`);

            // Update Camera Status
            const cameraBadge = document.getElementById('camera-status');
            const health = data.camera_health || {};
            const healthTones = {success: 'success', warning: 'warning', neutral: 'secondary'};
            cameraBadge.textContent = health.label || 'Status unavailable';
            cameraBadge.className = `badge badge--${healthTones[health.tone] || 'warning'}`;
            cameraBadge.setAttribute('title', health.detail || 'Camera health information is unavailable.');
            document.getElementById('camera-health-detail').textContent = health.detail || 'Camera health information is unavailable.';
            if (data.camera_active) {
                document.getElementById('fps-text').textContent = data.fps ? data.fps.toFixed(1) : '--';
                document.getElementById('resolution-text').textContent = data.resolution || '--';
                document.getElementById('camera-device-text').textContent = data.camera_name || '--';
                document.getElementById('camera-room-text').textContent = data.camera_room || '--';
            } else {
                document.getElementById('fps-text').textContent = '--';
                document.getElementById('resolution-text').textContent = '--';
                document.getElementById('camera-device-text').textContent = '--';
                document.getElementById('camera-room-text').textContent = '--';
            }

            const coverage = data.camera_coverage || {};
            document.getElementById('coverage-total-rooms').textContent = coverage.total_rooms ?? '--';
            document.getElementById('coverage-no-camera').textContent = coverage.rooms_without_camera ?? '--';
            document.getElementById('coverage-healthy').textContent = coverage.healthy_cameras ?? '--';
            document.getElementById('coverage-failed').textContent = coverage.failed_cameras ?? '--';
            document.getElementById('coverage-stale').textContent = coverage.stale_cameras ?? '--';
            document.getElementById('coverage-not-tested').textContent = coverage.not_recently_tested ?? '--';
            updatePipelineLatency(data.pipeline_latency || {});

            // Update Logs
            // Use a small threshold to determine if scrolled to bottom
            const isScrolledToBottom = (logTerminal.scrollHeight - logTerminal.clientHeight - logTerminal.scrollTop) < 10;
            
            if (data.logs_restricted) {
                logTerminal.textContent = 'Raw application logs are restricted. Use Sanitized Technical History for reviewed operational events.';
            } else if (!Array.isArray(data.logs) || data.logs.length === 0) {
                logTerminal.textContent = 'No recent warnings or errors.';
            } else {
                logTerminal.replaceChildren();
                data.logs.forEach((line, index) => {
                    if (index > 0) logTerminal.appendChild(document.createElement('br'));
                    logTerminal.appendChild(document.createTextNode(String(line)));
                });
            }

            if (isScrolledToBottom) {
                logTerminal.scrollTop = logTerminal.scrollHeight;
            }
    } catch (err) {
        console.error("Diagnostics polling failed:", err);
        showDiagnosticsError(
            err.name === 'AbortError'
                ? 'Diagnostics request timed out. Metrics are stale; retrying automatically.'
                : (err.message || 'Failed to connect to backend diagnostics. Metrics are stale.')
        );
    } finally {
        clearTimeout(timeout);
        diagnosticsPollInFlight = false;
    }
}

// Initial poll and set interval
pollDiagnostics();
setInterval(pollDiagnostics, 5000);
