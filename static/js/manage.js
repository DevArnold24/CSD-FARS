document.addEventListener("DOMContentLoaded", function() {
    const rows = document.querySelectorAll('#classTableBody .class-row');
    const teacherSet = new Set();
    rows.forEach(row => {
        const teacherName = row.dataset.teacher;
        if(teacherName) {
            teacherSet.add(teacherName.trim());
        }
    });
    const filterSelect = document.getElementById('teacherFilter');
    if (filterSelect) {
        teacherSet.forEach(t => {
            const opt = document.createElement('option');
            opt.value = t;
            opt.innerText = t;
            filterSelect.appendChild(opt);
        });
    }

    setupCameraRoomSorting();
    setupSlotHelper();
});

function setupSlotHelper() {
    const roomSelect = document.getElementById('room_id');
    const panel = document.getElementById('slot-helper-panel');
    const content = document.getElementById('slot-helper-content');
    const retry = document.getElementById('slot-helper-retry');
    if (!roomSelect || !panel || !content || !retry) return;

    const classIdMatch = window.location.pathname.match(/\/classes\/(\d+)\/edit/);
    const excludeClassId = classIdMatch ? classIdMatch[1] : '';
    let scheduleRequest = 0;
    let pending = null;

    function cancelPending() {
        if (!pending) return;
        const previous = pending;
        pending = null;
        window.clearTimeout(previous.timeout);
        previous.reject(new Error('Room selection changed.'));
        previous.controller.abort();
    }

    async function updateScheduleHelper(trigger) {
        const request = ++scheduleRequest;
        cancelPending();
        // Keep a keyboard-triggered retry in place while its result updates.
        retry.hidden = trigger !== 'retry';
        retry.removeAttribute('aria-disabled');
        content.removeAttribute('aria-busy');
        const roomId = roomSelect.value;
        if (!roomId) {
            panel.style.display = 'none';
            return;
        }

        const controller = new AbortController();
        const state = {controller};
        const deadline = new Promise((_, reject) => {
            state.reject = reject;
            state.timeout = window.setTimeout(() => {
                reject(new Error('Room availability request timed out.'));
                controller.abort();
            }, 15000);
        });
        pending = state;
        panel.style.display = 'block';
        content.dataset.state = 'loading';
        content.textContent = 'Loading room bookings...';
        content.setAttribute('aria-busy', 'true');
        retry.setAttribute('aria-disabled', 'true');
        retry.textContent = 'Checking Room...';
        let url = `/api/rooms/${roomId}/schedule`;
        if (excludeClassId) url += `?exclude_class_id=${excludeClassId}`;

        try {
            const result = await Promise.race([(async () => {
                const res = await fetch(url, {signal: controller.signal, cache: 'no-store'});
                if (request !== scheduleRequest || pending !== state) return null;
                if (AuthResponse.redirectIfUnauthorized(res)) return {expired: true};
                return {res, data: await AuthResponse.readJson(res)};
            })(), deadline]);
            if (request !== scheduleRequest || !result || result.expired) return;
            const {res, data} = result;
            if (!res.ok || !Array.isArray(data.slots)) throw new Error('Failed to fetch schedule');
            content.dataset.state = 'ready';

            if (data.slots.length === 0) {
                content.textContent = 'No classes are booked in this room. Teacher availability is checked when you save.';
                return;
            }

            const heading = document.createElement('div');
            heading.style.marginBottom = '4px';
            heading.style.fontWeight = '500';
            heading.textContent = 'Existing room bookings';
            const list = document.createElement('ul');
            list.style.margin = '0';
            list.style.paddingLeft = '18px';
            list.style.lineHeight = '1.5';
            data.slots.forEach(slot => {
                const item = document.createElement('li');
                const days = document.createElement('strong');
                days.textContent = String(slot.days || '');
                item.appendChild(days);
                item.appendChild(document.createTextNode(
                    `: ${slot.start_time || ''} - ${slot.end_time || ''} (` +
                    `${slot.course_name || ''} / ${slot.class_code || ''} - ` +
                    `Prof. ${slot.teacher_name || ''})`
                ));
                list.appendChild(item);
            });
            content.replaceChildren(heading, list);
        } catch (e) {
            if (request !== scheduleRequest) return;
            content.dataset.state = 'error';
            content.textContent = 'Room availability could not be checked. Retry the check below. Your class details are still here; availability is also checked when you save.';
            retry.hidden = false;
        } finally {
            window.clearTimeout(state.timeout);
            if (request === scheduleRequest) {
                pending = null;
                retry.removeAttribute('aria-disabled');
                retry.textContent = content.dataset.state === 'error' ? 'Retry Room Check' : 'Check Room Again';
                content.removeAttribute('aria-busy');
            }
        }
    }

    roomSelect.addEventListener('change', updateScheduleHelper);
    retry.addEventListener('click', () => {
        if (!pending) return updateScheduleHelper('retry');
    });
    window.addEventListener('pagehide', () => {
        ++scheduleRequest;
        cancelPending();
        content.removeAttribute('aria-busy');
    });
    window.addEventListener('pageshow', event => {
        if (event.persisted) updateScheduleHelper();
    });
    if (roomSelect.value) updateScheduleHelper();
}

function filterClasses() {
    const filterSelect = document.getElementById('teacherFilter');
    if (!filterSelect) return;
    
    const selected = filterSelect.value;
    const rows = document.querySelectorAll('#classTableBody .class-row');
    rows.forEach(row => {
        if (selected === "all") {
            row.style.display = '';
        } else {
            const teacherName = row.dataset.teacher;
            if (teacherName && teacherName.trim() === selected) {
                row.style.display = '';
            } else {
                row.style.display = 'none';
            }
        }
    });
}

function setupCameraRoomSorting() {
    const roomSelect = document.getElementById('room_id');
    const roomSearch = document.getElementById('room_search');
    const roomInput = document.getElementById('room_number');
    const cameraSelect = document.getElementById('camera_id');
    if (!roomInput || !cameraSelect) return;
    const warning = document.getElementById('camera-room-warning');

    const originalOptions = Array.from(cameraSelect.options).map(option => option.cloneNode(true));
    const originalRoomOptions = roomSelect ? Array.from(roomSelect.options).map(option => option.cloneNode(true)) : [];

    function normalize(value) {
        return (value || '').trim().toLowerCase();
    }

    function naturalCompare(a, b) {
        return String(a || '').localeCompare(String(b || ''), undefined, { numeric: true, sensitivity: 'base' });
    }

    function isFallbackOption(option) {
        return option && option.dataset.fallback === '1';
    }

    function selectedRoomId() {
        return roomSelect ? roomSelect.value : '';
    }

    function selectedRoomCode() {
        if (roomSelect) {
            const selected = roomSelect.selectedOptions[0];
            return selected ? selected.dataset.roomCode || '' : '';
        }
        return roomInput.value;
    }

    function syncRoomInput() {
        if (roomSelect) {
            roomInput.value = selectedRoomCode();
        }
    }

    function sortAndFilterRoomOptions() {
        if (!roomSelect) return;
        const selectedValue = roomSelect.value;
        const query = normalize(roomSearch ? roomSearch.value : '');
        const placeholder = originalRoomOptions.find(option => !option.value);
        const roomOptions = originalRoomOptions
            .filter(option => option.value)
            .filter(option => {
                if (!query) return true;
                return normalize(`${option.dataset.roomCode || ''} ${option.textContent || ''}`).includes(query);
            })
            .sort((a, b) => naturalCompare(a.dataset.roomCode, b.dataset.roomCode) || naturalCompare(a.textContent, b.textContent));

        roomSelect.innerHTML = '';
        if (placeholder) roomSelect.appendChild(placeholder.cloneNode(true));
        roomOptions.forEach(option => roomSelect.appendChild(option.cloneNode(true)));
        if (selectedValue && roomOptions.some(option => option.value === selectedValue)) {
            roomSelect.value = selectedValue;
        }
        syncRoomInput();
        sortCameraOptions();
    }

    function updateCameraWarning() {
        if (!warning) return;

        const selected = cameraSelect.selectedOptions[0];
        syncRoomInput();
        const room = normalize(roomInput.value);
        const roomId = selectedRoomId();
        if (!selected || !selected.value || !room) {
            warning.classList.remove('is-visible');
            warning.textContent = '';
            return;
        }

        const cameraRoom = normalize(selected.dataset.room);
        const cameraRoomId = selected.dataset.roomId || '';
        const cameraRoomDisplay = selected.dataset.roomDisplay || selected.dataset.room || 'the selected camera room';
        if (isFallbackOption(selected)) {
            warning.textContent = 'This camera is not assigned to the selected room. Configure the room camera before starting camera attendance.';
            warning.classList.add('is-visible');
            return;
        }

        if ((roomId && cameraRoomId && cameraRoomId !== roomId) || (!roomId && cameraRoom && cameraRoom !== room)) {
            warning.textContent = `This camera is assigned to ${cameraRoomDisplay}, but this class room is ${roomInput.value.trim()}. Check the room assignment before starting camera attendance.`;
            warning.classList.add('is-visible');
            return;
        }

        warning.classList.remove('is-visible');
        warning.textContent = '';
    }

    function sortCameraOptions() {
        syncRoomInput();
        const selectedValue = cameraSelect.value;
        const room = normalize(roomInput.value);
        const roomId = selectedRoomId();
        const placeholder = originalOptions.find(option => !option.value);
        const cameraOptions = originalOptions.filter(option => option.value);

        cameraOptions.sort((a, b) => {
            const aRoom = normalize(a.dataset.room);
            const bRoom = normalize(b.dataset.room);
            const aRoomId = a.dataset.roomId || '';
            const bRoomId = b.dataset.roomId || '';
            const aMatch = (roomId && aRoomId === roomId) || (room && aRoom === room) ? 0 : 1;
            const bMatch = (roomId && bRoomId === roomId) || (room && bRoom === room) ? 0 : 1;
            if (aMatch !== bMatch) return aMatch - bMatch;

            const aFallback = isFallbackOption(a) ? 1 : 0;
            const bFallback = isFallbackOption(b) ? 1 : 0;
            if (aFallback !== bFallback) return aFallback - bFallback;

            const aDefault = a.dataset.default === '1' || a.dataset.default === 'True' ? 0 : 1;
            const bDefault = b.dataset.default === '1' || b.dataset.default === 'True' ? 0 : 1;
            if (aDefault !== bDefault) return aDefault - bDefault;

            return a.textContent.localeCompare(b.textContent);
        });

        cameraSelect.innerHTML = '';
        if (placeholder) {
            cameraSelect.appendChild(placeholder.cloneNode(true));
        }
        cameraOptions.forEach(option => cameraSelect.appendChild(option.cloneNode(true)));
        if (selectedValue) {
            cameraSelect.value = selectedValue;
        } else {
            const sameRoomOptions = cameraOptions.filter(option => {
                if (isFallbackOption(option)) return false;
                return (roomId && option.dataset.roomId === roomId) || (room && normalize(option.dataset.room) === room);
            });
            if (sameRoomOptions.length === 1) {
                cameraSelect.value = sameRoomOptions[0].value;
            }
        }
        updateCameraWarning();
    }

    if (roomSelect) {
        sortAndFilterRoomOptions();
        roomSelect.addEventListener('change', sortCameraOptions);
    } else if (roomInput) {
        roomInput.addEventListener('input', sortCameraOptions);
    }
    cameraSelect.addEventListener('change', updateCameraWarning);
    sortCameraOptions();
}
