"""Bound long-lived video responses so ordinary requests retain worker capacity.

The supported launchers use one threaded Waitress process. Limits are per
application process, not a promise about camera CPU or network throughput.
"""

import threading

from flask import current_app, jsonify, make_response, request


EXTENSION_KEY = "csd_stream_capacity"
DEFAULT_MAX_CLIENTS = 4  # Local launcher has eight request workers.
RESERVED_CONTROL_WORKERS = 4

# Flask runs response hooks in reverse registration order. Admission is installed
# before the launcher's security hook, so its refusal must retain policies that
# have already been applied. Do not copy video representation/transport headers.
_SECURITY_RESPONSE_HEADERS = (
    "Strict-Transport-Security", "Content-Security-Policy",
    "Content-Security-Policy-Report-Only", "X-Frame-Options",
    "X-Content-Type-Options", "Permissions-Policy", "Referrer-Policy",
    "Cross-Origin-Opener-Policy", "Cross-Origin-Embedder-Policy",
    "Cross-Origin-Resource-Policy",
)


class StreamCapacity:
    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.admitted = 0
        self.rejected = 0

    def acquire(self, limit):
        with self._lock:
            if limit is None or self.active >= limit:
                self.rejected += 1
                return None
            self.active += 1
            self.admitted += 1
        return _Lease(self)

    def snapshot(self, limit):
        with self._lock:
            return {"active_streams": self.active, "max_streams": limit,
                    "available_streams": max(0, limit - self.active) if limit is not None else None,
                    "admitted_total": self.admitted, "rejected_total": self.rejected,
                    "scope": "application_process"}


class _Lease:
    def __init__(self, capacity):
        self.capacity = capacity
        self.released = False

    def release(self):
        with self.capacity._lock:
            if not self.released:
                self.released = True
                self.capacity.active -= 1


class _LeasedIterator:
    """Release on exhaustion/error AND close-before-first-iteration."""

    def __init__(self, iterable, lease):
        self.iterator = iter(iterable)
        self.lease = lease
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        try:
            return next(self.iterator)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            close = getattr(self.iterator, "close", None)
            if close is not None:
                close()
        finally:
            self.lease.release()


def _limit(app):
    budget = app.config.get("STREAM_MAX_CLIENTS")
    if type(budget) is not int or not 0 <= budget <= 256:
        return None
    if app.config.get('PERSISTED_CAMERA_CAPACITY'):
        from services.camera_capacity import load_limits, CapacityUnavailable
        try:
            return min(budget, load_limits()['viewers'])
        except CapacityUnavailable:
            return None
    return budget


def configure_worker_capacity(app, worker_threads):
    """Retain four workers for controls in the supported threaded LAN server."""
    workers = int(worker_threads)
    if workers < 1:
        raise ValueError("Worker count must be positive.")
    app.config["STREAM_MAX_CLIENTS"] = max(0, workers - RESERVED_CONTROL_WORKERS)
    app.config["STREAM_RESERVED_CONTROL_WORKERS"] = min(workers, RESERVED_CONTROL_WORKERS)


def stream_capacity_snapshot():
    capacity = current_app.extensions.get(EXTENSION_KEY)
    if capacity is None:
        return {"available": False, "scope": "application_process"}
    result = capacity.snapshot(_limit(current_app))
    result['configuration_available'] = result['max_streams'] is not None
    result["reserved_control_workers"] = current_app.config.get(
        "STREAM_RESERVED_CONTROL_WORKERS", RESERVED_CONTROL_WORKERS)
    return result


def viewing_availability():
    """Safe advice only; the actual response still acquires atomically."""
    snapshot = stream_capacity_snapshot()
    if snapshot.get('max_streams') is None:
        return {'available': False, 'code': 'stream_capacity_unavailable',
                'message': 'Video viewing capacity could not be verified. Ask the Dean or IT Support to check it. Attendance status is separate.'}
    if not snapshot.get('available_streams'):
        return {'available': False, 'code': 'stream_capacity_full',
                'message': 'Video viewing capacity is full or paused. Close an unused video view before retrying. Attendance controls remain available.'}
    return {'available': True, 'code': 'stream_capacity_available', 'message': 'A viewing slot was available at this check; this is not camera readiness.'}


def install_stream_capacity(app, *, persisted_limits=False):
    if EXTENSION_KEY in app.extensions:
        return
    capacity = StreamCapacity()
    app.extensions[EXTENSION_KEY] = capacity
    app.config.setdefault("STREAM_MAX_CLIENTS", DEFAULT_MAX_CLIENTS)
    app.config['PERSISTED_CAMERA_CAPACITY'] = persisted_limits

    @app.after_request
    def admit_video_response(response):
        # Evaluate after endpoint authorization and validation. Secondary camera
        # responses register cleanup callbacks even before iteration begins.
        if request.method == "HEAD" or response.status_code != 200 or not (
            response.is_streamed and response.mimetype == "multipart/x-mixed-replace"
        ):
            return response
        limit = _limit(app)
        lease = capacity.acquire(limit)
        if lease is None:
            try:
                response.close()
            except Exception:
                app.logger.warning("Rejected video response cleanup did not finish normally.")
            refused = make_response(jsonify({
                "error": ("Video viewing capacity is full. Close another camera tab and retry. Attendance controls remain available."
                          if limit is not None else "Video viewing capacity configuration is unavailable. Ask the Dean or IT Support to check it. Attendance status is separate."),
                "code": "stream_capacity_full" if limit is not None else "stream_capacity_unavailable", "retry_after_seconds": 5,
            }), 503)
            for name in _SECURITY_RESPONSE_HEADERS:
                for value in response.headers.getlist(name):
                    refused.headers.add(name, value)
            refused.headers["Retry-After"] = "5"
            refused.headers["Cache-Control"] = "no-store"
            return refused
        try:
            response.response = _LeasedIterator(response.response, lease)
            response.call_on_close(lease.release)
        except BaseException:
            lease.release()
            response.close()
            raise
        return response
