"""Supervised attendance-session worker management."""

import multiprocessing
import multiprocessing.shared_memory as shared_memory
import threading
import time
import math
import queue
import uuid
from contextlib import contextmanager

import config
from core import get_logger
from core.ai_pipeline import live_session_loop


logger = get_logger("session_manager")


WORKER_GRACEFUL_STOP_TIMEOUT_SECONDS = 3.0
WORKER_TERMINATE_TIMEOUT_SECONDS = 2.0
SECONDARY_CLEANUP_RETRY_SECONDS = 1.0
MANAGER_RPC_TIMEOUT_SECONDS = 0.5


class ManagerUnavailableError(OSError):
    """Manager control is unverified; existing owners must remain reserved."""


class _ManagerControl:
    """One bounded parent RPC lane, with no retries after uncertain completion.

    Manager proxies have no response deadline. A stalled server must not hold
    the application's lifecycle lock forever. One daemon performs parent IPC;
    a timeout permanently closes this lane, so polling cannot leak more threads
    or repeat a possibly completed mutation. Worker IPC remains unchanged.
    """

    def __init__(self):
        self._queue = queue.Queue(maxsize=1)
        self._failed = threading.Event()
        self._call_lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True, name="csd-manager-control").start()

    @property
    def available(self):
        return not self._failed.is_set()

    def _run(self):
        while not self._failed.is_set():
            function, done, result = self._queue.get()
            try:
                result.append((True, function()))
            except Exception as exc:
                result.append((False, exc))
            finally:
                done.set()
                # An idle RPC lane must not retain a completed closure/result
                # and thereby keep its context, proxy and Manager alive.
                del function, done, result

    def call(self, function, *, timeout=MANAGER_RPC_TIMEOUT_SECONDS):
        if not self._call_lock.acquire(timeout=timeout):
            raise ManagerUnavailableError("Session control is busy; ownership remains reserved.")
        try:
            if self._failed.is_set():
                raise ManagerUnavailableError("Session control is unavailable; ownership remains reserved.")
            done, result = threading.Event(), []
            self._queue.put_nowait((function, done, result))
            if not done.wait(timeout):
                self._failed.set()
                raise ManagerUnavailableError("Session control timed out; ownership remains reserved.")
            ok, value = result[0]
            if not ok:
                if isinstance(value, (OSError, EOFError, BrokenPipeError)):
                    self._failed.set()
                    raise ManagerUnavailableError("Session control is unavailable; ownership remains reserved.") from None
                raise value
            return value
        finally:
            self._call_lock.release()


class _BoundedEvent:
    def __init__(self, event, control):
        self._event, self._control = event, control

    def set(self):
        return self._control.call(self._event.set)

    def is_set(self):
        return self._control.call(self._event.is_set)


class CameraBusyError(RuntimeError):
    """A camera already has an owner whose cleanup is not confirmed."""


class CameraLease:
    """A bounded secondary camera owner, including suspended HTTP streams.

    Expiry actively stops capture on its own timer. Ownership survives expiry
    when native cleanup cannot be confirmed; another request must never open a
    second handle simply because the browser stopped consuming its response.
    """

    def __init__(self, manager, camera_id, purpose, ttl):
        self.manager = manager
        self.camera_id = camera_id
        self.purpose = purpose
        self.capture = None
        self.closed = False
        self.preview_user_id = None
        self.preview_token = None
        self.preview_frame_at = None
        self.cleanup_attempts = 0
        self.expires_at = time.monotonic() + ttl
        self.camera_identity = None
        self.stop_event = threading.Event()
        self._lock = threading.RLock()
        self._timer = threading.Timer(ttl, self.close)
        self._timer.daemon = True

    @property
    def cleanup_confirmed(self):
        return self.closed and (
            self.capture is None or self.capture.cleanup_confirmed is True
        )

    def start(self, capture):
        with self._lock:
            if self.closed:
                return False
            self.capture = capture
            return capture.start()

    def close(self):
        with self._lock:
            self.closed = True
            self.stop_event.set()
            self._timer.cancel()
            self.cleanup_attempts += 1
            if self.capture is not None:
                try:
                    self.capture.stop(timeout=2.0)
                except Exception:
                    logger.error("Secondary camera cleanup failed; its camera remains reserved.")
            confirmed = self.cleanup_confirmed
            if not confirmed:
                # A timed-out release can become containable shortly later.
                # Retry independently of browser consumption or future polls;
                # retain the reservation throughout every cleanup attempt.
                self._timer = threading.Timer(SECONDARY_CLEANUP_RETRY_SECONDS, self.close)
                self._timer.daemon = True
                self._timer.start()
        if confirmed:
            self.manager._release_camera_lease(self)
        return confirmed


class SessionContextDict:
    """Dictionary proxy that syncs heartbeats and metadata via a multiprocessing.Manager().dict() proxy.
    Includes a custom _set_local(self, key, value) method to store non-serializable objects locally."""

    def __init__(self, manager_dict, control=None):
        self._shared = manager_dict
        self._local = {}
        self._control = control

    def _call(self, function):
        return self._control.call(function) if self._control is not None else function()

    def __bool__(self):
        # Existence of a registered context is a local ownership fact; checking
        # it must not require a live Manager or imply that cleanup succeeded.
        return True

    def _set_local(self, key, value):
        self._local[key] = value

    def __getitem__(self, key):
        if key in self._local:
            return self._local[key]
        return self._call(lambda: self._shared[key])

    def __setitem__(self, key, value):
        if key.startswith("_"):
            self._local[key] = value
        else:
            self._call(lambda: self._shared.__setitem__(key, value))
            if key in self._local:
                self._local[key] = value

    def __contains__(self, key):
        return key in self._local or self._call(lambda: key in self._shared)

    def get(self, key, default=None):
        if key in self._local:
            return self._local[key]
        return self._call(lambda: self._shared.get(key, default))

    def setdefault(self, key, default=None):
        if key in self._local:
            return self._local[key]
        if key.startswith("_"):
            self._local[key] = default
            return self._local[key]
        return self._call(lambda: self._shared.setdefault(key, default))

    def pop(self, key, default=None):
        if key in self._local:
            return self._local.pop(key, default)
        return self._call(lambda: self._shared.pop(key, default))

    def __delitem__(self, key):
        if key in self._local:
            del self._local[key]
        else:
            self._call(lambda: self._shared.__delitem__(key))

    def update(self, other=None, **kwargs):
        """Update proxy-backed values with one manager round trip.

        Per-frame telemetry contains several related counters. Sending each
        counter through ``__setitem__`` separately makes the recognition
        worker wait on the manager process for every field.
        """
        values = {}
        if other is not None:
            values.update(dict(other))
        values.update(kwargs)

        local_values = {
            key: value for key, value in values.items() if key.startswith("_")
        }
        shared_values = {
            key: value for key, value in values.items() if not key.startswith("_")
        }
        self._local.update(local_values)
        if shared_values:
            self._call(lambda: self._shared.update(shared_values))

    def keys(self):
        return set(self._local.keys()) | set(self._call(lambda: self._shared.keys()))

    def values(self):
        return [self[k] for k in self.keys()]

    def items(self):
        return [(k, self[k]) for k in self.keys()]

    def __len__(self):
        return len(self.keys())

    def __iter__(self):
        return iter(self.keys())

    def __getstate__(self):
        return {"_shared": self._shared}

    def __setstate__(self, state):
        self._shared = state["_shared"]
        self._local = {}
        self._control = None


def _test_dummy_loop(class_id: int, session_context):
    """A dummy loop function used in tests when live_session_loop is mocked."""
    import time
    session_context["last_seen"] = time.time()
    session_context["started_at"] = time.time()
    
    stop_event = session_context.get("stop_event")
    while not (stop_event and stop_event.is_set()):
        session_context["last_seen"] = time.time()
        time.sleep(0.01)


def _test_dummy_sandbox_loop(camera_id, session_context):
    """A dummy loop function used in tests when sandbox_session_loop is mocked."""
    import time
    session_context["last_seen"] = time.time()
    session_context["started_at"] = time.time()
    session_context["stats"] = {
        "detection_ms": 12.5,
        "liveness_ms": 5.2,
        "embedding_ms": 18.1,
        "matching_ms": 3.4,
        "total_ms": 39.2,
        "fps": 25.0,
        "best_distance": 0.32,
        "second_distance": 0.55,
        "margin": 0.23,
        "ratio": 0.58,
        "match_info": {"accepted": True, "best_index": 0, "best_distance": 0.32}
    }
    session_context["pipeline_latency"] = {
        "detection_ms": 12.5,
        "liveness_ms": 5.2,
        "embedding_ms": 18.1,
        "matching_ms": 3.4,
        "total_ms": 39.2
    }
    stop_event = session_context.get("stop_event")
    while not (stop_event and stop_event.is_set()):
        session_context["last_seen"] = time.time()
        time.sleep(0.01)



class ManagedSessionDict(dict):
    """Dictionary that stops workers when tests or cleanup code clear it."""

    def __init__(self, owner):
        super().__init__()
        self._owner = owner

    def clear(self):
        failed = [key for key in list(self) if not self._owner.stop(key)]
        if failed:
            raise RuntimeError(
                f"Session workers did not stop cleanly: {failed}"
            )


class SessionManager:
    """Start, stop, and observe live attendance session workers."""

    def __init__(self):
        self._lifecycle_lock = threading.RLock()
        self.active_sessions = ManagedSessionDict(self)
        self.recent_stops = {}
        self._camera_leases = {}
        self._manager = None
        self._manager_control = None
        self._run_namespace = uuid.uuid4().hex
        self.confirmed_lifecycle_tokens = set()

    def _new_context(self, values):
        """Allocate only new IPC objects and keep immutable ownership locally."""
        if self._manager_control is None:
            self._manager_control = _ManagerControl()
        control = self._manager_control
        if not control.available:
            raise CameraBusyError("Session control is unavailable. Existing owners remain reserved; stop the application safely before retrying.")
        if self._manager is None:
            self._manager = control.call(multiprocessing.Manager, timeout=5.0)
        manager = self._manager
        stop_event, lock, preview_lock, shared = control.call(
            lambda: (manager.Event(), manager.Lock(), manager.Lock(), manager.dict())
        )
        # A new run and a new allocation both contribute randomness. A copied
        # installation/database identifier can never select another run's block.
        for _attempt in range(4):
            shm_name = f"csd_frame_{self._run_namespace}_{uuid.uuid4().hex}"
            try:
                shm = shared_memory.SharedMemory(name=shm_name, create=True, size=10 * 1024 * 1024)
                break
            except FileExistsError:
                # The colliding block is unknown. Never attach, zero or unlink it.
                continue
        else:
            raise RuntimeError("Could not allocate a new session frame buffer.")
        context = SessionContextDict(shared, control=control)
        initial = {
            **values, "shm_name": shm_name, "stop_event": stop_event,
            "lock": lock, "preview_lock": preview_lock,
            "preview_buffer_offset": len(shm.buf) // 2,
        }
        try:
            context.update(initial)
        except Exception:
            shm.close()
            shm.unlink()
            raise
        shm.buf[:4] = bytes(4)
        offset = initial["preview_buffer_offset"]
        shm.buf[offset:offset + 4] = bytes(4)
        for key in ("camera_id", "camera_identity", "attendance_session_id", "startup_attempt",
                    "startup_deadline", "worker_mode", "shm_name", "started_at", "lifecycle_owner_token"):
            context._set_local(key, initial.get(key))
        context._set_local("stop_event", _BoundedEvent(stop_event, control))
        context._set_local("shm", shm)
        return context

    @staticmethod
    def _worker_alive(context):
        """Return a definitive worker liveness value when one is available."""
        if not context:
            return None
        worker = context.get("worker")
        if worker is None:
            return None
        is_alive = getattr(worker, "is_alive", None)
        if not callable(is_alive):
            return None
        try:
            return bool(is_alive())
        except Exception:
            logger.exception("Failed to inspect session worker liveness.")
            return None

    def _reap_dead_session(self, class_id):
        """Remove one definitively stopped worker and release its resources.

        Camera workers can terminate themselves after a source or pipeline
        failure.  Keeping that dead context registered makes the camera appear
        owned forever and prevents a safe restart.  Ambiguous worker objects
        remain registered; only a definitive ``is_alive() is False`` result is
        reaped.
        """
        context = self.active_sessions.get(class_id)
        if context is None or self._worker_alive(context) is not False:
            return context

        if not self._contain_capture_helper(context):
            return context

        if self.active_sessions.get(class_id) is context:
            self.active_sessions.pop(class_id, None)
        stop_info = self._stop_info_from_context(context)
        if stop_info:
            self.recent_stops[class_id] = stop_info
        self._release_context_resources(class_id, context)
        logger.info("Reaped stopped session worker for %s.", class_id)
        return None

    def is_running(self, class_id):
        with self._lifecycle_lock:
            return self._reap_dead_session(class_id) is not None

    def worker_state(self, class_id):
        """Observe execution without equating retained ownership with liveness.

        Call only after authorizing the operator. Definitively exited workers
        are reaped using the same native-containment gate as camera ownership;
        unknown liveness and unfinished cleanup continue to reserve the camera.
        """
        with self._lifecycle_lock:
            context = self.active_sessions.get(class_id)
            if context is None:
                return "missing"
            try:
                alive = self._worker_alive(context)
                if alive is False:
                    return "missing" if self._reap_dead_session(class_id) is None else "stopping"
                if alive is None:
                    return "unverified"
                stop_event = context.get("stop_event")
                return "stopping" if stop_event and stop_event.is_set() else "running"
            except (OSError, EOFError, BrokenPipeError):
                # Broken manager IPC is not evidence of completed cleanup.
                self.active_sessions.setdefault(class_id, context)
                return "unverified"

    def get_camera_owner(self, camera_id):
        """
        Returns the session key ("sandbox" or integer class_id) if camera_id
        is currently in use by an active session, or None if free.
        """
        if camera_id is None:
            return None

        def _norm(val):
            if val is None:
                return None
            try:
                return int(val)
            except (ValueError, TypeError):
                return str(val)

        target_norm = _norm(camera_id)

        with self._lifecycle_lock:
            from services.camera_identity import camera_identity, equivalent_camera_ids, CameraIdentityUnavailable
            try:
                target_identity = camera_identity(camera_id)
            except CameraIdentityUnavailable as exc:
                raise CameraBusyError(str(exc)) from None
            # Browser navigation does not release a camera. Only clear a
            # worker whose process/thread has definitively stopped, including
            # a failed diagnostics sandbox worker.
            for key in list(self.active_sessions):
                self._reap_dead_session(key)

            for key, context in list(self.active_sessions.items()):
                ctx_cam_id = context.get("camera_id")
                if ctx_cam_id is not None and _norm(ctx_cam_id) == target_norm:
                    return key
                identity = context.get("camera_identity") if isinstance(context, dict) else context._local.get("camera_identity")
                if identity is None and ctx_cam_id is not None:
                    try:
                        identity = camera_identity(ctx_cam_id)
                    except CameraIdentityUnavailable:
                        if target_identity[0] == "local":
                            return key  # Local ownership is uncertain, not free.
                if identity is not None and identity == target_identity:
                    return key
            for key, lease in list(self._camera_leases.items()):
                if lease.cleanup_confirmed:
                    self._camera_leases.pop(key, None)
                elif _norm(lease.camera_id) == target_norm or lease.camera_identity == target_identity:
                    return lease.purpose
            from services.session_lifecycle import reserved_camera
            try:
                candidates = equivalent_camera_ids(camera_id, target_identity)
            except CameraIdentityUnavailable as exc:
                raise CameraBusyError(str(exc)) from None
            for candidate in candidates:
                owner = reserved_camera(candidate)
                if owner is not None:
                    return owner
            return None

    def registration_preview_fresh(self, camera_id, user_id, token):
        """Report freshness only for this user's exact registration stream."""
        if not token:
            return False
        with self._lifecycle_lock:
            leases = list(self._camera_leases.values())
        return any(
            lease.camera_id == camera_id and lease.purpose == "registration stream"
            and lease.preview_user_id == user_id and lease.preview_token == token
            and not lease.closed and lease.preview_frame_at is not None
            and 0 <= time.monotonic() - lease.preview_frame_at <= 3.0
            for lease in leases
        )

    def registration_preview_busy(self, camera_id, user_id, token):
        """Report contention without disclosing the other camera owner's identity."""
        with self._lifecycle_lock:
            if self.get_camera_owner(camera_id) is None:
                return False
            own_preview = any(
                lease.camera_id == camera_id and lease.purpose == "registration stream"
                and token and lease.preview_user_id == user_id and lease.preview_token == token
                and not lease.closed
                for lease in self._camera_leases.values()
            )
            return not own_preview

    def acquire_camera(self, camera_id, purpose, ttl=30.0):
        """Atomically reserve a camera before any secondary capture I/O."""
        with self._lifecycle_lock:
            if self.get_camera_owner(camera_id) is not None:
                raise CameraBusyError(
                    "Camera is busy. Stop its live session, tuner or camera preview, then retry."
                )
            self._check_secondary_capacity()
            lease = CameraLease(self, camera_id, purpose, max(0.01, float(ttl)))
            from services.camera_identity import camera_identity
            lease.camera_identity = camera_identity(camera_id)
            self._camera_leases[id(lease)] = lease
            lease._timer.start()
            return lease

    def _check_secondary_capacity(self):
        from services.camera_capacity import load_limits, CapacityUnavailable
        try:
            limit = load_limits()['secondary']
        except CapacityUnavailable as exc:
            raise CameraBusyError(str(exc)) from None
        used = sum(not lease.cleanup_confirmed for lease in self._camera_leases.values())
        used += int('sandbox' in self.active_sessions)
        if used >= limit:
            raise CameraBusyError('Secondary camera capacity is full or paused. Close an unused test, preview or tuner and wait for confirmed cleanup; existing camera owners remain protected.')

    def _release_camera_lease(self, lease):
        with self._lifecycle_lock:
            if lease.cleanup_confirmed:
                self._camera_leases.pop(id(lease), None)

    @staticmethod
    def _contain_capture_helper(context, timeout=0.0):
        # A dead attendance process can leave a native capture helper briefly
        # alive. Reap that registered descendant before releasing the camera.
        capture_clean = True
        encoding_clean = True
        try:
            if context.get("capture_helper_pid"):
                from core.capture_process import contain_registered_helper
                capture_clean = contain_registered_helper(context, timeout=timeout)
            if context.get("encoding_helper_pid"):
                from core.face_encoding_process import contain_registered_encoder
                encoding_clean = contain_registered_encoder(context, timeout=timeout)
        except (OSError, EOFError, BrokenPipeError):
            return False  # Unavailable Manager state cannot prove helper cleanup.
        return capture_clean and encoding_clean

    def start(self, class_id, attendance_session_id=None, overrides=None, camera_id=None, startup_attempt=None):
        with self._lifecycle_lock:
            return self._start(class_id, attendance_session_id, overrides, camera_id, startup_attempt)

    def _start(self, class_id, attendance_session_id=None, overrides=None, camera_id=None, startup_attempt=None):
        if startup_attempt:
            from services import session_lifecycle as lifecycle, session_start
            with lifecycle.connection() as conn:
                if not session_start.may_start(conn, attendance_session_id, startup_attempt):
                    raise ValueError('Startup was interrupted or its deadline has passed.')
        self._reap_dead_session(class_id)
        if class_id in self.active_sessions:
            return self.active_sessions[class_id], False
        owner = self.get_camera_owner(camera_id)
        if owner not in (None, class_id):
            # Preserve attendance's existing same-camera tuner priority in the
            # supervised startup path. The lifecycle lock keeps observation and
            # cleanup tied to that owner; no SQLite writer is held here.
            if not (startup_attempt and owner == 'sandbox' and self.stop_sandbox()):
                raise CameraBusyError("Camera is busy. Stop its current owner, then retry.")
        from services.session_lifecycle import reserved_camera
        from services.camera_identity import camera_identity, equivalent_camera_ids
        candidates = equivalent_camera_ids(camera_id, camera_identity(camera_id))
        if any(reserved_camera(candidate, exclude_session=attendance_session_id) is not None for candidate in candidates):
            raise CameraBusyError("Camera cleanup for another attendance occurrence is unconfirmed. Its camera remains reserved.")
        if not startup_attempt:
            # Reviewed Start already reserved its admission durably. A later cap
            # reduction does not revoke that accepted work. Legacy callers still
            # require a fresh check before allocating a worker.
            from services.camera_capacity import load_limits, recognition_reservations, CapacityUnavailable
            from database.db_manager import managed_transaction
            try:
                with managed_transaction(commit=False) as conn:
                    limit = load_limits(conn)['recognition']
                    reserved = recognition_reservations(conn, attendance_session_id)
                used = len(reserved) + sum(1 for key, ctx in self.active_sessions.items()
                    if key != 'sandbox' and ctx.get('attendance_session_id') not in reserved
                    and (attendance_session_id is None or ctx.get('attendance_session_id') != attendance_session_id))
                if used >= limit:
                    raise CameraBusyError('Recognition capacity is full or paused. Existing work remains protected; no replacement worker was created.')
            except CapacityUnavailable as exc:
                raise CameraBusyError(str(exc)) from None
        self.recent_stops.pop(class_id, None)

        from services.camera_identity import camera_identity
        startup_deadline = None
        if startup_attempt:
            with lifecycle.connection() as conn:
                attempt = session_start.current(conn, attendance_session_id)
                state = lifecycle.lifecycle(conn, attendance_session_id)
                if attempt and attempt["token"] == startup_attempt:
                    startup_deadline = attempt["deadline_mono"]
                    if state and state.get("cutoff") is not None:
                        startup_deadline = min(startup_deadline,
                            time.monotonic() + max(0.0, state["cutoff"] - time.time()))
        context = self._new_context({
            "last_seen": time.time(), "worker_mode": "process", "started_at": time.time(),
            "attendance_session_id": attendance_session_id, "startup_attempt": startup_attempt,
            "startup_deadline": startup_deadline, "camera_id": camera_id,
            "camera_identity": camera_identity(camera_id),
            "overrides": dict(overrides) if overrides else {},
        })
        stop_event = context.get("stop_event")
        shm = context.get("shm")

        # Thread mode uses the lightweight loop retained for headless/test
        # operation; process mode runs the live attendance pipeline.
        is_test_mode = "Mock" in type(live_session_loop).__name__ or getattr(config, "TESTING", False)
        use_thread_worker = is_test_mode or getattr(config, "SESSION_WORKER_MODE", "process") == "thread"
        target_func = _test_dummy_loop if use_thread_worker else live_session_loop

        if use_thread_worker:
            # In test mode, use a Thread instead of a Process to avoid
            # SQLite WAL-mode write-lock contention. Threads share the
            # parent process address space and do NOT hold independent
            # SQLite file handles, so setUp() cleanup DELETEs succeed.
            worker = threading.Thread(
                target=target_func,
                args=(class_id, context),
                daemon=True,
            )
            context["worker_mode"] = "thread"
            logger.info("Session worker started for class %s in thread mode.", class_id)
        else:
            worker = multiprocessing.Process(
                target=target_func,
                args=(class_id, context),
                daemon=True,
            )
            logger.info("Session worker started for class %s in process mode.", class_id)

        context._set_local("worker", worker)
        context._set_local("shm", shm)

        self.active_sessions[class_id] = context
        from services.session_lifecycle import reserve_process, identify_process
        import os
        token = reserve_process(attendance_session_id, 'worker')
        context['lifecycle_owner_token'] = token
        context._set_local('lifecycle_owner_token', token)
        if startup_attempt:
            with lifecycle.connection() as conn:
                if not session_start.may_start(conn, attendance_session_id, startup_attempt):
                    # This worker was never spawned. Confirm only its own receipt;
                    # dispatcher/helper reservations retain their own proof gates.
                    from services.session_lifecycle import confirm_process
                    confirm_process(token)
                    stop_event.set()
                    raise ValueError('Startup expired before worker launch.')
        worker.start()
        identify_process(token, os.getpid() if use_thread_worker else worker.pid)
        return context, True

    @contextmanager
    def bounded_runtime_guard(self):
        """Read requests must not wait behind native startup indefinitely."""
        acquired = self._lifecycle_lock.acquire(timeout=0.15)
        try:
            yield acquired
        finally:
            if acquired:
                self._lifecycle_lock.release()

    @contextmanager
    def lifecycle_guard(self):
        """Prevent a new worker start during a cross-system lifecycle change.

        Callers that also need a SQLite write transaction must acquire this
        guard first so every worker/database lifecycle operation has one order.
        """
        with self._lifecycle_lock:
            yield

    def _release_context_resources(self, class_id, context):
        """Release parent-owned resources after worker termination is known."""
        token = context.get('lifecycle_owner_token')
        if token:
            self.confirmed_lifecycle_tokens.add(token)
        shm = context.get("shm")
        if shm:
            try:
                shm.close()
            except Exception as e:
                logger.warning("Error closing shm in stop: %s", e)
            try:
                shm.unlink()
            except Exception as e:
                logger.warning("Error unlinking shm in stop: %s", e)

    def poll_stop(self, class_id, session_id):
        """Signal one exact occurrence; observe cleanup without waiting for joins.

        Missing local context is not cleanup evidence. Durable process owners
        are checked separately by the coordinator, including after a restart.
        """
        if not self._lifecycle_lock.acquire(blocking=False):
            return False
        try:
            context = self.active_sessions.get(class_id)
        finally:
            self._lifecycle_lock.release()
        # Manager IPC and native process inspection may stall. They must not
        # hold the shared camera lifecycle lock or a SQLite transaction.
        if context is None:
            return True
        if context.get('attendance_session_id') != session_id:
            return False
        stop_event = context.get('stop_event')
        if stop_event:
            try:
                stop_event.set()
            except (OSError, EOFError, BrokenPipeError):
                # Lost Manager IPC is not a reason to skip containment of the
                # exact locally owned worker. Helper ownership stays reserved.
                logger.warning("Session stop signal is unverified; checking the owned worker.")
        alive = self._worker_alive(context)
        if alive is not False:
            requested = (context._local.get('lifecycle_stop_requested_at')
                         if isinstance(context, SessionContextDict)
                         else context.get('lifecycle_stop_requested_at'))
            if requested is None:
                if isinstance(context, SessionContextDict):
                    context._set_local('lifecycle_stop_requested_at', time.monotonic())
                else:
                    context['lifecycle_stop_requested_at'] = time.monotonic()
            elif alive and time.monotonic() - requested >= 2 and context.get('worker_mode') == 'process':
                terminate = getattr(context.get('worker'), 'terminate', None)
                if callable(terminate):
                    terminate()
            return False
        if not self._contain_capture_helper(context, timeout=0.0):
            return False
        if not self._lifecycle_lock.acquire(blocking=False):
            return False
        try:
            if self.active_sessions.get(class_id) is not context:
                return False
            self.active_sessions.pop(class_id, None)
        finally:
            self._lifecycle_lock.release()
        self._release_context_resources(class_id, context)
        return True

    def stop(self, class_id):
        """Request and confirm worker shutdown.

        ``True`` means the requested class has no live worker (including the
        already-stopped/no-registration case). ``False`` means a registered
        worker is still alive after the bounded shutdown lifecycle. Failed
        workers remain registered so callers cannot mistake them for stopped
        workers and may retry safely.
        """
        with self._lifecycle_lock:
            context = self.active_sessions.get(class_id)
            self.recent_stops.pop(class_id, None)
            if context is None:
                return True

            stop_event = context.get("stop_event")
            if stop_event:
                try:
                    stop_event.set()
                except (OSError, EOFError, BrokenPipeError):
                    # Known process handles can still be joined/terminated, but
                    # helper cleanup remains unverified if its registry is lost.
                    logger.warning("Session control unavailable during stop; retaining uncertain camera ownership.")

            worker = context.get("worker")
            if worker is not None:
                join = getattr(worker, "join", None)
                is_alive = getattr(worker, "is_alive", None)
                if not callable(join) or not callable(is_alive):
                    logger.error(
                        "Cannot verify shutdown for class %s: worker lacks lifecycle methods.",
                        class_id,
                    )
                    return False

                join(timeout=WORKER_GRACEFUL_STOP_TIMEOUT_SECONDS)
                if is_alive() and context.get("worker_mode") == "process":
                    terminate = getattr(worker, "terminate", None)
                    if callable(terminate):
                        logger.warning(
                            "Graceful shutdown timed out for class %s; terminating process worker.",
                            class_id,
                        )
                        terminate()
                        join(timeout=WORKER_TERMINATE_TIMEOUT_SECONDS)

                if is_alive():
                    logger.error(
                        "Session worker for class %s remains alive after shutdown request.",
                        class_id,
                    )
                    return False

            if not self._contain_capture_helper(context, timeout=2.0):
                return False

            if self.active_sessions.get(class_id) is context:
                self.active_sessions.pop(class_id, None)
            self._release_context_resources(class_id, context)
            logger.info("Session worker stopped for class %s.", class_id)
            return True

    def heartbeat(self, class_id):
        if class_id in self.active_sessions:
            try:
                self.active_sessions[class_id]["last_seen"] = time.time()
            except (OSError, EOFError, BrokenPipeError):
                logger.warning("Could not update heartbeat for class %s; worker status is unverified.", class_id)

    def cleanup_if_inactive(self, class_id, is_active):
        with self._lifecycle_lock:
            return self._cleanup_if_inactive(class_id, is_active)

    def _cleanup_if_inactive(self, class_id, is_active):
        if not is_active and class_id in self.active_sessions:
            context = self.active_sessions.get(class_id)
            worker = context.get("worker") if context else None
            is_alive = getattr(worker, "is_alive", None) if worker is not None else None
            if worker is not None and (not callable(is_alive) or is_alive()):
                logger.warning(
                    "Retaining worker reference for inactive class %s because termination is unconfirmed.",
                    class_id,
                )
                return None

            if context and not self._contain_capture_helper(context):
                return None

            context = self.active_sessions.pop(class_id, None)
            stop_info = self._stop_info_from_context(context)
            if stop_info:
                self.recent_stops[class_id] = stop_info

            if context:
                self._release_context_resources(class_id, context)

            logger.info("Cleaned inactive worker reference for class %s.", class_id)
            return stop_info
        if is_active:
            self.recent_stops.pop(class_id, None)
            return None
        return self.recent_stops.get(class_id)

    def _stop_info_from_context(self, context):
        if not context:
            return None
        try:
            reason = context.get("stop_reason")
            message = context.get("stop_message")
            stopped_at = context.get("stopped_at")
        except (OSError, EOFError, BrokenPipeError):
            return None
        if not reason and not message:
            return None
        return {
            "reason": reason or "session_stopped",
            "message": message or "Live session stopped.",
            "stopped_at": stopped_at,
        }

    def first_session_context(self):
        with self._lifecycle_lock:
            for key in list(self.active_sessions):
                self._reap_dead_session(key)
            if not self.active_sessions:
                return None
            return next(iter(self.active_sessions.values()))

    def capacity_observations(self):
        """Read allowlisted telemetry without opening cameras or retiring owners."""
        fields = (
            "camera_name", "capture_state", "capture_unstable", "fps",
            "capture_fps", "preview_fps", "capture_timestamp_monotonic",
            "published_capture_timestamp_monotonic", "read_failures", "reconnect_attempts",
        )
        with self._lifecycle_lock:
            workers = []
            for key, context in self.active_sessions.items():
                try:
                    row = {field: context.get(field) for field in fields}
                    alive = self._worker_alive(context)
                    row["execution"] = "running" if alive is True else "stopped" if alive is False else "unverified"
                    row["purpose"] = "Recognition tuner" if key == "sandbox" else "Attendance"
                    workers.append(row)
                except (OSError, EOFError, BrokenPipeError):
                    workers.append({"execution": "unverified", "purpose": "Camera session"})
            preview_reservations = sum(not lease.cleanup_confirmed for lease in self._camera_leases.values())
        return workers, preview_reservations

    def is_sandbox_running(self):
        return self.is_running("sandbox")

    def start_sandbox(self, camera_id=None, overrides=None):
        with self._lifecycle_lock:
            return self._start_sandbox(camera_id=camera_id, overrides=overrides)

    def _start_sandbox(self, camera_id=None, overrides=None):
        self._reap_dead_session("sandbox")
        if "sandbox" in self.active_sessions:
            ctx = self.active_sessions["sandbox"]
            if overrides:
                existing_overrides = dict(ctx.get("overrides") or {})
                existing_overrides.update(overrides)
                ctx["overrides"] = existing_overrides
            return ctx, False

        if self.get_camera_owner(camera_id) is not None:
            raise CameraBusyError("Camera is busy. Stop its current owner, then retry.")

        self._check_secondary_capacity()

        self.recent_stops.pop("sandbox", None)

        from services.camera_identity import camera_identity
        context = self._new_context({
            "last_seen": time.time(), "worker_mode": "process", "started_at": time.time(),
            "camera_id": camera_id, "camera_identity": camera_identity(camera_id),
            "overrides": dict(overrides) if overrides else {},
            "stats": {}, "pipeline_latency": {}, "class_id": "sandbox",
        })
        shm = context.get("shm")

        from core.ai_pipeline import sandbox_session_loop
        target_func = sandbox_session_loop
        is_test_mode = "Mock" in type(sandbox_session_loop).__name__ or getattr(config, "TESTING", False)
        use_thread_worker = is_test_mode or getattr(config, "SESSION_WORKER_MODE", "process") == "thread"
        if use_thread_worker:
            target_func = _test_dummy_sandbox_loop if "Mock" in type(sandbox_session_loop).__name__ else sandbox_session_loop

        if use_thread_worker:
            worker = threading.Thread(
                target=target_func,
                args=(camera_id, context),
                daemon=True,
            )
            context["worker_mode"] = "thread"
            logger.info("Sandbox session worker started in thread mode.")
        else:
            worker = multiprocessing.Process(
                target=target_func,
                args=(camera_id, context),
                daemon=True,
            )
            context["worker_mode"] = "process"
            logger.info("Sandbox session worker started in process mode.")

        context._set_local("worker", worker)
        context._set_local("shm", shm)

        self.active_sessions["sandbox"] = context
        worker.start()
        return context, True

    def stop_sandbox(self):
        return self.stop("sandbox")

    def get_sandbox_context(self):
        with self._lifecycle_lock:
            return self._reap_dead_session("sandbox")

    def get_sandbox_stats(self):
        ctx = self.get_sandbox_context()
        if not ctx:
            return {
                "active": False,
                **self.get_preview_stats("sandbox"),
                "detection_ms": 0.0,
                "tracker_init_ms": None,
                "tracker_update_ms": None,
                "liveness_ms": 0.0,
                "embedding_ms": 0.0,
                "matching_ms": 0.0,
                "total_ms": 0.0,
                "fps": 0.0,
                "capture_fps": 0.0,
                "publication_fps": 0.0,
                "capture_sequence": 0,
                "capture_timestamp_monotonic": None,
                "processed_sequence": 0,
                "publication_sequence": 0,
                "publication_timestamp_monotonic": None,
                "skipped_frames": 0,
                "current_frame_age_ms": 0.0,
                "read_failures": 0,
                "capture_state": "idle",
                "open_failures": 0,
                "reconnect_attempts": 0,
                "consecutive_failures": 0,
                "last_failure": None,
                "best_distance": None,
                "second_distance": None,
                "margin": None,
                "ratio": None,
                "match_info": None,
                "match_history": [],
                "overrides": {},
            }
        stats = ctx.get("stats") or {}
        pipeline_latency = ctx.get("pipeline_latency") or {}

        detection_ms = stats.get("detection_ms", pipeline_latency.get("detect_ms", pipeline_latency.get("detection_ms", 0.0)))
        liveness_ms = stats.get("liveness_ms", pipeline_latency.get("liveness_ms", 0.0))
        embedding_ms = stats.get("embedding_ms", pipeline_latency.get("encode_face_ms", pipeline_latency.get("embedding_ms", 0.0)))
        matching_ms = stats.get("matching_ms", pipeline_latency.get("match_ms", pipeline_latency.get("matching_ms", 0.0)))
        total_ms = stats.get("total_ms", pipeline_latency.get("loop_ms", pipeline_latency.get("total_ms", 0.0)))

        last_match = ctx.get("last_match_confidence") or stats.get("match_info")

        published_capture_at = ctx.get("published_capture_timestamp_monotonic")
        current_frame_age_ms = 0.0
        if published_capture_at is not None:
            current_frame_age_ms = max(0.0, (time.monotonic() - published_capture_at) * 1000.0)

        return {
            "active": True,
            **self.get_preview_stats("sandbox"),
            **{key: pipeline_latency.get(key) for key in (
                "detect_ms", "encode_face_ms", "loop_ms", "rolling_liveness_ms", "match_ms",
                "tracker_init_ms", "tracker_update_ms",
            )},
            "detection_ms": round(float(detection_ms or 0.0), 2),
            "liveness_ms": round(float(liveness_ms or 0.0), 2),
            "embedding_ms": round(float(embedding_ms or 0.0), 2),
            "matching_ms": round(float(matching_ms or 0.0), 2),
            "total_ms": round(float(total_ms or 0.0), 2),
            "fps": ctx.get("fps", stats.get("fps", 0.0)),
            "capture_fps": round(float(ctx.get("capture_fps", stats.get("capture_fps", 0.0)) or 0.0), 2),
            "publication_fps": round(float(ctx.get("publication_fps", stats.get("publication_fps", 0.0)) or 0.0), 2),
            "capture_sequence": int(ctx.get("capture_sequence", stats.get("capture_sequence", 0)) or 0),
            "capture_timestamp_monotonic": ctx.get("capture_timestamp_monotonic"),
            "processed_sequence": int(ctx.get("processed_sequence", stats.get("processed_sequence", 0)) or 0),
            "publication_sequence": int(ctx.get("publication_sequence", stats.get("publication_sequence", 0)) or 0),
            "publication_timestamp_monotonic": ctx.get("publication_timestamp_monotonic"),
            "skipped_frames": int(ctx.get("skipped_frames", stats.get("skipped_frames", 0)) or 0),
            "current_frame_age_ms": round(current_frame_age_ms, 2),
            "read_failures": int(ctx.get("read_failures", stats.get("read_failures", 0)) or 0),
            "capture_state": ctx.get("capture_state", stats.get("capture_state", "unknown")) or "unknown",
            "open_failures": int(ctx.get("open_failures", stats.get("open_failures", 0)) or 0),
            "reconnect_attempts": int(ctx.get("reconnect_attempts", stats.get("reconnect_attempts", 0)) or 0),
            "consecutive_failures": int(ctx.get("consecutive_failures", stats.get("consecutive_failures", 0)) or 0),
            "last_failure": ctx.get("last_failure", stats.get("last_failure")),
            "best_distance": stats.get("best_distance", last_match.get("best_distance") if isinstance(last_match, dict) else None),
            "second_distance": stats.get("second_distance", last_match.get("second_distance") if isinstance(last_match, dict) else None),
            "margin": stats.get("margin", last_match.get("margin") if isinstance(last_match, dict) else None),
            "ratio": stats.get("ratio", last_match.get("ratio") if isinstance(last_match, dict) else None),
            "match_info": last_match,
            "match_history": list(ctx.get("match_history") or [])[-20:],
            "overrides": ctx.get("overrides", {}),
        }

    def get_preview_stats(self, class_id=None, *, context=None):
        """Non-identifying progress for the independent live picture."""
        ctx = context if context is not None else self.active_sessions.get(class_id)
        result = {"preview_publication_sequence": 0, "preview_current_frame_age_ms": None,
                  "preview_fps": 0.0, "preview_resolution": None, "preview_state": "stopped"}
        if ctx is None:
            return result
        try:
            result["preview_publication_sequence"] = max(0, int(ctx.get("preview_publication_sequence", 0) or 0))
            captured_at = ctx.get("preview_published_capture_timestamp_monotonic")
            if captured_at is not None:
                age = (time.monotonic() - float(captured_at)) * 1000.0
                result["preview_current_frame_age_ms"] = round(age, 2) if math.isfinite(age) and age >= 0 else None
            fps = float(ctx.get("preview_fps", 0.0) or 0.0)
            result["preview_fps"] = round(fps, 2) if math.isfinite(fps) and fps >= 0 else 0.0
            result["preview_resolution"] = ctx.get("preview_resolution")
            result["preview_state"] = ctx.get("preview_state", "starting")
        except (TypeError, ValueError, OSError, EOFError, BrokenPipeError):
            pass
        return result

    def update_sandbox_overrides(self, new_overrides: dict):
        ctx = self.active_sessions.get("sandbox")
        if not ctx:
            return None
        current_overrides = dict(ctx.get("overrides") or {})
        current_overrides.update(new_overrides)
        ctx["overrides"] = current_overrides
        return current_overrides

    def update_live_session_overrides(self, class_id, new_overrides: dict):
        ctx = self.active_sessions.get(class_id)
        if not ctx:
            return None
        current_overrides = dict(ctx.get("overrides") or {})
        current_overrides.update(new_overrides)
        ctx["overrides"] = current_overrides
        return current_overrides

    def get_sandbox_overrides(self):
        ctx = self.active_sessions.get("sandbox")
        if not ctx:
            return {}
        return ctx.get("overrides") or {}


session_manager = SessionManager()
