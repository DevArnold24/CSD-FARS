// Accessible global modal utility for replacing native alert() and confirm().

let activeModal = null;
let returnFocus = null;
let fallbackNotice = null;

function showModalFallback(message, title, onDismiss = null) {
    if (!fallbackNotice || !fallbackNotice.region.isConnected) {
        const region = document.createElement('section');
        region.id = 'app-modal-fallback-notice';
        region.className = 'flash flash--warning';
        region.setAttribute('role', 'alert');
        const heading = document.createElement('h3');
        const text = document.createElement('p');
        text.style.whiteSpace = 'pre-wrap';
        const dismiss = document.createElement('button');
        dismiss.type = 'button';
        dismiss.className = 'btn btn--secondary';
        dismiss.textContent = 'Dismiss notice';
        region.appendChild(heading);
        region.appendChild(text);
        region.appendChild(dismiss);
        (document.querySelector('main') || document.body).prepend(region);
        fallbackNotice = {region, heading, text, dismiss, focusTarget: null, onDismiss: null};
        dismiss.addEventListener('click', () => {
            const notice = fallbackNotice;
            if (notice.region.hidden) return;
            const callback = notice.onDismiss;
            const focusTarget = notice.focusTarget;
            notice.region.hidden = true;
            // Clear one-time credentials before any callback can navigate away.
            notice.text.textContent = '';
            notice.heading.textContent = '';
            notice.onDismiss = null;
            notice.focusTarget = null;
            if (focusTarget?.isConnected && typeof focusTarget.focus === 'function') focusTarget.focus();
            if (callback) callback();
        });
    }
    const notice = fallbackNotice;
    if (!notice.focusTarget) notice.focusTarget = document.activeElement;
    notice.heading.textContent = title;
    notice.text.textContent = message;
    notice.onDismiss = onDismiss;
    notice.region.hidden = false;
    notice.dismiss.focus();
    notice.region.scrollIntoView({block: 'nearest'});
}

function focusableElements(modal) {
    return Array.from(modal.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    )).filter(element => !element.hidden && element.offsetParent !== null);
}

function closeModal(modal, callback = null) {
    if (!modal) return;
    modal.style.display = 'none';
    modal.setAttribute('aria-hidden', 'true');
    // One-time reset credentials must not remain in a dismissed dialog's DOM.
    const message = modal.querySelector('#global-alert-message, #global-confirm-message');
    if (message) message.textContent = '';
    modal._appModalCancel = null;
    modal._appModalDismissible = false;
    if (activeModal === modal) activeModal = null;
    const focusTarget = returnFocus;
    returnFocus = null;
    if (focusTarget && typeof focusTarget.focus === 'function' && focusTarget.isConnected) {
        focusTarget.focus();
    }
    if (callback) callback();
}

function openModal(modal, initialFocus, onCancel, dismissible = false) {
    if (activeModal && activeModal !== modal) closeModal(activeModal);
    returnFocus = document.activeElement;
    activeModal = modal;
    modal._appModalCancel = onCancel || null;
    modal._appModalDismissible = Boolean(dismissible);
    modal.style.display = 'flex';
    modal.setAttribute('aria-hidden', 'false');
    (initialFocus || modal.querySelector('[role="dialog"], [role="alertdialog"]'))?.focus();
}

function cancelActiveModal() {
    if (!activeModal || !activeModal._appModalDismissible) return;
    const modal = activeModal;
    const onCancel = modal._appModalCancel;
    closeModal(modal, onCancel);
}

function handleModalKeydown(event) {
    if (!activeModal) return;
    if (event.key === 'Escape') {
        event.preventDefault();
        cancelActiveModal();
        return;
    }
    if (event.key !== 'Tab') return;
    const focusable = focusableElements(activeModal);
    if (!focusable.length) {
        event.preventDefault();
        activeModal.querySelector('[role="dialog"], [role="alertdialog"]')?.focus();
        return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
    }
}

function handleModalBackdropClick(event) {
    if (activeModal && event.target === activeModal) cancelActiveModal();
}

if (typeof document !== 'undefined') {
    document.addEventListener('keydown', handleModalKeydown);
    document.addEventListener('click', handleModalBackdropClick);
}

window.AppModal = {
    alert(message, title = 'Notice', onDismiss = null) {
        const modal = document.getElementById('global-alert-modal');
        const titleEl = document.getElementById('global-alert-title');
        const msgEl = document.getElementById('global-alert-message');
        const btnOk = document.getElementById('global-alert-btn-ok');

        if (!modal || !titleEl || !msgEl || !btnOk) {
            console.error('AppModal: alert markup is unavailable. Showing an in-page notice.');
            showModalFallback(message, title, onDismiss);
            return;
        }

        titleEl.textContent = title;
        msgEl.textContent = message;
        const newBtnOk = btnOk.cloneNode(true);
        btnOk.parentNode.replaceChild(newBtnOk, btnOk);
        newBtnOk.disabled = false;
        const dismiss = () => closeModal(modal, onDismiss);
        newBtnOk.addEventListener('click', dismiss);
        openModal(modal, newBtnOk, null, false);
    },

    confirm(message, onConfirm, onCancel = null, title = 'Confirmation Required') {
        const modal = document.getElementById('global-confirm-modal');
        const titleEl = document.getElementById('global-confirm-title');
        const msgEl = document.getElementById('global-confirm-message');
        const btnOk = document.getElementById('global-confirm-btn-ok');
        const btnCancel = document.getElementById('global-confirm-btn-cancel');

        if (!modal || !titleEl || !msgEl || !btnOk || !btnCancel) {
            console.error('AppModal: confirmation markup is unavailable. The action was cancelled.');
            if (onCancel) onCancel();
            showModalFallback('This action was cancelled because its confirmation could not be displayed. Reload this page before trying again.', 'Action cancelled');
            return;
        }

        titleEl.textContent = title;
        msgEl.textContent = message;
        const newBtnOk = btnOk.cloneNode(true);
        btnOk.parentNode.replaceChild(newBtnOk, btnOk);
        const newBtnCancel = btnCancel.cloneNode(true);
        btnCancel.parentNode.replaceChild(newBtnCancel, btnCancel);
        newBtnOk.disabled = false;
        newBtnCancel.disabled = false;

        newBtnOk.addEventListener('click', () => {
            newBtnOk.disabled = true;
            closeModal(modal, onConfirm || null);
        });
        newBtnCancel.addEventListener('click', () => closeModal(modal, onCancel || null));
        openModal(modal, newBtnCancel, onCancel, true);
    },

    confirmSubmit(event, message, title = 'Confirmation Required') {
        event.preventDefault();
        const submitter = event.submitter || (event.currentTarget?.form ? event.currentTarget : null);
        const form = submitter?.form || event.target.closest?.('form') || event.target;
        const submitterAction = submitter?.getAttribute('formaction');
        this.confirm(message || 'Continue with this action?', () => {
            if (form && submitterAction) form.action = submitterAction;
            if (form && typeof form.submit === 'function') {
                form.submit();
            } else if (form) {
                HTMLFormElement.prototype.submit.call(form);
            }
        }, null, title);
        return false;
    }
};

if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
        cancelActiveModal,
        closeModal,
        focusableElements,
        handleModalBackdropClick,
        handleModalKeydown,
        openModal
    };
}
