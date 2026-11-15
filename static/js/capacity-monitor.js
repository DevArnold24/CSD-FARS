/* Bounded observations while this page is open; never a certified camera limit. */
const CapacityMonitor = (() => {
    const readings = [];
    const windowMs = 5 * 60 * 1000;
    const node = id => document.getElementById(id);
    const numeric = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
    const count = value => numeric(value) ? String(value) : '--';
    const decimal = value => numeric(value) ? value.toFixed(1) : '--';
    const tones = {success: 'success', warning: 'warning', danger: 'danger', neutral: 'secondary'};
    function cell(row, text) {
        const td = document.createElement('td');
        td.textContent = text;
        row.appendChild(td);
    }
    function update(data) {
        const valid = data && data.available === true && Array.isArray(data.cameras) && Array.isArray(data.alerts);
        const status = node('capacity-status');
        status.textContent = valid ? data.label : 'Capacity readings unavailable';
        status.className = `badge badge--${valid ? tones[data.tone] || 'warning' : 'warning'}`;
        node('capacity-alerts').replaceChildren();
        node('capacity-cameras').replaceChildren();
        node('capacity-empty').hidden = false;
        if (!valid) {
            readings.length = 0;
            for (const id of ['capacity-configured', 'capacity-sessions', 'capacity-previews', 'capacity-streams']) node(id).textContent = '--';
            node('capacity-stream-detail').textContent = 'Current viewing limits are unavailable.';
            node('capacity-window').textContent = 'Observation interrupted. Fresh readings are needed.';
            node('capacity-empty').textContent = 'Current camera readings are unavailable.';
            return;
        }
        node('capacity-configured').textContent = count(data.configured_cameras);
        node('capacity-sessions').textContent = count(data.recognition_sessions);
        node('capacity-previews').textContent = count(data.preview_reservations);
        const admission = data.admission;
        if (admission?.configured) {
            node('capacity-sessions').textContent = `${count(admission.recognition_reserved)} / ${count(admission.configured.recognition)} reserved`;
            node('capacity-previews').textContent = `${count(admission.secondary_reserved)} / ${count(admission.configured.secondary)} reserved`;
        }
        const streams = data.streams || {};
        node('capacity-streams').textContent = `${count(streams.active_streams)} / ${count(streams.max_streams)}`;
        node('capacity-stream-detail').textContent = `${count(streams.available_streams)} video slots free; ${count(streams.rejected_total)} viewing requests refused since server start. Other camera use includes setup, enrollment previews and pending cleanup.`;
        for (const text of data.alerts) {
            const item = document.createElement('li');
            item.textContent = text;
            node('capacity-alerts').appendChild(item);
        }
        for (const camera of data.cameras) {
            const row = document.createElement('tr');
            cell(row, `${camera.name} · ${camera.purpose}`);
            cell(row, camera.status);
            cell(row, decimal(camera.capture_fps));
            cell(row, decimal(camera.preview_fps));
            cell(row, decimal(camera.recognition_fps));
            cell(row, numeric(camera.result_age_ms) ? `${(camera.result_age_ms / 1000).toFixed(1)} s` : '--');
            cell(row, `${count(camera.read_failures)} / ${count(camera.reconnect_attempts)}`);
            node('capacity-cameras').appendChild(row);
        }
        node('capacity-empty').hidden = data.cameras.length > 0;
        node('capacity-empty').textContent = 'No recognition sessions are using cameras. An idle reading cannot establish the camera limit.';
        const now = performance.now();
        // A suspended/background tab has a gap, not continuous load evidence.
        if (readings.length && now - readings[readings.length - 1].at > 15000) readings.length = 0;
        while (readings.length && (now - readings[0].at > windowMs || readings.length >= 61)) readings.shift();
        if (numeric(data.cpu_percent) && numeric(data.ram_percent)) readings.push({at: now, cpu: data.cpu_percent, ram: data.ram_percent, sessions: data.recognition_sessions});
        if (readings.length) {
            const seconds = Math.floor((now - readings[0].at) / 1000);
            node('capacity-window').textContent = `This page: ${readings.length} readings over ${seconds} s (up to 5 minutes). Peak CPU ${Math.max(...readings.map(r => r.cpu)).toFixed(1)}%, memory ${Math.max(...readings.map(r => r.ram)).toFixed(1)}%, recognition sessions ${Math.max(...readings.map(r => numeric(r.sessions) ? r.sessions : 0))}. This is observed load, not a validated camera count.`;
        }
    }
    return {update};
})();
