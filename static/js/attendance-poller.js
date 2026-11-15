/** Attendance-only transport lifecycle. Retired/aborted work never publishes. */
(function (root, factory) {
    const api = factory();
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    root.AttendancePolling = api;
})(typeof window !== 'undefined' ? window : globalThis, () => {
    const states = new Set(['active', 'manual', 'inactive', 'starting', 'interrupted', 'stopping', 'unverified']);
    const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
    const nullableText = value => value === null || typeof value === 'string';
    function validSnapshot(data) {
        if (!object(data) || !states.has(data.session_state)
            || !['is_active', 'occurrence_open', 'resume_available'].every(key => typeof data[key] === 'boolean')
            || !(data.session_id === null || (Number.isInteger(data.session_id) && data.session_id > 0))
            || !Number.isInteger(data.publication_sequence) || data.publication_sequence < 0
            || !(data.current_frame_age_ms === null || (Number.isFinite(data.current_frame_age_ms) && data.current_frame_age_ms >= 0))
            || !nullableText(data.session_message) || !nullableText(data.session_end_reason)
            || !Array.isArray(data.present) || !Array.isArray(data.absent)) return false;
        if ('display_enabled' in data || 'display_url' in data) {
            if (typeof data.display_enabled !== 'boolean'
                || !(data.display_url === null || (typeof data.display_url === 'string'
                    && /^\/display\/session\/[A-Za-z0-9_-]{32,128}$/.test(data.display_url)))
                || (!data.display_enabled && data.display_url !== null)) return false;
        }
        const identity = row => object(row) && typeof row.student_id === 'string' && typeof row.name === 'string';
        return data.absent.every(identity) && data.present.every(row => identity(row)
            && typeof row.status === 'string' && typeof row.time === 'string' && nullableText(row.reason));
    }

    class AttendancePoller {
        constructor(options) {
            this.url = options.url;
            this.fetch = options.fetch || ((...args) => fetch(...args));
            this.onData = options.onData || (() => {});
            this.onState = options.onState || (() => {});
            this.onAuth = options.onAuth || (() => {});
            this.onAuthMetadata = options.onAuthMetadata || (() => {});
            this.now = options.now || (() => Date.now());
            this.setTimer = options.setTimeout || ((fn, delay) => setTimeout(fn, delay));
            this.clearTimer = options.clearTimeout || (id => clearTimeout(id));
            this.document = options.documentRef || document;
            this.window = options.windowRef || window;
            this.timeoutMs = options.timeoutMs || 8000;
            this.intervalMs = options.intervalMs || 2000;
            this.retryDelays = options.retryDelays || [2000, 5000, 10000, 15000];
            this.timer = null;
            this.request = null;
            this.generation = 0;
            this.running = false;
            this.terminal = false;
            this.authStatus = null;
            this.authMetadata = null;
            this.pageHidden = false;
            this.mutating = false;
            this.mutationUncertain = false;
            this.lastSuccess = null;
            this.failures = 0;
            this.state = 'connecting';
            this.onAvailability = () => this.availabilityChanged();
            this.onHide = () => { this.pageHidden = true; this.availabilityChanged(); };
            this.onShow = () => { this.pageHidden = false; this.availabilityChanged(); };
            this.document.addEventListener('visibilitychange', this.onAvailability);
            this.window.addEventListener('offline', this.onAvailability);
            this.window.addEventListener('online', this.onAvailability);
            this.window.addEventListener('pagehide', this.onHide);
            this.window.addEventListener('pageshow', this.onShow);
        }
        available() {
            return this.running && !this.terminal && !this.pageHidden
                && this.document.visibilityState !== 'hidden' && this.window.navigator?.onLine !== false;
        }
        emit(state) {
            this.state = state;
            this.onState({state, lastSuccess:this.lastSuccess, mutationUncertain:this.mutationUncertain, mutating:this.mutating});
        }
        cancelTimer() { if (this.timer !== null) this.clearTimer(this.timer); this.timer = null; }
        retire() {
            this.generation++;
            this.cancelTimer();
            const previous = this.request;
            this.request = null;
            if (previous) {
                if (previous.kind === 'mutation' && !this.terminal) this.mutationUncertain = true;
                this.clearTimer(previous.deadline);
                previous.cancel(new Error('Request retired'));
                if (!previous.readingAuthMetadata) previous.controller.abort();
            }
            this.mutating = false;
        }
        current(attempt) { return !this.terminal && this.request === attempt && attempt.generation === this.generation; }
        isCurrent(generation) { return !this.terminal && generation === this.generation; }
        start() {
            if (this.running || this.terminal) return;
            this.running = true;
            this.availabilityChanged();
        }
        stop() {
            this.running = false;
            this.retire();
            if (this.authMetadata) {
                this.authMetadata.cancel(new Error('Auth metadata stopped'));
                this.authMetadata = null;
            }
        }
        deny(status, response = null, attempt = null) {
            if (this.terminal && (this.authStatus === 403 || status !== 403)) return;
            if (this.authMetadata) this.authMetadata.cancel(new Error('Access state superseded'));
            this.terminal = true;
            this.authStatus = status;
            this.running = false;
            if (status === 401 && response && attempt) attempt.readingAuthMetadata = true;
            this.retire();
            // Clear private content at headers, before attempting any body.
            this.onAuth(status);
            if (attempt?.readingAuthMetadata) this.readAuthMetadata(response, attempt);
        }
        async readAuthMetadata(response, attempt) {
            const pending = {generation:this.generation, attempt};
            this.authMetadata = pending;
            const deadline = new Promise((_resolve, reject) => {
                pending.cancel = error => { attempt.controller.abort(); reject(error); };
                pending.timer = this.setTimer(() => pending.cancel(new Error('Auth metadata timed out')), this.timeoutMs);
            });
            try {
                const data = await Promise.race([response.json(), deadline]);
                if (this.authMetadata === pending && this.generation === pending.generation
                    && this.authStatus === 401 && object(data) && data.password_reset_required === true) {
                    // Auth metadata can only refine the already-locked screen.
                    // Use the known local route, never a body-supplied URL.
                    this.onAuthMetadata({password_reset_required:true, password_reset_url:'/password_reset'});
                }
            } catch (_error) {
                // HTML, malformed or hanging bodies retain generic sign-in guidance.
            } finally {
                this.clearTimer(pending.timer);
                if (this.authMetadata === pending) this.authMetadata = null;
                attempt.readingAuthMetadata = false;
                attempt.controller.abort();
            }
        }
        availabilityChanged() {
            if (this.authMetadata && (this.pageHidden || this.document.visibilityState === 'hidden'
                || this.window.navigator?.onLine === false)) {
                this.generation++;
                this.authMetadata.cancel(new Error('Auth metadata paused'));
                this.authMetadata = null;
            }
            if (!this.running || this.terminal) return;
            if (!this.available()) {
                this.retire();
                this.emit(this.window.navigator?.onLine === false ? 'disconnected' : 'paused');
            } else if (!this.request && !this.mutating) {
                this.cancelTimer();
                this.poll();
            }
        }
        schedule(delay) {
            this.cancelTimer();
            if (this.available() && !this.mutating) {
                this.timer = this.setTimer(() => { this.timer = null; this.poll(); }, delay);
            }
        }
        begin(kind) {
            const attempt = {kind, generation:this.generation, controller:new AbortController()};
            attempt.cancelled = new Promise((_resolve, reject) => { attempt.cancel = reject; });
            this.request = attempt;
            return attempt;
        }
        async exchange(attempt, url, options = {}) {
            const deadline = new Promise((_resolve, reject) => {
                attempt.deadline = this.setTimer(() => reject(new Error('Request timed out')), this.timeoutMs);
            });
            const operation = (async () => {
                const response = await this.fetch(url, {...options, signal:attempt.controller.signal, cache:'no-store'});
                if (!this.current(attempt)) throw new Error('Request retired');
                if (response.status === 401 || response.status === 403) {
                    this.deny(response.status, response, attempt);
                    throw new Error('Access ended');
                }
                const data = await response.json();
                if (!this.current(attempt)) throw new Error('Request retired');
                return {response, data, generation:attempt.generation};
            })();
            // The deadline covers headers AND body parsing. Browsers normally
            // cancel fetch; the identity check also rejects abort-ignoring
            // transports and late promise completions from retired attempts.
            return Promise.race([operation, deadline, attempt.cancelled]);
        }
        finish(attempt) {
            this.clearTimer(attempt.deadline);
            if (this.current(attempt)) this.request = null;
            if (!attempt.readingAuthMetadata) attempt.controller.abort();
        }
        async poll() {
            if (!this.available() || this.request || this.mutating) return;
            this.cancelTimer();
            const attempt = this.begin('poll');
            if (this.state !== 'connected') this.emit(this.lastSuccess === null && !this.failures ? 'connecting' : 'reconnecting');
            let delay = this.intervalMs;
            try {
                const result = await this.exchange(attempt, this.url);
                if (!this.current(attempt)) return;
                if (!result.response.ok || !validSnapshot(result.data)) throw new Error('Invalid attendance response');
                this.lastSuccess = this.now();
                this.failures = 0;
                this.onData(result.data);
                this.emit('connected');
            } catch (_error) {
                if (!this.current(attempt)) return;
                delay = this.retryDelays[Math.min(this.failures++, this.retryDelays.length - 1)];
                this.emit('disconnected');
            } finally {
                const wasCurrent = this.current(attempt);
                this.finish(attempt);
                if (wasCurrent) this.schedule(delay);
            }
        }
        async mutate(url, options, validate = object) {
            if (!this.available() || this.mutating || this.mutationUncertain) return null;
            this.retire();
            this.mutating = true;
            const attempt = this.begin('mutation');
            this.emit('updating');
            try {
                const result = await this.exchange(attempt, url, options);
                if (!this.current(attempt)) return null;
                if (!object(result.data) || result.response.status >= 500
                    || (result.response.ok && !validate(result.data))) throw new Error('Unknown action outcome');
                return result;
            } catch (_error) {
                if (!this.current(attempt)) return null;
                this.mutationUncertain = true;
                this.emit('disconnected');
                return {uncertain:true, generation:this.generation};
            } finally {
                const wasCurrent = this.current(attempt);
                this.finish(attempt);
                if (wasCurrent) {
                    this.mutating = false;
                    // A timer, not an inline GET, lets the caller apply its
                    // synchronous action result before the next observation.
                    this.schedule(0);
                }
            }
        }
    }
    return {AttendancePoller, validSnapshot};
});
