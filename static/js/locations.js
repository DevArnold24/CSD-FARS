const locationData = window.CSD_LOCATIONS || { buildings: [], floors: [], rooms: [] };
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
const locationFeedbackKey = 'csd-location-feedback';
const locationSuccessMessages = {
    building: 'Building saved successfully. Next, select it under Floor and add a floor.',
    floor: 'Floor saved successfully. Next, select it under Room and add a room.',
    room: 'Room saved successfully. It is now available in Classes and Cameras.',
    updated: 'Location updated successfully.',
    deleted: 'Unused location deleted successfully.',
    deactivated: 'Location deactivated. Its active cameras were also deactivated. Existing classes and attendance history were kept.',
    activated: 'Location activated. Parent locations must also be active. Cameras keep their current availability; manage them in Cameras.'
};

function showLocationFeedback() {
    try {
        const message = sessionStorage.getItem(locationFeedbackKey);
        sessionStorage.removeItem(locationFeedbackKey);
        if (!Object.values(locationSuccessMessages).includes(message)) return;
        const feedback = document.getElementById('location-feedback');
        feedback.textContent = message;
        feedback.hidden = false;
        feedback.focus();
    } catch (_) {
        // Storage may be unavailable; this must not block location management.
    }
}

function escapeLocationText(value) {
    const element = document.createElement('span');
    element.textContent = value == null ? '' : String(value);
    return element.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

let locationBusy = false;
let locationUncertain = false;
let pendingLocationDelete = null;
const locationDraftKey = 'csd-location-drafts';
const locationFields = {
    building: ['building-id','building-name'],
    floor: ['floor-id','floor-building-id','floor-name','floor-sort-order'],
    room: ['room-id','room-floor-id','room-code','room-name','room-type','room-capacity','room-notes']
};
const locationInitialValues = Object.fromEntries(Object.values(locationFields).flat().map(id => [id,document.getElementById(id).value]));

function setLocationControls() {
    document.querySelectorAll('#building-form button, #floor-form button, #room-form button, #location-hierarchy button, #building-form input, #floor-form input, #room-form input, #floor-form select, #room-form select, #room-form textarea')
        .forEach(control => { control.disabled = locationBusy || locationUncertain; });
}

function locationNotice(message, target = document.getElementById('location-feedback'), error = false, reload = false) {
    target.textContent = message;
    target.className = `location-item-feedback flash flash--${error ? 'error' : 'success'}`;
    target.setAttribute('role', error ? 'alert' : 'status');
    target.hidden = false;
    if (reload) {
        const link = document.createElement('a');
        link.href = '/settings/locations';
        link.textContent = 'Reload Saved Locations';
        link.style.marginLeft = '0.5rem';
        target.appendChild(link);
    }
    target.focus();
}

function rememberLocationDrafts(excludeType) {
    const drafts = {};
    for (const [type,fields] of Object.entries(locationFields)) {
        if (type === excludeType) continue;
        const values = Object.fromEntries(fields.map(id => [id,document.getElementById(id).value]));
        if (fields.some(id => values[id] !== locationInitialValues[id])) drafts[type] = values;
    }
    sessionStorage.setItem(locationDraftKey, JSON.stringify(drafts));
}

function restoreLocationDrafts() {
    try {
        const raw = sessionStorage.getItem(locationDraftKey);
        sessionStorage.removeItem(locationDraftKey);
        if (!raw || raw.length > 20000) return;
        const drafts = JSON.parse(raw);
        if (!drafts || typeof drafts !== 'object') return;
        for (const [type,fields] of Object.entries(locationFields)) {
            const values = drafts[type];
            if (!values || typeof values !== 'object') continue;
            fields.forEach(id => { if (typeof values[id] === 'string' && values[id].length <= 3000) document.getElementById(id).value = values[id]; });
            updateLocationFormLabel(type);
        }
    } catch (_) { /* A draft must never prevent the saved hierarchy from loading. */ }
}

async function locationRequest(url, method, body, successMessage = locationSuccessMessages.updated, target, excludeType) {
    if (locationBusy || locationUncertain) return false;
    closeLocationDelete(false);
    locationBusy = true;
    setLocationControls();
    let timer;
    const controller = new AbortController();
    let saved = false;
    try {
        const deadline = new Promise((_, reject) => {
            timer = setTimeout(() => {controller.abort(); reject(new Error('Unconfirmed location save'));},15000);
        });
        const {response,data} = await Promise.race([(async () => {
            const response = await fetch(url, {
                method, signal:controller.signal,
                headers: {'Content-Type':'application/json','X-CSRFToken':csrfToken,'Accept':'application/json'},
                body: body === undefined ? undefined : JSON.stringify(body)
            });
            return {response, data:await AuthResponse.readJson(response)};
        })(), deadline]);
        if (AuthResponse.redirectIfUnauthorized(response,data)) return false;
        if (!response.ok) {
            if (response.status >= 500) throw new Error('Unconfirmed server result');
            locationNotice(data.error || 'The location change was rejected. Your entries have been kept.',target,true);
            return false;
        }
        if (!data || typeof data.message !== 'string' || !data.message) throw new Error('Incomplete save confirmation');
        saved = true;
        try {
            rememberLocationDrafts(excludeType);
            sessionStorage.setItem(locationFeedbackKey,successMessage);
            window.location.reload();
        } catch (_) {
            locationUncertain = true;
            locationNotice(successMessage+' Copy any other unsaved entries before reloading to see the changes.',target,false,true);
        }
        return true;
    } catch (_) {
        locationUncertain = true;
        locationNotice('This change could not be confirmed. Your entries are still shown. Check the saved locations before making another change.',target,true,true);
        return false;
    } finally {
        clearTimeout(timer);
        locationBusy = saved;
        setLocationControls();
    }
}

async function submitLocationForm(event, type, body) {
    event.preventDefault();
    const form = event.currentTarget;
    if (locationBusy || locationUncertain || form.dataset.saving === 'true') return;
    const button = form.querySelector('[type="submit"]');
    const label = button.textContent;
    const id = document.getElementById(`${type}-id`).value;
    form.dataset.saving = 'true';
    form.setAttribute('aria-busy','true');
    button.textContent = 'Saving…';
    const saved = await locationRequest(id ? `/api/${type}s/${id}` : `/api/${type}s`, id ? 'PUT' : 'POST',body,
        id ? locationSuccessMessages.updated : locationSuccessMessages[type],document.getElementById(`${type}-feedback`),type);
    if (saved) {
        form.removeAttribute('aria-busy');
        button.textContent = 'Saved';
    } else {
        delete form.dataset.saving;
        form.removeAttribute('aria-busy');
        button.textContent = label;
        setLocationControls();
    }
}

function updateLocationFormLabel(type) {
    const editing = !!document.getElementById(`${type}-id`).value;
    const label = type[0].toUpperCase()+type.slice(1);
    document.getElementById(`${type}-form`).querySelector('[type="submit"]').textContent = `${editing ? 'Update' : 'Save'} ${label}`;
    document.getElementById(`${type}-clear`).textContent = editing ? 'Cancel Edit' : 'Clear';
}

function closeLocationDelete(restoreFocus = true) {
    if (!pendingLocationDelete) return;
    const {target, button, onKeydown} = pendingLocationDelete;
    pendingLocationDelete = null;
    target.removeEventListener('keydown', onKeydown);
    target.replaceChildren();
    target.hidden = true;
    target.setAttribute('role', 'status');
    target.removeAttribute('aria-label');
    button.removeAttribute('aria-expanded');
    if (restoreFocus && button.isConnected && !button.disabled) button.focus();
}

function reviewLocationDelete(type, item, button, target) {
    closeLocationDelete(false);
    if (!target) return;
    const building = type === 'floor' ? locationData.buildings.find(row => Number(row.id) === Number(item.building_id)) : null;
    const name = type === 'room' ? `${item.room_code}${item.room_name ? `: ${item.room_name}` : ''}`
        : type === 'floor' && building ? `${building.name} / ${item.name}` : item.name;
    const blocker = type === 'building' && Number(item.floor_count) > 0
        ? 'It contains floors.'
        : type === 'floor' && (Number(item.room_count) > 0 || locationData.rooms.some(row => Number(row.floor_id) === Number(item.id)))
            ? 'It contains rooms.'
            : type === 'room' && (Number(item.class_count) > 0 || Number(item.session_count) > 0)
                ? 'It is referenced by classes or attendance.'
                : type === 'room' && item.active_camera_id ? 'A camera is assigned to it.' : '';
    if (blocker) {
        locationNotice(`Cannot delete ${type} "${name}". ${blocker} Deactivate it instead if it is no longer in use; existing records are kept.`, target, true);
        return;
    }
    target.replaceChildren();
    target.className = 'location-item-feedback flash flash--warning';
    target.setAttribute('role', 'region');
    target.setAttribute('aria-label', `Delete ${type} ${name}`);
    target.hidden = false;
    const explanation = document.createElement('p');
    explanation.textContent = `Permanently delete ${type} "${name}"? This cannot be undone. Only unused locations can be deleted; the server will check current references before removing it.`;
    target.appendChild(explanation);
    const actions = document.createElement('div');
    actions.className = 'location-actions';
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'btn btn--secondary btn--sm';
    cancel.textContent = 'Cancel';
    const confirm = document.createElement('button');
    confirm.type = 'button';
    confirm.className = 'btn btn--danger btn--sm';
    confirm.textContent = `Delete ${type} permanently`;
    actions.appendChild(cancel);
    actions.appendChild(confirm);
    target.appendChild(actions);
    const pending = {target, button, onKeydown: event => {
        if (event.key === 'Escape' && !locationBusy) {
            event.preventDefault();
            closeLocationDelete();
        }
    }};
    pendingLocationDelete = pending;
    button.setAttribute('aria-expanded', 'true');
    target.addEventListener('keydown', pending.onKeydown);
    cancel.addEventListener('click', () => { if (pendingLocationDelete === pending) closeLocationDelete(); });
    confirm.addEventListener('click', () => {
        if (pendingLocationDelete !== pending || locationBusy || locationUncertain) return;
        return locationRequest(`/api/${type}s/${item.id}`, 'DELETE', undefined, locationSuccessMessages.deleted, target);
    });
    cancel.focus();
}

function statusActions(type, item) {
    const noun = type[0].toUpperCase() + type.slice(1);
    const name = escapeLocationText(item.name || item.room_code);
    return `<button class="btn btn--secondary btn--sm" aria-label="Edit ${noun} ${name}" data-edit-${type}="${item.id}">Edit</button>
        <button class="btn btn--secondary btn--sm" aria-label="${item.is_active ? 'Deactivate' : 'Activate'} ${noun} ${name}" data-toggle-${type}="${item.id}">${item.is_active ? 'Deactivate' : 'Activate'} ${noun}</button>
        <button class="btn btn--secondary btn--sm" aria-label="Delete ${noun} ${name}" data-delete-${type}="${item.id}">Delete</button>`;
}

function renderHierarchy() {
    const target = document.getElementById('location-hierarchy');
    target.innerHTML = locationData.buildings.map(building => {
        const floors = locationData.floors.filter(floor => Number(floor.building_id) === Number(building.id));
        return `<article data-location-item style="border:1px solid var(--color-border);border-radius:var(--radius-md);padding:var(--space-md);margin-bottom:var(--space-md);">
            <div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:var(--space-md);align-items:center;">
                <div><h4>${escapeLocationText(building.name)}</h4><small>${building.floor_count} ${Number(building.floor_count) === 1 ? 'floor' : 'floors'} · ${building.room_count} ${Number(building.room_count) === 1 ? 'room' : 'rooms'} · ${building.is_active ? 'Active' : 'Inactive'}</small></div>
                <div class="location-actions">${statusActions('building', building)}</div>
            </div>
            <div class="location-item-feedback flash" role="status" tabindex="-1" hidden></div>
            ${floors.map(floor => {
                const rooms = locationData.rooms.filter(room => Number(room.floor_id) === Number(floor.id));
                return `<div data-location-item style="margin:var(--space-md) 0 0 var(--space-lg);padding-left:var(--space-md);border-left:3px solid var(--color-border);">
                    <div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:var(--space-md);"><strong>${escapeLocationText(floor.name)} <small class="text-muted">${!building.is_active ? '(building inactive)' : floor.is_active ? '(active)' : '(inactive)'}</small></strong><span class="location-actions">${statusActions('floor', floor)}</span></div>
                    <div class="location-item-feedback flash" role="status" tabindex="-1" hidden></div>
                    ${rooms.map(room => `<div data-location-item style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:var(--space-md);padding:var(--space-sm);margin-top:var(--space-xs);background:var(--color-bg-input);border-radius:var(--radius-sm);">
                        <span><strong>${escapeLocationText(room.room_code)}</strong>: ${escapeLocationText(room.room_name)}<br><small>${room.active_camera_name ? `CCTV: ${escapeLocationText(room.active_camera_name)} (${escapeLocationText(room.camera_health_state === 'Healthy' ? 'Recent pass at refresh' : room.camera_health_state || 'Not tested')})` : 'No active CCTV / Manual Mode available'} · ${room.class_count} ${Number(room.class_count) === 1 ? 'class reference' : 'class references'} · ${!building.is_active ? 'Unavailable: building inactive' : !floor.is_active ? 'Unavailable: floor inactive' : room.is_active ? 'Active' : 'Inactive'}</small></span>
                        <span class="location-actions">${statusActions('room', room)}</span>
                        ${room.active_camera_id ? `<small style="flex-basis:100%;">${escapeLocationText(room.camera_health_detail || 'No valid saved check is available.')}</small>` : ''}
                        <div class="location-item-feedback flash" role="status" tabindex="-1" hidden></div>
                    </div>`).join('') || '<p class="text-muted">No rooms.</p>'}
                </div>`;
            }).join('') || '<p class="text-muted">No floors.</p>'}
        </article>`;
    }).join('') || '<p class="text-muted">No buildings have been created.</p>';
}

document.getElementById('building-form').addEventListener('submit', event => {
    submitLocationForm(event, 'building', { name: document.getElementById('building-name').value });
});
document.getElementById('floor-form').addEventListener('submit', event => {
    submitLocationForm(event, 'floor', { building_id: document.getElementById('floor-building-id').value, name: document.getElementById('floor-name').value, sort_order: document.getElementById('floor-sort-order').value });
});
document.getElementById('room-form').addEventListener('submit', event => {
    submitLocationForm(event, 'room', { floor_id: document.getElementById('room-floor-id').value, room_code: document.getElementById('room-code').value, room_name: document.getElementById('room-name').value, room_type: document.getElementById('room-type').value, capacity: document.getElementById('room-capacity').value, notes: document.getElementById('room-notes').value });
});

function clearLocationForm(type) {
    if (locationBusy || locationUncertain) return;
    document.getElementById(`${type}-form`).reset();
    document.getElementById(`${type}-id`).value = '';
    document.getElementById(`${type}-feedback`).hidden = true;
    updateLocationFormLabel(type);
}

document.getElementById('building-clear').addEventListener('click', () => clearLocationForm('building'));
document.getElementById('floor-clear').addEventListener('click', () => clearLocationForm('floor'));
document.getElementById('room-clear').addEventListener('click', () => clearLocationForm('room'));

document.getElementById('location-hierarchy').addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button || button.disabled || locationBusy || locationUncertain) return;
    for (const type of ['building', 'floor', 'room']) {
        const editId = button.dataset[`edit${type[0].toUpperCase()}${type.slice(1)}`];
        const toggleId = button.dataset[`toggle${type[0].toUpperCase()}${type.slice(1)}`];
        const deleteId = button.dataset[`delete${type[0].toUpperCase()}${type.slice(1)}`];
        const collection = locationData[`${type}s`];
        const target = button.closest('[data-location-item]')?.querySelector('.location-item-feedback');
        if (editId) {
            const item = collection.find(row => Number(row.id) === Number(editId));
            if (!item) return;
            closeLocationDelete(false);
            document.getElementById(`${type}-id`).value = item.id;
            if (type === 'building') document.getElementById('building-name').value = item.name;
            if (type === 'floor') {
                document.getElementById('floor-building-id').value = item.building_id;
                document.getElementById('floor-name').value = item.name;
                document.getElementById('floor-sort-order').value = item.sort_order;
            }
            if (type === 'room') {
                document.getElementById('room-floor-id').value = item.floor_id;
                document.getElementById('room-code').value = item.room_code;
                document.getElementById('room-name').value = item.room_name;
                document.getElementById('room-type').value = item.room_type;
                document.getElementById('room-capacity').value = item.capacity ?? '';
                document.getElementById('room-notes').value = item.notes || '';
            }
            updateLocationFormLabel(type);
            const focus = document.getElementById(type === 'room' ? 'room-code' : `${type}-name`);
            focus.focus();
            focus.scrollIntoView({block:'center',behavior:'smooth'});
            return;
        }
        if (toggleId) {
            const item = collection.find(row => Number(row.id) === Number(toggleId));
            if (!item) return;
            return locationRequest(`/api/${type}s/${toggleId}/toggle`, 'POST', {is_active:!item.is_active},item.is_active ? locationSuccessMessages.deactivated : locationSuccessMessages.activated,target);
        }
        if (deleteId) {
            const item = collection.find(row => Number(row.id) === Number(deleteId));
            if (item) reviewLocationDelete(type, item, button, target);
            return;
        }
    }
});

renderHierarchy();
restoreLocationDrafts();
showLocationFeedback();
window.addEventListener('pageshow', event => {
    if (event.persisted) {
        closeLocationDelete(false);
        locationUncertain = true;
        setLocationControls();
        locationNotice('This page was restored from history. Reload the saved locations before editing.',undefined,true,true);
    }
});
