(() => {
    'use strict';
    const node = id => document.getElementById(id);
    const refresh = node('network-refresh');
    const automatic = node('network-auto');
    let pending = null;
    let scheduled = null;
    let stopped = false;
    const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
    const age = value => finite(value) ? `${Math.round(value)} seconds ago` : 'Time unavailable';
    const rate = value => finite(value) ? `${value.toFixed(3)} Mbps` : 'Waiting for a new sample';
    const adapterGuidance = {
        unavailable: 'Adapter guidance is unavailable. Refresh to read the server connections again. Camera reachability and the route used by a camera have not been checked.',
        incomplete: 'Adapter readings are incomplete. CSD cannot reliably compare all connected IPv4 address ranges. Refresh and ask the installer to check the server connections if camera access changes when Wi-Fi is enabled.',
        overlap: 'Connected adapters report overlapping IPv4 address ranges. If these adapters lead to separate networks, Windows may send camera traffic through the wrong connection. Ask the installer to check the address ranges and routes. This reading does not identify the route used by a camera.',
        multiple: 'More than one IPv4 adapter is connected. No overlap was found in the reported adapter address ranges. Wi-Fi internet and a camera LAN can work together; Windows chooses the route for camera video. This does not confirm camera reachability or which route is used.',
        single: 'One IPv4 adapter is reported as connected. This observation does not test camera reachability or identify its route.',
        none: 'No connected IPv4 adapters were reported. Check the server connection; this observation cannot establish camera reachability.'
    };

    function renderAdapterGuidance(advisory) {
        const state = advisory?.state;
        const knownState = Object.prototype.hasOwnProperty.call(adapterGuidance, state) ? state : 'unavailable';
        const incomplete = advisory?.incomplete === true;
        const effectiveState = incomplete && ['multiple', 'single', 'none'].includes(knownState) ? 'incomplete' : knownState;
        node('network-adapter-guidance').textContent = adapterGuidance[effectiveState]
            + (effectiveState === 'overlap' && incomplete ? ' Some adapter readings are also unavailable or incomplete.' : '');
        node('network-adapter-guidance').className = effectiveState === 'overlap' ? 'network-warning' : '';
    }

    function card(container, title, pairs) {
        const item = document.createElement('article');
        item.className = 'network-device';
        const heading = document.createElement('h3');
        heading.textContent = title || 'Unnamed';
        item.appendChild(heading);
        const list = document.createElement('dl');
        for (const [label, value] of pairs) {
            const term = document.createElement('dt');
            const detail = document.createElement('dd');
            term.textContent = label;
            detail.textContent = value ?? 'Not reported';
            list.append(term, detail);
        }
        item.appendChild(list);
        node(container).appendChild(item);
    }

    function clearReadings(message) {
        for (const id of ['interfaces', 'clients', 'cameras', 'neighbors']) node(`network-${id}`).replaceChildren();
        for (const id of ['mode', 'address', 'response']) node(`network-${id}`).textContent = '--';
        for (const id of ['interface-note', 'client-note', 'camera-note', 'neighbor-note', 'route-note', 'mode-help']) node(`network-${id}`).textContent = 'Current information is unavailable.';
        node('network-address-warning').hidden = true;
        renderAdapterGuidance(null);
        node('network-status').textContent = message;
    }

    function render(data, elapsed) {
        if (!data.server || !data.interfaces || !data.windows || !Array.isArray(data.clients) || !Array.isArray(data.cameras?.items)) throw new Error('Invalid observations');
        clearReadings('Readings updated.');
        renderAdapterGuidance(data.interfaces.available === true ? data.adapter_advisory : null);
        const mode = data.server.mode;
        node('network-mode').textContent = {local: 'Local only', lan: 'Shared LAN', unmanaged: 'Launch mode not confirmed'}[mode] || 'Unknown';
        node('network-mode-help').textContent = mode === 'lan'
            ? 'This server was launched for shared access. A successful request here does not confirm that another laptop can connect.'
            : mode === 'local' ? 'This launch is for this computer. Complete shared setup before connecting other laptops.'
                : 'Use the supported server launcher to confirm the mode and connection address.';
        node('network-response').textContent = `${Math.round(elapsed)} ms`;
        const url = data.server.public_url;
        if (mode === 'lan' && typeof url === 'string' && /^https:\/\/\d{1,3}(?:\.\d{1,3}){3}\/$/.test(url)) {
            const link = document.createElement('a');
            link.textContent = url;
            link.href = url;
            node('network-address').replaceChildren(link);
            const address = new URL(url).hostname;
            if (data.interfaces.available && !data.interfaces.items.some(item => item.up && item.addresses.some(value => value.split('/')[0] === address))) {
                node('network-address-warning').textContent = 'The shared address is not on a connected adapter in this reading. Check the server connection and shared setup before distributing its connection kit.';
                node('network-address-warning').hidden = false;
            }
        } else node('network-address').textContent = 'Not available for this launch';
        const tables = data.windows;
        node('network-route-note').textContent = tables.routes_available
            ? `Gateway table read ${age(tables.age_seconds)}. A listed gateway is a route setting, not a connection test.`
            : 'Gateway information is unavailable or still loading.';
        node('network-interface-note').textContent = !data.interfaces.available ? 'Adapter readings are unavailable.'
            : data.interfaces.truncated ? 'Showing the first 128 adapters.'
                : data.interfaces.incomplete ? 'Some adapter readings are incomplete. Available readings are shown below.'
                : data.interfaces.items.length ? 'Traffic and error changes cover the sample interval shown on each adapter.' : 'No non-loopback IPv4 adapters were reported.';
        for (const item of (data.interfaces.items || [])) {
            const gateways = tables.routes_available ? (tables.routes || []).filter(row => row.interface === item.name).map(row => row.gateway).join(', ') : '';
            card('network-interfaces', item.name, [
                ['Link', item.up === true ? 'Connected' : item.up === false ? 'Disconnected' : 'Unknown'],
                ['IPv4 / prefix', item.addresses.join(', ')], ['Gateway', gateways || 'Not reported'],
                ['Link speed', finite(item.link_mbps) ? `${item.link_mbps} Mbps` : null],
                ['Receiving', rate(item.received_mbps)], ['Sending', rate(item.sent_mbps)],
                ['New errors', finite(item.new_errors) ? item.new_errors : null],
                ['New discards', finite(item.new_discards) ? item.new_discards : null],
                ['Sample interval', finite(item.sample_seconds) ? `${item.sample_seconds} seconds` : 'A second reading is needed']
            ]);
        }
        node('network-client-note').textContent = data.clients.length ? `${data.clients.length} recently observed ${data.clients.length === 1 ? 'address' : 'addresses'}.` : 'No signed-in client requests have been observed in this period.';
        for (const item of data.clients) card('network-clients', ['127.0.0.1', '::1'].includes(item.address) ? `This server (${item.address})` : item.address, [['Last request', age(item.age_seconds)], ['Observation', 'Signed-in CSD request']]);
        node('network-camera-note').textContent = !data.cameras.available ? 'Camera records are unavailable.'
            : data.cameras.truncated ? 'Showing the first 128 configured cameras.' : `${data.cameras.items.length} configured ${data.cameras.items.length === 1 ? 'camera' : 'cameras'}. Saved checks do not prove current reachability or recognition accuracy.`;
        for (const item of data.cameras.items) card('network-cameras', item.name, [
            ['Room', item.room || 'Unassigned'], ['Type', item.type], ['Enabled', item.enabled ? 'Yes' : 'No'],
            ['Saved check', item.check.label], ['Details', item.check.detail]
        ]);
        node('network-neighbor-note').textContent = tables.neighbors_available
            ? `Cache read ${age(tables.age_seconds)}. ${tables.truncated ? 'Showing at most 128 entries.' : `${tables.neighbors.length} entries reported.`}`
            : tables.supported === false ? 'Windows neighbor observations are not available on this operating system.'
                : tables.refreshing ? 'Reading the Windows cache. Refresh again shortly.' : 'Windows neighbor observations are unavailable.';
        if (tables.neighbors_available) for (const item of tables.neighbors) card('network-neighbors', item.address, [
            ['Windows state', item.state], ['Adapter', item.interface], ['Hardware address', item.mac]
        ]);
        node('network-updated').textContent = `Last refresh: ${new Date().toLocaleTimeString()}`;
    }

    function schedule() {
        clearTimeout(scheduled);
        if (!stopped && automatic.checked && !document.hidden) scheduled = setTimeout(poll, 10000);
    }
    async function poll() {
        if (pending || stopped || document.hidden) return;
        clearTimeout(scheduled);
        const controller = new AbortController();
        pending = controller;
        refresh.disabled = true;
        const timeout = setTimeout(() => controller.abort(), 6000);
        const started = performance.now();
        try {
            const response = await fetch('/api/diagnostics/network', {cache: 'no-store', signal: controller.signal});
            const data = await AuthResponse.readJson(response);
            if (stopped || document.hidden) return;
            if (AuthResponse.redirectIfUnauthorized(response, data)) {
                stopped = true;
                clearReadings('Sign in again to view network information.');
                return;
            }
            if (response.status === 403) {
                stopped = true;
                clearReadings('Your account no longer has access to network information.');
                return;
            }
            if (!response.ok) throw new Error('Unavailable');
            render(data, performance.now() - started);
        } catch (_) {
            if (!stopped && !document.hidden) clearReadings('Network readings could not be refreshed. Use Refresh now to retry.');
        } finally {
            clearTimeout(timeout);
            pending = null;
            refresh.disabled = stopped;
            schedule();
        }
    }
    refresh.addEventListener('click', poll);
    automatic.addEventListener('change', () => {
        schedule();
        node('network-status').textContent = automatic.checked ? 'Automatic refresh is on.' : 'Automatic refresh is paused. Displayed readings are from the last refresh.';
        if (automatic.checked) poll();
    });
    document.addEventListener('visibilitychange', () => {
        clearTimeout(scheduled);
        if (document.hidden) {
            pending?.abort();
            clearReadings('Refresh paused while this page is hidden.');
        } else if (!stopped) poll();
    });
    window.addEventListener('pagehide', () => { stopped = true; clearTimeout(scheduled); pending?.abort(); });
    window.addEventListener('pageshow', event => { if (event.persisted) { stopped = false; clearReadings('Refreshing network observations.'); poll(); } });
    poll();
})();
