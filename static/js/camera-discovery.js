// Read-only camera discovery. Device data is untrusted and is never rendered as markup.
(() => {
    const button = document.getElementById('btn-discover-cameras');
    const network = document.getElementById('camera-search-network');
    const status = document.getElementById('camera-discovery-status');
    const results = document.getElementById('discover-results');
    let options, devices = [], selectedInterface = null, generation = 0, controller = null, searching = false;
    const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
    const current = run => run === generation && options?.getMode() === 'rtsp';

    function syncControls() {
        button.disabled = searching || options?.isBusy() || options?.getMode() !== 'rtsp';
        network.disabled = searching || options?.isBusy();
        button.textContent = searching ? 'Looking for cameras...' : 'Find cameras';
        results.setAttribute('aria-busy', String(searching));
    }

    function reset() {
        generation++;
        controller?.abort();
        controller = null;
        searching = false;
        devices = [];
        selectedInterface = null;
        results.replaceChildren();
        results.style.display = 'none';
        status.textContent = '';
        status.setAttribute('role', 'status');
        syncControls();
    }

    function populateNetworks(data) {
        if (!data || !Array.isArray(data.interfaces)) throw new Error('Network connections could not be listed. Try again or use manual setup.');
        const selected = network.value;
        network.innerHTML = '<option value="">Choose automatically</option>' + data.interfaces.map(item => {
            if (!item || typeof item.id !== 'string' || typeof item.name !== 'string' || typeof item.address !== 'string') throw new Error('Network search returned incomplete connection details. Try again.');
            return `<option value="${escape(item.id)}">${escape(item.name)}${item.recommended ? ' (recommended)' : ''}: ${escape(item.address)}</option>`;
        }).join('');
        network.value = data.interfaces.some(item => item.id === selected) ? selected : '';
    }

    async function loadNetworks() {
        const run = generation;
        const pending = new AbortController();
        const timeout = setTimeout(() => pending.abort(), 8000);
        try {
            const response = await fetch('/api/cameras/discovery/networks', {signal: pending.signal, cache: 'no-store'});
            const data = await AuthResponse.readJson(response);
            if (run !== generation || AuthResponse.redirectIfUnauthorized(response, data)) return;
            if (!response.ok) return; // Automatic discovery can still explain or recover from this failure.
            populateNetworks(data);
        } catch { /* Manual entry and an automatic search remain available. */ }
        finally { clearTimeout(timeout); }
    }

    function guidance(camera) {
        if (camera.activated === false) return ['Needs first-time setup', 'The camera needs to be activated by its installer before it can be used. Ask the person who installed it to complete its initial setup.'];
        if (camera.network_status === 'different_network') return ['Network settings need checking', 'The camera and this computer use different address ranges. They may fail to connect even when plugged into the same switch. Ask the person who manages your network to align the camera settings with the intended network.'];
        if (camera.network_status === 'same_network') return ['Found on this network', 'Enter the camera login, then test the picture. Discovery alone does not verify the video connection.'];
        return ['Found. Connection needs testing.', 'The camera responded, but its network settings could not be checked. Test the picture, or ask your installer for help.'];
    }

    function renderDevices() {
        results.style.display = 'block';
        results.innerHTML = devices.map((camera, index) => {
            const [label, help] = guidance(camera);
            const networkLabel = selectedInterface ? `${selectedInterface.name} (${selectedInterface.address})` : 'Not reported';
            return `<section class="camera-discovery-card" aria-labelledby="found-camera-${index}">
                <h4 id="found-camera-${index}">${escape(camera.model || camera.name || 'Network camera')}</h4>
                <p class="camera-found-status">${escape(label)}</p><p>${escape(help)}</p>
                <button type="button" class="btn btn--secondary" data-discovery-index="${index}">Use this camera</button>
                <details><summary>Details for IT support</summary><dl>
                <dt>System connection</dt><dd>${escape(networkLabel)}</dd>
                <dt>System subnet mask</dt><dd>${escape(selectedInterface?.netmask || 'Not reported')}</dd>
                <dt>Camera address</dt><dd>${escape(camera.ip)}</dd>
                <dt>Camera subnet mask</dt><dd>${escape(camera.netmask || 'Not reported')}</dd>
                <dt>Discovery method</dt><dd>${escape(camera.protocols.join(', '))}</dd></dl>
                <p>Check camera power, the switch connection, address settings and any network isolation. Network changes should preserve this computer's working connection. No network settings were changed by this search.</p></details></section>`;
        }).join('');
    }

    async function discover() {
        if (searching || options.isBusy() || options.getMode() !== 'rtsp') return;
        reset();
        const run = generation;
        searching = true;
        const requestController = new AbortController();
        controller = requestController;
        const timeout = setTimeout(() => requestController.abort(), 12000);
        syncControls();
        status.textContent = 'Looking for powered-on cameras. This usually takes a few seconds.';
        try {
            const query = network.value ? `?interface=${encodeURIComponent(network.value)}` : '';
            const response = await fetch('/api/cameras/discovery' + query, {signal: controller.signal, cache: 'no-store'});
            const data = await AuthResponse.readJson(response);
            if (!current(run) || AuthResponse.redirectIfUnauthorized(response, data)) return;
            if (!response.ok) throw new Error(data?.error || 'Camera search could not finish. Try again or use manual setup.');
            if (data?.interface === null && Array.isArray(data.interfaces) && Array.isArray(data.devices) && !data.devices.length && Array.isArray(data.warnings) && data.warnings.includes('no_local_network')) {
                populateNetworks(data);
                status.textContent = 'No local network connection is available for an automatic search. Connect this computer to the camera\'s router or switch and try again, or choose a connection in Search options.';
                return;
            }
            if (!data || !Array.isArray(data.devices) || !data.interface || typeof data.interface.name !== 'string' || typeof data.interface.address !== 'string' || !Array.isArray(data.warnings)
                || data.devices.some(item => !item || typeof item.ip !== 'string' || !Array.isArray(item.protocols))) {
                throw new Error('Camera search returned an incomplete result. Try again; this does not mean there are no cameras.');
            }
            populateNetworks(data);
            selectedInterface = data.interface;
            devices = data.devices;
            const suffix = data.warnings.length ? ' Part of the search could not complete. Try again or use manual setup if your camera is missing.' : '';
            status.textContent = devices.length
                ? `${devices.length} ${devices.length === 1 ? 'camera found' : 'cameras found'} using ${selectedInterface.name}.${suffix}`
                : `No camera replied using ${selectedInterface.name}. Check its power and cable or Wi-Fi connection, then try again. Some cameras require discovery to be enabled by their installer. You can also enter its connection details manually.${suffix}`;
            renderDevices();
        } catch (error) {
            if (!current(run)) return;
            status.setAttribute('role', 'alert');
            status.textContent = error.name === 'AbortError'
                ? 'Camera search timed out. Check the camera connection and try again. Your form details are still here.'
                : error instanceof TypeError || error.name === 'TypeError' ? 'The system could not be reached. Check your connection and try again.' : error.message;
            loadNetworks();
        } finally {
            clearTimeout(timeout);
            if (run === generation) { searching = false; controller = null; syncControls(); }
        }
    }

    window.CameraDiscovery = {reset, syncControls, init(callbacks) {
        options = callbacks;
        button.addEventListener('click', discover);
        network.addEventListener('change', reset);
        results.addEventListener('click', event => {
            const target = event.target.closest('[data-discovery-index]');
            if (!target || searching || options.isBusy() || options.getMode() !== 'rtsp') return;
            const index = Number(target.dataset.discoveryIndex);
            if (!Number.isInteger(index) || !devices[index]) return;
            options.onSelected(devices[index], guidance(devices[index])[1]);
        });
        window.addEventListener('pagehide', reset, {once: true});
        syncControls();
        loadNetworks();
    }};
})();
