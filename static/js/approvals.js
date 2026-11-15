document.addEventListener('DOMContentLoaded', () => {
    const alertBox = document.getElementById('approval-alert');
    const countStatus = document.getElementById('pending-approval-count');
    const tableContainer = document.getElementById('approval-table-container');
    const emptyState = document.getElementById('approval-empty-state');
    const reviewModal = document.getElementById('approval-review-modal');
    const reviewDialog = reviewModal.querySelector('[tabindex="-1"]');
    const reviewForm = document.getElementById('approval-review-form');
    const reviewStudent = document.getElementById('approval-review-student');
    const reviewError = document.getElementById('approval-review-error');
    const reviewNote = document.getElementById('approval-review-note');
    const reviewCancel = document.getElementById('approval-review-cancel');
    const reviewConfirm = document.getElementById('approval-review-confirm');
    let activeReviewButton = null;
    let reviewRequestToken = 0;
    let reviewPending = false;
    let approvalMutationPending = false;

    function reviewModalFocusableElements() {
        return Array.from(reviewModal.querySelectorAll(
            'button:not([disabled]), textarea:not([disabled]), input:not([disabled]):not([type="hidden"]), [href], [tabindex]:not([tabindex="-1"])'
        )).filter((element) => !element.hidden && element.offsetParent !== null);
    }

    function showAlert(message, isError = false, focus = false) {
        alertBox.hidden = false;
        alertBox.setAttribute('role', isError ? 'alert' : 'status');
        alertBox.setAttribute('aria-live', isError ? 'assertive' : 'polite');
        alertBox.style.backgroundColor = isError ? 'rgba(220, 38, 38, 0.08)' : 'rgba(22, 163, 74, 0.08)';
        alertBox.style.borderColor = isError ? 'var(--color-error)' : 'var(--color-success)';
        alertBox.style.color = isError ? 'var(--color-error)' : 'var(--color-success)';
        alertBox.textContent = message;
        if (focus && reviewModal.getAttribute('aria-hidden') !== 'false') {
            alertBox.focus();
        }
    }

    function updatePendingCount() {
        const count = document.querySelectorAll('#approval-request-body tr').length;
        countStatus.textContent = `${count} request${count === 1 ? '' : 's'} awaiting review`;
        tableContainer.hidden = count === 0;
        emptyState.hidden = count !== 0;
    }

    function openReviewModal(button) {
        if (reviewPending || approvalMutationPending) return;
        activeReviewButton = button;
        reviewStudent.textContent = button.dataset.studentName;
        reviewNote.value = '';
        reviewError.hidden = true;
        reviewError.textContent = '';
        reviewModal.style.display = 'flex';
        reviewModal.setAttribute('aria-hidden', 'false');
        reviewNote.focus();
    }

    function setReviewPending(pending) {
        reviewPending = pending;
        reviewModal.setAttribute('aria-busy', pending ? 'true' : 'false');
        reviewCancel.disabled = pending;
        reviewNote.disabled = pending;
        reviewConfirm.disabled = pending;
    }

    function closeReviewModal(restoreFocus = true, force = false) {
        if (reviewPending && !force) return false;
        const returnFocus = activeReviewButton;
        reviewModal.style.display = 'none';
        reviewModal.setAttribute('aria-hidden', 'true');
        reviewModal.removeAttribute('aria-busy');
        activeReviewButton = null;
        if (restoreFocus && returnFocus && document.contains(returnFocus)) {
            returnFocus.focus();
        }
        return true;
    }

    async function reviewRequest(button, note = '') {
        const isReject = button.dataset.action === 'reject';
        if (isReject && (reviewPending || activeReviewButton !== button)) return;
        if (approvalMutationPending) return;
        approvalMutationPending = true;
        const requestToken = isReject ? ++reviewRequestToken : null;
        const requestButton = button;
        const row = button.closest('tr');
        const actionButtons = Array.from(document.querySelectorAll('.approval-action'));
        actionButtons.forEach((actionButton) => {
            actionButton.disabled = true;
        });
        if (row) row.setAttribute('aria-busy', 'true');
        if (isReject) {
            setReviewPending(true);
            reviewDialog.focus();
        }
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 15000);

        try {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content');
            const response = await fetch(button.dataset.url, {
                method: 'POST',
                signal: controller.signal,
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken || ''
                },
                body: JSON.stringify({ review_note: note || '' })
            });
            const data = await AuthResponse.readJson(response);
            if (AuthResponse.redirectIfUnauthorized(response, data)) return;
            if (isReject && (
                requestToken !== reviewRequestToken ||
                activeReviewButton !== requestButton
            )) return;
            if (!response.ok) {
                const message = data.error || 'Unable to review request.';
                if (isReject) {
                    reviewError.textContent = message;
                    reviewError.hidden = false;
                    reviewError.focus();
                } else {
                    showAlert(message, true, true);
                }
                return;
            }

            if (isReject) {
                setReviewPending(false);
                closeReviewModal(false, true);
            }
            if (row) row.remove();
            updatePendingCount();
            showAlert(data.message || 'Request reviewed.', false, true);
        } catch (error) {
            console.error('Approval request failed:', error);
            const message = 'Unable to confirm the decision. Check Review History or refresh Pending Requests before retrying.';
            if (isReject && requestToken === reviewRequestToken && activeReviewButton === requestButton) {
                reviewError.textContent = message;
                reviewError.hidden = false;
                reviewError.focus();
            } else if (!isReject) {
                showAlert(message, true, true);
            }
        } finally {
            window.clearTimeout(timeout);
            if (row && document.contains(row)) {
                row.removeAttribute('aria-busy');
            }
            approvalMutationPending = false;
            actionButtons.forEach((actionButton) => {
                if (document.contains(actionButton)) actionButton.disabled = false;
            });
            if (isReject && requestToken === reviewRequestToken && activeReviewButton === requestButton) {
                setReviewPending(false);
            }
        }
    }

    document.querySelectorAll('.approval-action').forEach((button) => {
        button.addEventListener('click', () => {
            if (button.dataset.action === 'reject') {
                openReviewModal(button);
                return;
            }
            reviewRequest(button);
        });
    });

    reviewForm.addEventListener('submit', (event) => {
        event.preventDefault();
        if (activeReviewButton && !reviewPending) {
            reviewRequest(activeReviewButton, reviewNote.value.trim());
        }
    });
    reviewCancel.addEventListener('click', () => closeReviewModal());
    reviewModal.addEventListener('click', (event) => {
        if (event.target === reviewModal) closeReviewModal();
    });
    reviewModal.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            event.preventDefault();
            closeReviewModal();
            return;
        }
        if (event.key !== 'Tab') return;

        const focusable = reviewModalFocusableElements();
        if (!focusable.length) {
            event.preventDefault();
            reviewDialog.focus();
            return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (!focusable.includes(document.activeElement)) {
            event.preventDefault();
            (event.shiftKey ? last : first).focus();
        } else if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });
});
