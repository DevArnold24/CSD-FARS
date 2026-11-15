/* Local guidance only; the server remains authoritative. Never persist secrets. */
(function (root) {
    'use strict';
    function contains(ranges, code) {
        let low = 0, high = ranges.length - 1;
        while (low <= high) {
            const mid = (low + high) >> 1;
            if (code < ranges[mid][0]) high = mid - 1;
            else if (code > ranges[mid][1]) low = mid + 1;
            else return true;
        }
        return false;
    }
    function evaluate(value, policy) {
        const points = Array.from(value, character => character.codePointAt(0));
        return {
            length: points.length >= policy.minimum,
            upper: points.some(code => contains(policy.upper, code)),
            lower: points.some(code => contains(policy.lower, code)),
            digit: points.some(code => contains(policy.digit, code)),
            special: points.some(code => !contains(policy.alnum, code))
        };
    }
    function attach(panel) {
        const form = panel.closest('form');
        const password = form && form.querySelector('[name="password"]');
        if (!password) return;
        const visibility = form.querySelector('[data-password-visibility]');
        if (visibility) {
            visibility.addEventListener('click', () => {
                const reveal = password.type === 'password';
                password.type = reveal ? 'text' : 'password';
                visibility.textContent = reveal ? 'Hide' : 'Show';
                visibility.setAttribute('aria-label', `${reveal ? 'Hide' : 'Show'} temporary password`);
                visibility.setAttribute('aria-pressed', String(reveal));
            });
        }
        let policy;
        try { policy = JSON.parse(panel.querySelector('[data-password-policy]').textContent); }
        catch (_) { return; } // Static guidance and server validation remain usable.
        const confirm = form.querySelector('[name="confirm_password"]');
        const match = form.querySelector('#password-confirmation-hint');
        const summary = panel.querySelector('[data-password-summary]');
        const rows = panel.querySelectorAll('[data-password-rule]');
        const setText = (element, text) => {
            if (element && element.textContent !== text) element.textContent = text;
        };
        let attempted = false;
        function refresh(submitted) {
            attempted = attempted || !!submitted;
            submitted = attempted;
            const rules = evaluate(password.value, policy);
            const valid = Object.values(rules).every(Boolean);
            const entered = password.value.length > 0;
            rows.forEach(row => {
                const met = rules[row.dataset.passwordRule];
                row.dataset.met = entered || submitted ? String(met) : 'neutral';
                setText(row, `${entered || submitted ? (met ? '✓ ' : '○ ') : '○ '}${row.dataset.label}`);
            });
            const missing = Array.from(rows).filter(row => !rules[row.dataset.passwordRule]).map(row => row.dataset.label.toLowerCase());
            panel.dataset.valid = String(valid);
            setText(summary, valid ? 'Meets password requirements' :
                (entered || submitted ? `Still needed: ${missing.join(', ')}.` : 'Use a password that meets all five requirements.'));
            const matches = !!confirm && !!confirm.value && confirm.value === password.value;
            setText(match, !confirm || !confirm.value ? 'Enter the same new password again.' :
                (matches ? 'Passwords match' : 'Passwords do not match yet.'));
            if (submitted || entered) password.setAttribute('aria-invalid', String(!valid));
            else password.removeAttribute('aria-invalid');
            if (confirm) {
                if (submitted || confirm.value) confirm.setAttribute('aria-invalid', String(!matches));
                else confirm.removeAttribute('aria-invalid');
            }
            return {valid, matches};
        }
        form.addEventListener('submit', event => {
            const state = refresh(true);
            if (!state.valid || (confirm && !state.matches)) {
                event.preventDefault();
                (!state.valid ? password : confirm).focus();
            }
        }, true);
        [password, confirm].filter(Boolean).forEach(input => {
            ['input', 'change', 'focus'].forEach(name => input.addEventListener(name, () => refresh(false)));
        });
        // Password-manager autofill is not required to dispatch input/change.
        // Values stay only in these existing controls, never in storage or requests.
        const timer = root.setInterval(() => {
            if (!panel.isConnected) { root.clearInterval(timer); return; }
            if (!document.hidden) refresh(false);
        }, 500);
        root.addEventListener('pageshow', () => refresh(false));
        refresh(false);
    }
    if (typeof module !== 'undefined' && module.exports) module.exports = {evaluate, contains, attach};
    if (typeof document !== 'undefined') {
        const ready = () => document.querySelectorAll('[data-password-feedback]').forEach(attach);
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready);
        else ready();
    }
})(typeof window !== 'undefined' ? window : globalThis);
