(function initializeAuthResponse(root, factory) {
    const api = factory(root);
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    if (root) root.AuthResponse = api;
}(typeof window !== 'undefined' ? window : globalThis, (root) => {
    async function readJson(response) {
        if (!response || typeof response.json !== 'function') return {};
        try {
            const payload = await response.json();
            return payload && typeof payload === 'object' ? payload : {};
        } catch (error) {
            return {};
        }
    }

    function redirectIfUnauthorized(response, payload = {}) {
        if (!response || response.status !== 401) return false;
        const data = payload && typeof payload === 'object' ? payload : {};
        const destination = data.password_reset_url || data.login_url || '/login';
        if (root && root.location) root.location.href = destination;
        return true;
    }

    return Object.freeze({
        readJson,
        redirectIfUnauthorized
    });
}));
