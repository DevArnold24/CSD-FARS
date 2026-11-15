(function (root) {
    'use strict';

    const blankFeedSrc = 'data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=';

    function byId(documentRef, id) {
        return documentRef && typeof documentRef.getElementById === 'function'
            ? documentRef.getElementById(id)
            : null;
    }

    function clearAttendanceSessionPage(documentRef) {
        const endNotice = byId(documentRef, 'session-end-notice');
        if (endNotice) endNotice.hidden = true;
        for (const id of ['session-end-notice-title', 'session-end-notice-detail']) {
            const element = byId(documentRef, id);
            if (element) element.textContent = '';
        }
        const presentList = byId(documentRef, 'present-list');
        const absentList = byId(documentRef, 'absent-list');
        for (const id of ['pending-list', 'excused-list', 'session-completion-title', 'session-completion-counts', 'session-completion-detail', 'session-completion-context', 'session-accepted-opening', 'attendance-progress', 'attendance-filter-result', 'override-student-context', ...['pending', 'present', 'absent', 'excused'].flatMap(key => [`attendance-heading-${key}`, `attendance-filter-${key}`])]) {
            const element = byId(documentRef, id);
            if (element) element.replaceChildren();
        }
        const completion = byId(documentRef, 'session-completion');
        if (completion) completion.hidden = true;
        const clockReview = byId(documentRef, 'session-clock-review');
        if (clockReview) clockReview.hidden = true;
        const clockNote = byId(documentRef, 'session-clock-reason');
        if (clockNote) clockNote.value = '';
        const clockChecked = byId(documentRef, 'session-clock-reviewed');
        if (clockChecked) clockChecked.checked = false;
        const review = byId(documentRef, 'session-review-link');
        if (review) review.removeAttribute('href');
        const video = byId(documentRef, 'video-feed');
        const placeholder = byId(documentRef, 'video-placeholder');
        const overrideModal = byId(documentRef, 'override-modal');
        const countdown = byId(documentRef, 'countdown-timer');
        const status = byId(documentRef, 'session-status-badge');
        const availability = byId(documentRef, 'session-availability-badge');
        const displayStatus = byId(documentRef, 'attendance-display-status');

        if (presentList) presentList.innerHTML = '';
        if (absentList) absentList.innerHTML = '';
        if (video) {
            video.src = blankFeedSrc;
            video.removeAttribute('srcset');
        }
        if (placeholder) {
            placeholder.textContent = 'Camera feed hidden because your login session expired.';
            placeholder.style.display = 'block';
        }
        if (overrideModal) {
            overrideModal.style.display = 'none';
            overrideModal.setAttribute?.('aria-hidden', 'true');
        }
        const overrideName = byId(documentRef, 'override-student-name');
        if (overrideName) overrideName.textContent = '';
        ['override-student-id', 'override-reason', 'override-status', 'attendance-search', 'attendance-filter'].forEach(id => {
            const field = byId(documentRef, id);
            if (field) field.value = '';
        });
        if (countdown) countdown.textContent = '';
        if (status) {
            status.textContent = 'Session expired';
            status.className = 'badge badge--warning';
        }
        if (availability) {
            availability.textContent = 'Log in to resume';
            availability.className = 'badge badge--warning';
        }
        if (displayStatus) {
            displayStatus.textContent = 'Login expired';
            displayStatus.className = 'badge badge--warning';
        }

        [
            'btn-start-session',
            'btn-end-session',
            'btn-enable-display',
            'btn-copy-display',
            'btn-deactivate-display',
            'btn-maximize-feed',
            'btn-submit-override',
        ].forEach((id) => {
            const control = byId(documentRef, id);
            if (control) control.disabled = true;
        });
        const openDisplay = byId(documentRef, 'link-open-display');
        if (openDisplay) {
            openDisplay.hidden = true;
            openDisplay.removeAttribute('href');
        }
        const displayMessage = byId(documentRef, 'attendance-display-message');
        if (displayMessage) displayMessage.textContent = 'Log in again to manage this occurrence display.';
        const banner = byId(documentRef, 'session-expired-banner');
        if (banner) banner.hidden = false;
    }

    function handleSessionExpired(documentRef, payload) {
        clearAttendanceSessionPage(documentRef);
        const login = byId(documentRef, 'session-expired-login');
        const title = byId(documentRef, 'session-auth-title');
        const message = byId(documentRef, 'session-auth-message');
        const passwordResetRequired = Boolean(payload && payload.password_reset_required);
        if (passwordResetRequired) {
            if (title) title.textContent = 'Password reset required.';
            if (message) message.textContent = 'Sensitive screen data was cleared. Reset your password, then return to resume the open attendance occurrence.';
            if (login) {
                login.href = payload.password_reset_url || '/password_reset';
                login.textContent = 'Reset password to resume';
            }
        } else if (login && payload && payload.login_url) {
            login.href = payload.login_url;
        }
    }

    function handleAccessDenied(documentRef) {
        clearAttendanceSessionPage(documentRef);
        const text = {
            'session-status-badge': 'Access denied',
            'session-availability-badge': 'Access no longer available',
            'attendance-display-status': 'Access denied',
            'session-auth-title': 'Access denied.',
            'session-auth-message': 'You no longer have permission to view or change this attendance session. Private content was cleared. Contact your administrator or sign in with an authorized account.',
            'video-placeholder': 'Camera feed hidden because access was denied.',
            'attendance-display-message': 'Display links were hidden because access was denied.',
        };
        Object.entries(text).forEach(([id, message]) => { const element = byId(documentRef, id); if (element) element.textContent = message; });
        const login = byId(documentRef, 'session-expired-login');
        if (login) { login.href = '/login'; login.textContent = 'Return to sign in'; }
    }

    const api = { blankFeedSrc, clearAttendanceSessionPage, handleSessionExpired, handleAccessDenied };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    root.SessionAuth = api;
})(typeof window !== 'undefined' ? window : globalThis);
