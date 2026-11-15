(function initializeActionLock(root) {
    const lockedElements = new WeakMap();

    function tryLock(element, pendingText = '') {
        if (!element || lockedElements.has(element)) return false;
        lockedElements.set(element, {
            disabled: Boolean(element.disabled),
            textContent: element.textContent,
            ariaBusy: element.getAttribute?.('aria-busy')
        });
        element.disabled = true;
        element.setAttribute?.('aria-busy', 'true');
        if (pendingText) element.textContent = pendingText;
        return true;
    }

    function release(element) {
        const original = element ? lockedElements.get(element) : null;
        if (!original) return false;
        element.disabled = original.disabled;
        element.textContent = original.textContent;
        if (original.ariaBusy === null || original.ariaBusy === undefined) {
            element.removeAttribute?.('aria-busy');
        } else {
            element.setAttribute?.('aria-busy', original.ariaBusy);
        }
        lockedElements.delete(element);
        return true;
    }

    async function run(element, action, pendingText = '') {
        if (!tryLock(element, pendingText)) return { started: false };
        try {
            return { started: true, value: await action() };
        } finally {
            release(element);
        }
    }

    const api = { release, run, tryLock };
    root.ActionLock = api;
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
