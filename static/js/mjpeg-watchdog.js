/** Conservative recovery for long-lived MJPEG <img> responses. */
(function exposeMjpegWatchdog(root, factory) {
    const api = factory();
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    }
    if (root) {
        root.CsdMjpegFeed = api;
    }
})(typeof globalThis !== 'undefined' ? globalThis : this, () => {
    const DEFAULT_RETRY_DELAYS_MS = Object.freeze([1000, 2000, 5000, 10000, 15000]);

    function cacheBustedUrl(baseUrl, token) {
        const value = String(baseUrl || '');
        const hashIndex = value.indexOf('#');
        const base = hashIndex >= 0 ? value.slice(0, hashIndex) : value;
        const hash = hashIndex >= 0 ? value.slice(hashIndex) : '';
        const separator = base.includes('?') ? '&' : '?';
        return `${base}${separator}_mjpeg=${encodeURIComponent(String(token))}${hash}`;
    }

    class MjpegFeedWatchdog {
        constructor(image, options = {}) {
            if (!image || typeof image.addEventListener !== 'function') {
                throw new TypeError('An MJPEG image element is required.');
            }
            if (!options.baseUrl) {
                throw new TypeError('An MJPEG base URL is required.');
            }

            this.image = image;
            this.baseUrl = String(options.baseUrl);
            this.isActive = typeof options.isActive === 'function' ? options.isActive : () => true;
            this.onStateChange = typeof options.onStateChange === 'function'
                ? options.onStateChange
                : () => {};
            this.stallAfterMs = Math.max(1000, Number(options.stallAfterMs) || 15000);
            const configuredDelays = Array.isArray(options.retryDelaysMs)
                ? options.retryDelaysMs.filter(delay => Number.isFinite(delay) && delay >= 0)
                : [];
            this.retryDelaysMs = configuredDelays.length
                ? configuredDelays.map(Number)
                : DEFAULT_RETRY_DELAYS_MS;

            this.now = typeof options.now === 'function' ? options.now : () => performance.now();
            // Native browser timers must not receive this watchdog as their receiver.
            this.setTimer = typeof options.setTimeout === 'function' ? options.setTimeout : (fn, delay) => setTimeout(fn, delay);
            this.clearTimer = typeof options.clearTimeout === 'function' ? options.clearTimeout : id => clearTimeout(id);
            this.documentRef = options.documentRef !== undefined
                ? options.documentRef
                : (typeof document !== 'undefined' ? document : null);
            this.windowRef = options.windowRef !== undefined
                ? options.windowRef
                : (typeof window !== 'undefined' ? window : null);

            this.running = false;
            this.destroyed = false;
            this.timer = null;
            this.timerKind = null;
            this.retryAttempt = 0;
            this.maxRetries = 3;
            this.episodeLimitMs = 45000;
            this.episodeStartedAt = null;
            this.stableSince = null;
            this.exhausted = false;
            this.awaitingImageLoad = false;
            this.lastSequence = null;
            this.lastProgressAt = null;
            this.state = 'stopped';

            this.handleLoad = () => this.noteImageLoad();
            this.handleError = () => this.scheduleRetry('stream-error');
            this.handleVisibility = () => this.handleAvailabilityChange();
            this.handleOnline = () => this.handleAvailabilityChange();
            this.handleOffline = () => this.handleAvailabilityChange();

            image.addEventListener('load', this.handleLoad);
            image.addEventListener('error', this.handleError);
            this.documentRef?.addEventListener?.('visibilitychange', this.handleVisibility);
            this.windowRef?.addEventListener?.('online', this.handleOnline);
            this.windowRef?.addEventListener?.('offline', this.handleOffline);
        }

        canConnect() {
            if (!this.running || this.destroyed || !this.isActive()) return false;
            if (this.documentRef?.visibilityState === 'hidden') return false;
            return this.windowRef?.navigator?.onLine !== false;
        }

        emitState(state, detail = {}) {
            this.state = state;
            this.onStateChange(state, detail);
        }

        cancelTimer() {
            if (this.timer !== null) {
                this.clearTimer(this.timer);
                this.timer = null;
                this.timerKind = null;
            }
        }

        start(options = {}) {
            if (this.destroyed) return;
            this.cancelTimer();
            this.running = true;
            this.retryAttempt = 0;
            this.episodeStartedAt = null;
            this.stableSince = null;
            this.exhausted = false;
            this.awaitingImageLoad = false;
            this.lastSequence = null;
            this.lastProgressAt = this.now();
            if (options.reload === false) {
                this.emitState('watching');
                this.armWatchdog();
            } else {
                this.reloadStream(false);
            }
        }

        stop() {
            this.running = false;
            this.cancelTimer();
            this.retryAttempt = 0;
            this.awaitingImageLoad = false;
            this.lastSequence = null;
            this.lastProgressAt = null;
            this.emitState('stopped');
        }

        destroy() {
            if (this.destroyed) return;
            this.stop();
            this.destroyed = true;
            this.image.removeEventListener?.('load', this.handleLoad);
            this.image.removeEventListener?.('error', this.handleError);
            this.documentRef?.removeEventListener?.('visibilitychange', this.handleVisibility);
            this.windowRef?.removeEventListener?.('online', this.handleOnline);
            this.windowRef?.removeEventListener?.('offline', this.handleOffline);
        }

        noteImageLoad() {
            if (!this.canConnect() || this.exhausted) return;
            this.awaitingImageLoad = false;
            this.lastProgressAt = this.now();
            if (this.stableSince === null) this.stableSince = this.now();
            this.emitState('connected');
            this.armWatchdog();
        }

        observeProgress(sequence, frameAgeMs) {
            if (!this.running || this.destroyed || !this.isActive() || this.exhausted) return;

            const now = this.now();
            const numericSequence = Number(sequence);
            const hasSequence = Number.isFinite(numericSequence) && numericSequence >= 0;
            const hasPublishedSequence = hasSequence && numericSequence > 0;
            const sequenceAdvanced = hasPublishedSequence
                && numericSequence !== this.lastSequence;
            if (hasSequence) this.lastSequence = numericSequence;

            const numericAge = frameAgeMs === null || frameAgeMs === undefined
                ? NaN
                : Number(frameAgeMs);
            // Some status surfaces historically reported age=0 before the
            // first publication. Sequence zero is not evidence of progress.
            const hasAge = hasPublishedSequence
                && Number.isFinite(numericAge)
                && numericAge >= 0;
            if (hasAge) {
                const inferredProgressAt = now - numericAge;
                if (this.lastProgressAt === null || inferredProgressAt > this.lastProgressAt) {
                    this.lastProgressAt = inferredProgressAt;
                }
            } else if (sequenceAdvanced) {
                this.lastProgressAt = now;
            }

            if (!hasAge && !sequenceAdvanced) return;

            if (hasAge && numericAge >= this.stallAfterMs) {
                this.scheduleRetry('stalled-publication');
                return;
            }

            // Healthy producer telemetry cannot prove that this browser's
            // broken <img> response recovered. Preserve a pending retry (or
            // post-reload confirmation timeout) until the image fires load.
            if (this.awaitingImageLoad || this.timerKind === 'retry') return;

            if (sequenceAdvanced || (hasAge && numericAge < this.stallAfterMs)) {
                if (this.stableSince !== null && now - this.stableSince >= 10000) {
                    this.retryAttempt = 0;
                    this.episodeStartedAt = null;
                }
                this.emitState('connected');
            }
            this.armWatchdog();
        }

        armWatchdog() {
            this.cancelTimer();
            if (this.exhausted) return;
            if (!this.canConnect()) {
                if (this.running && this.isActive()) this.emitState('paused');
                return;
            }
            const progressAt = this.lastProgressAt === null ? this.now() : this.lastProgressAt;
            let remaining = Math.max(250, this.stallAfterMs - (this.now() - progressAt));
            if (this.episodeStartedAt !== null) remaining = Math.min(remaining, Math.max(0, this.episodeLimitMs - (this.now() - this.episodeStartedAt)));
            this.timer = this.setTimer(() => {
                this.timer = null;
                this.timerKind = null;
                this.scheduleRetry('stalled-stream');
            }, remaining);
            this.timerKind = 'watchdog';
        }

        scheduleRetry(reason) {
            if (!this.running || this.destroyed || !this.isActive() || this.exhausted) return;
            if (this.timer !== null && this.timerKind === 'retry') return;
            this.awaitingImageLoad = true;
            this.stableSince = null;
            this.cancelTimer();
            if (this.episodeStartedAt === null) this.episodeStartedAt = this.now();
            if (this.retryAttempt >= this.maxRetries || this.now() - this.episodeStartedAt >= this.episodeLimitMs) {
                this.exhausted = true;
                this.image.removeAttribute?.('src');
                this.emitState('exhausted', {reason, attempts: this.retryAttempt});
                return;
            }
            if (!this.canConnect()) {
                this.emitState('paused', { reason });
                return;
            }

            const delayIndex = Math.min(this.retryAttempt, this.retryDelaysMs.length - 1);
            const delayMs = Math.min(this.retryDelaysMs[delayIndex], this.episodeLimitMs - (this.now() - this.episodeStartedAt));
            this.emitState('retrying', { reason, delayMs });
            this.timer = this.setTimer(() => {
                this.timer = null;
                this.timerKind = null;
                this.reloadStream(true);
            }, delayMs);
            this.timerKind = 'retry';
        }

        reloadStream(isRetry) {
            if (this.exhausted) return;
            if (isRetry && this.episodeStartedAt !== null && this.now() - this.episodeStartedAt >= this.episodeLimitMs) {
                this.scheduleRetry('recovery-timeout');
                return;
            }
            if (!this.canConnect()) {
                if (this.running && this.isActive()) this.emitState('paused');
                return;
            }
            const now = this.now();
            if (isRetry) this.retryAttempt += 1;
            this.awaitingImageLoad = true;
            this.lastProgressAt = now;
            this.image.src = cacheBustedUrl(this.baseUrl, `${now}-${this.retryAttempt}`);
            this.emitState(isRetry ? 'reconnecting' : 'connecting', {
                attempt: this.retryAttempt,
            });
            this.armWatchdog();
        }

        handleAvailabilityChange() {
            if (!this.running || this.destroyed || !this.isActive() || this.exhausted) return;
            if (!this.canConnect()) {
                this.cancelTimer();
                this.emitState('paused');
                return;
            }
            if (this.awaitingImageLoad) {
                this.scheduleRetry('connection-resumed');
                return;
            }
            const progressAt = this.lastProgressAt === null ? 0 : this.lastProgressAt;
            if (this.now() - progressAt >= this.stallAfterMs) {
                this.scheduleRetry('connection-resumed');
            } else {
                this.armWatchdog();
            }
        }
    }

    return { MjpegFeedWatchdog, cacheBustedUrl, DEFAULT_RETRY_DELAYS_MS };
});
