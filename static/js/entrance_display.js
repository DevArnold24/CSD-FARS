/** Attendance Display polling and privacy-safe state transitions. */

function clearActiveDisplay(elements, options = {}) {
    const message = options.message || 'A Teacher will enable this display for an active attendance occurrence.';
    elements.rosterPanel.hidden = true;
    if (elements.layout) elements.layout.className = 'display-layout';
    if (elements.visualPanel) elements.visualPanel.hidden = false;
    if (elements.help) elements.help.textContent = '';
    if (elements.roomName) elements.roomName.textContent = 'Attendance Display';
    elements.rosterCounter.textContent = '0 / 0';
    elements.rosterList.innerHTML = '';
    if (elements.pageStatus) elements.pageStatus.textContent = '';
    elements.feed.src = '';
    elements.feed.hidden = true;
    elements.standbyPanel.hidden = false;
    elements.standbyTitle.textContent = options.title || 'Attendance Display Standby';
    elements.standbyMessage.textContent = message;
    elements.courseName.textContent = 'CSD Facial Attendance Recognition System';
    elements.schedule.textContent = '';
    elements.schedule.hidden = true;
    elements.roomCode.textContent = '';
    elements.roomCode.hidden = true;
    elements.statusPill.className = 'status-pill status-pill--idle';
    elements.statusText.textContent = options.status || 'Standby';
    if (typeof window !== 'undefined') {
        window.ACTIVE_ATTENDANCE_SESSION_ID = null;
    }
}

function escapeHtml(value) {
    return String(value || '').replace(/[&<>"']/g, (character) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
    })[character]);
}

function statusClass(status) {
    const normalized = String(status || 'Absent').toLowerCase();
    return ['present', 'late', 'absent', 'excused', 'pending'].includes(normalized) ? normalized : 'absent';
}

function displayTime(value) {
    const parts = String(value || '').split(':');
    return parts.length >= 2 ? `${parts[0]}:${parts[1]}` : String(value || '');
}

function isRecorded(student) {
    return Boolean(student.record_id) && ['Present', 'Late'].includes(student.status);
}

function boardStudents(roster, showAll, query = '') {
    const search = query.trim().toLocaleLowerCase();
    return roster.filter(student => (showAll || isRecorded(student)) &&
        `${student.name} ${student.student_code}`.toLocaleLowerCase().includes(search))
        .sort((a, b) => Number(isRecorded(b)) - Number(isRecorded(a)) ||
            String(b.recorded_at || b.time || '').localeCompare(String(a.recorded_at || a.time || '')) ||
            Number(b.record_id || 0) - Number(a.record_id || 0) ||
            String(a.name).localeCompare(String(b.name)));
}

// Keep each group stable while it is being read. New arrivals join spare spaces
// or the queue; they cannot continually reset the rotation and hide older names.
function createRosterPager() {
    let visible = [], pending = [], membership = new Set(), limit = 1;
    return {
        reset() { visible = []; pending = []; membership.clear(); },
        update(students, capacity) {
            limit = Math.max(1, capacity);
            const ids = students.map(student => student.student_code);
            const valid = new Set(ids);
            visible = visible.filter(id => valid.has(id));
            pending = pending.filter(id => valid.has(id));
            for (const id of ids) {
                if (!membership.has(id)) pending.push(id);
            }
            membership = valid;
            if (visible.length > limit) pending.unshift(...visible.splice(limit));
            while (visible.length < limit && pending.length) visible.push(pending.shift());
            return visible.map(id => students.find(student => student.student_code === id));
        },
        advance(students) {
            if (!pending.length) pending = students.map(student => student.student_code);
            visible = pending.splice(0, limit);
        }
    };
}

function renderStudentCards(students) {
    return students.map(student => {
        const recorded = isRecorded(student);
        const status = !student.record_id ? 'Pending' : student.status;
        return `<div class="student-card${recorded ? ' student-card--recorded' : ''}">
            <div class="student-name">${escapeHtml(student.name)}</div>
            <div class="student-meta">${escapeHtml(student.student_code)}</div>
            <div class="student-result"><strong>${recorded ? '✓ Recorded' : escapeHtml(status)}</strong>
                ${recorded ? `<span class="status-badge status--${statusClass(student.status)}">${escapeHtml(student.status)}</span>` : ''}</div>
            <div class="student-time">${recorded && student.time ? `Recorded ${escapeHtml(student.time)}` : '&nbsp;'}</div>
        </div>`;
    }).join('');
}

if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', () => {
        const config = window.ATTENDANCE_DISPLAY_CONFIG || {};
        const elements = {
            roomName: document.getElementById('room-display-name'),
            courseName: document.getElementById('course-display-name'),
            schedule: document.getElementById('schedule-display'),
            roomCode: document.getElementById('room-code-display'),
            clock: document.getElementById('clock-time'),
            statusPill: document.getElementById('status-pill'),
            statusText: document.getElementById('status-text'),
            feed: document.getElementById('attendance-feed'),
            standbyPanel: document.getElementById('standby-panel'),
            standbyTitle: document.getElementById('standby-title'),
            standbyMessage: document.getElementById('standby-message'),
            rosterPanel: document.getElementById('roster-panel'),
            rosterCounter: document.getElementById('roster-counter'),
            rosterList: document.getElementById('roster-list'),
            pageStatus: document.getElementById('page-status'),
            layout: document.getElementById('display-layout'),
            visualPanel: document.getElementById('visual-panel'),
            help: document.getElementById('display-help'),
            title: document.getElementById('results-title'),
            videoNotice: document.getElementById('display-video-notice')
        };
        let currentSessionId = null;
        let expectedSessionId = null;
        let roster = [];
        let collecting = false;
        const pager = createRosterPager();
        let nextGroupAt = Date.now() + 8000;
        let viewingCapacity = null;
        const videoWatchdog = window.CsdMjpegFeed ? new window.CsdMjpegFeed.MjpegFeedWatchdog(elements.feed, {
            baseUrl: '/inactive-display-video',
            isActive: () => collecting && currentSessionId !== null && !elements.visualPanel.hidden,
            onStateChange: state => {
                elements.videoNotice.textContent = state === 'connected' ? '' :
                    (viewingCapacity?.available === false ? viewingCapacity.message + ' ' : '') +
                    (state === 'exhausted' ? 'Camera preview unavailable. Ask your teacher to check the connection. Saved attendance is shown separately.' :
                     ['connecting', 'reconnecting', 'retrying'].includes(state) ? 'Reconnecting this video view. Saved attendance results are shown separately.' : '');
            }
        }) : null;

        function renderBoard(advance = false) {
            if (currentSessionId === null) return;
            const students = boardStudents(roster, false);
            const width = elements.rosterList.clientWidth - 24;
            const height = elements.rosterList.clientHeight - 24;
            const columns = Math.max(1, Math.floor((width + 10) / 330));
            const rows = Math.max(1, Math.floor((height + 10) / 200));
            const capacity = columns * rows;
            if (advance) pager.advance(students);
            const visible = pager.update(students, capacity);
            elements.rosterList.style.gridTemplateColumns = `repeat(${columns}, minmax(0, 1fr))`;
            const html = visible.length ? renderStudentCards(visible) : '<div class="empty-roster">' +
                (collecting ? 'Waiting for confirmed Present or Late attendance.' : 'No confirmed Present or Late results. Ask your teacher about your attendance.') + '</div>';
            if (elements.rosterList.innerHTML !== html) elements.rosterList.innerHTML = html;
            const multiple = students.length > capacity;
            elements.pageStatus.textContent = `Showing ${visible.length} of ${students.length} confirmations` +
                (multiple ? ` · Next group in ${Math.max(1, Math.ceil((nextGroupAt - Date.now()) / 1000))}s` : ' · All shown');
        }

        function changePage() {
            nextGroupAt = Date.now() + 8000;
            renderBoard(true);
        }
        if (typeof ResizeObserver !== 'undefined') new ResizeObserver(() => renderBoard()).observe(elements.rosterList);

        function updateClock() {
            elements.clock.textContent = new Date().toLocaleTimeString('en-US', {
                hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: true
            });
            if (Date.now() >= nextGroupAt) changePage();
            else renderBoard();
        }

        function renderInactive(message, status = 'Standby') {
            collecting = false;
            videoWatchdog?.stop();
            if (elements.videoNotice) elements.videoNotice.textContent = '';
            clearActiveDisplay(elements, { message, status });
            currentSessionId = null;
            roster = [];
            pager.reset();
        }

        function renderActive(data) {
            const sessionChanged = currentSessionId !== data.attendance_session_id;
            if (sessionChanged) {
                currentSessionId = data.attendance_session_id;
                pager.reset();
                nextGroupAt = Date.now() + 8000;
            }
            window.ACTIVE_ATTENDANCE_SESSION_ID = data.attendance_session_id;
            collecting = data.collection_active === true;
            elements.statusPill.className = !collecting && data.closure_confirmed
                ? 'status-pill status-pill--idle' : 'status-pill status-pill--active';
            elements.statusText.textContent = collecting ? (data.reopened ? 'Attendance reopened' : 'Attendance open') : data.closure_confirmed ? 'Ended' : data.collection_state === 'starting' ? 'Starting' : data.collection_state === 'interrupted' ? 'Interrupted' : 'Closure unconfirmed';
            elements.title.textContent = collecting ? 'Attendance confirmations' : 'Saved attendance results';
            elements.help.textContent = (collecting ? 'Present and Late cards confirm saved attendance. Ask your teacher if your name is missing.' : 'Collection is stopped. These are saved results for this session. Ask your teacher about missing or incorrect results.') + (data.review_required ? ' Some results still need teacher review.' : '') + (data.cleanup_unfinished && !collecting ? ' The camera stop has not been confirmed.' : '');
            elements.roomName.textContent = data.room_name || data.room_code || 'Attendance Display';
            elements.courseName.textContent = `${data.course_code ? `${data.course_code} - ` : ''}${data.course_name || ''}`;
            elements.roomCode.textContent = data.room_code || '';
            elements.roomCode.hidden = !data.room_code;
            elements.schedule.textContent = `${data.scheduled_date || ""} · ${displayTime(data.scheduled_start_time)} - ${displayTime(data.scheduled_end_time)} · Session #${data.attendance_session_id}`;
            elements.schedule.hidden = false;
            elements.rosterPanel.hidden = false;
            roster = Array.isArray(data.roster) ? data.roster : [];
            elements.rosterCounter.textContent = `${roster.filter(isRecorded).length} Present or Late`;

            elements.layout.className = collecting && data.has_camera ? 'display-layout' : 'display-layout results-only';
            elements.visualPanel.hidden = !(collecting && data.has_camera);
            if (collecting && data.has_camera && data.video_url) {
                viewingCapacity = data.viewing_capacity || null;
                if (videoWatchdog?.exhausted) {
                    elements.videoNotice.textContent = 'Camera preview unavailable. Ask your teacher to refresh this display after checking the connection. Saved attendance is shown separately.';
                }
                if (!videoWatchdog?.exhausted && (sessionChanged || (videoWatchdog ? !videoWatchdog.running : !elements.feed.getAttribute?.('src')))) {
                    if (videoWatchdog) { videoWatchdog.baseUrl = data.video_url; videoWatchdog.start(); }
                    else elements.feed.src = `${data.video_url}?t=${Date.now()}`;
                }
                if (Number.isInteger(data.publication_sequence) && Number.isFinite(data.current_frame_age_ms)) {
                    videoWatchdog?.observeProgress(data.publication_sequence, data.current_frame_age_ms);
                }
                elements.feed.hidden = false;
                elements.standbyPanel.hidden = true;
            } else {
                videoWatchdog?.stop();
                if (elements.videoNotice) elements.videoNotice.textContent = '';
                elements.feed.src = '';
                elements.feed.hidden = true;
                elements.standbyPanel.hidden = false;
                elements.standbyTitle.textContent = 'Manual Attendance Mode';
                elements.standbyMessage.textContent = 'No active CCTV is assigned. Attendance remains active and the roster updates from Teacher corrections.';
            }

            renderBoard();
        }

        async function pollStatus() {
            if (!config.statusUrl) {
                renderInactive('This retired Room bookmark cannot display attendance. Open the link generated by the Teacher.');
                return;
            }
            const controller = new AbortController();
            const deadline = setTimeout(() => controller.abort(), 8000);
            try {
                const separator = config.statusUrl.includes('?') ? '&' : '?';
                const response = await fetch(`${config.statusUrl}${separator}t=${Date.now()}`, {
                    cache: 'no-store', credentials: 'same-origin', signal: controller.signal
                });
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();
                if (!data.authorized) {
                    renderInactive(data.message || 'Attendance Display is inactive.');
                    return;
                }
                if (!Number.isInteger(data.attendance_session_id) || !Array.isArray(data.roster) || typeof data.collection_active !== 'boolean') throw new Error('Incomplete results response');
                if (expectedSessionId !== null && expectedSessionId !== data.attendance_session_id) throw new Error('Occurrence changed');
                expectedSessionId = data.attendance_session_id;
                renderActive(data);
            } catch (error) {
                console.warn('Attendance Display connection interrupted.');
                renderInactive('Connection interrupted. Attendance content was cleared while the display reconnects.', 'Connecting');
            } finally {
                clearTimeout(deadline);
                // Schedule after completion so slow responses cannot overwrite newer state.
                setTimeout(pollStatus, 1000);
            }
        }

        updateClock();
        setInterval(updateClock, 1000);
        pollStatus();
    });
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { clearActiveDisplay, displayTime, escapeHtml, statusClass, isRecorded, boardStudents, createRosterPager, renderStudentCards };
}
