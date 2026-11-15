"""Bounded process containment for native OpenCV camera operations."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import threading
import time

import numpy as np
import psutil

from core.capture_config import build_capture_config, serialize_capture_config
from core.capture_lifetime import CaptureJob


class CaptureTimeout(RuntimeError):
    def __init__(self, operation):
        self.operation = operation
        super().__init__(f"Camera {operation} deadline exceeded")


class CaptureStopped(RuntimeError):
    """The enclosing owner cancelled camera acquisition."""


class CaptureCleanupError(RuntimeError):
    """Ownership must stay reserved while cleanup cannot be confirmed."""


def contain_registered_helper(state, timeout=2.0):
    """After a worker dies, confirm its registered helper is dead before reuse.

    Registration precedes delivery of source/open configuration. A helper whose
    parent dies before registration has no camera to own. Creation time guards
    against killing an unrelated process that reused the PID.
    """
    pid = state.get("capture_helper_pid")
    created_at = state.get("capture_helper_created_at")
    if not pid:
        return True
    if created_at is None:
        return False
    try:
        process = psutil.Process(int(pid))
        if process.create_time() != created_at:
            return True
        process.kill()
        process.wait(timeout=max(0.0, timeout))
    except psutil.NoSuchProcess:
        pass
    except (psutil.Error, OSError, ValueError):
        return False
    state.update({"capture_helper_pid": None, "capture_helper_created_at": None})
    return True


class CaptureProcess:
    """VideoCapture-shaped adapter whose native work is always in a helper.

    Popen works inside a daemon multiprocessing session worker. Each read has
    one outstanding reply, consumed immediately by LatestFrameCapture, so no
    recognition backlog accumulates. The helper receives no application keys.
    """

    bounded_release = True

    def __init__(self, source, *, width=None, height=None, target_fps=None,
                 open_timeout=3.0, read_timeout=3.0, ready_timeout=8.0,
                 startup_deadline=None, stop_event=None, owner_state=None, command=None):
        self._config = build_capture_config(
            source, width=width, height=height, target_fps=target_fps,
            open_timeout=open_timeout, read_timeout=read_timeout)
        self._open_timeout = open_timeout
        self._read_timeout = read_timeout
        self._ready_timeout = max(0.0, float(ready_timeout))
        if startup_deadline is not None and not math.isfinite(float(startup_deadline)):
            raise ValueError("Invalid camera startup deadline")
        self._startup_deadline = None if startup_deadline is None else float(startup_deadline)
        self._stop_event = stop_event
        self._first_frame = False
        self._opened = False
        self._owner_state = owner_state
        self._command = command or [sys.executable, "-u", str(Path(__file__).with_name("capture_helper.py"))]
        if sys.platform == "win32" and getattr(sys, "_base_executable", sys.executable) != sys.executable:
            # A Windows venv executable is a launcher that creates another
            # python.exe. Killing/reaping that launcher does not establish the
            # native handle owner's death. Execute the base interpreter itself
            # with only this venv's dependency paths added explicitly.
            dependency_paths = [entry for entry in sys.path if "site-packages" in entry]
            bootstrap = (f"import sys,runpy;sys.path[:0]={dependency_paths!r};"
                         "sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')")
            if self._command[:2] != [sys.executable, "-u"] or len(self._command) != 3:
                raise ValueError("Capture helper must be a Python script")
            self._command = [sys._base_executable, "-u", "-c", bootstrap, self._command[2]]
        self._process = None
        self._spawn_pending = False
        self._job = None
        self._reader = None
        self._responses = queue.Queue(maxsize=3)
        self._lock = threading.RLock()
        self._closing = False
        self.captured_at_monotonic = None
        self.last_failure = None
        self.stage = "idle"

    @property
    def cleanup_confirmed(self):
        process, reader = self._process, self._reader
        return (not self._spawn_pending
                and (process is None or process.poll() is not None)
                and (reader is None or not reader.is_alive()))

    def _receive(self):
        def read_exact(length):
            result = bytearray(length)
            view = memoryview(result)
            offset = 0
            while offset < length:
                count = self._process.stdout.readinto(view[offset:])
                if not count:
                    raise EOFError
                offset += count
            return result
        try:
            while True:
                header_length = struct.unpack("!I", read_exact(4))[0]
                if header_length > 8192:
                    raise ValueError("Invalid capture reply")
                header = json.loads(read_exact(header_length))
                length = int(header.get("nbytes", 0))
                if length < 0 or length > 64 * 1024 * 1024:
                    raise ValueError("Invalid capture frame size")
                payload = read_exact(length) if length else None
                self._responses.put_nowait((header, payload))
        except (EOFError, OSError, ValueError, queue.Full):
            try:
                self._responses.put_nowait(({"kind": "closed", "ok": False}, None))
            except queue.Full:
                pass

    def _remaining(self, deadline=None):
        limit = self._startup_deadline if deadline is None else deadline
        return None if limit is None else max(0.0, limit - time.monotonic())

    def _abort(self, operation, *, stopped=False):
        remaining = self._remaining()
        # Cleanup consumes the enclosing budget too. An unconfirmed kill keeps
        # the registered owner reserved; it never permits a replacement helper.
        self.last_failure = "stopped" if stopped else f"{operation}_timeout"
        if not self.release(timeout=min(1.0, remaining) if remaining is not None else 1.0,
                            graceful=False):
            self.last_failure = "cleanup_failed"
            raise CaptureCleanupError("Camera helper cleanup is not confirmed")
        if stopped:
            raise CaptureStopped("Camera acquisition stopped")
        raise CaptureTimeout(operation) from None

    def _await(self, operation, timeout):
        self.stage = operation
        deadline = time.monotonic() + max(0.0, timeout)
        if self._startup_deadline is not None:
            # Leave a small part of the *same* total budget for kill/reap.
            deadline = min(deadline, self._startup_deadline - 0.25)
        while True:
            if self._closing or (self._stop_event is not None and self._stop_event.is_set()):
                self._abort(operation, stopped=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._abort(operation)
            try:
                return self._responses.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue

    def isOpened(self):
        config = dict(self._config)
        # Reserve the complete deadline field before ownership or helper launch.
        serialize_capture_config(config)
        with self._lock:
            if self._closing:
                return False
            if self._spawn_pending:
                raise CaptureCleanupError("Camera helper launch outcome is unconfirmed")
            if self._process is not None:
                return self._opened and self._process.poll() is None
            if self._startup_deadline is None:
                self._startup_deadline = (time.monotonic() + self._ready_timeout
                                          + self._open_timeout + self._read_timeout + 1.0)
            if self._remaining() <= 0.25:
                self.last_failure = "helper_ready_timeout"
                raise CaptureTimeout("helper_ready")
            if self._stop_event is not None and self._stop_event.is_set():
                raise CaptureStopped("Camera acquisition stopped")
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
            allowed = {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "HOME", "LANG", "LC_ALL"}
            helper_env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
            helper_env["CSD_CAPTURE_PARENT_PID"] = str(os.getpid())
            helper_env["PYTHON_DOTENV_DISABLED"] = "1"
            from services.session_lifecycle import reserve_process, identify_process
            self._lifecycle_token = reserve_process(self._owner_state.get('attendance_session_id') if self._owner_state is not None else None, 'capture')
            self._spawn_pending = True
            try:
                self._process = subprocess.Popen(self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                 stderr=subprocess.DEVNULL, bufsize=0, env=helper_env, **options)
            except FileNotFoundError:
                # A missing executable cannot create this helper. Other OS
                # errors can arise closing handles AFTER CreateProcess, so
                # those launch outcomes must retain the unknown reservation.
                self._spawn_pending = False
                self.last_failure = "helper_start_failed"
                self.release(graceful=False)
                raise
            self._spawn_pending = False
            identify_process(self._lifecycle_token, self._process.pid)
            if sys.platform == "win32":
                self._job = CaptureJob(self._process)
            # Register before the helper is allowed to open a camera.
            if self._owner_state is not None:
                self._owner_state.update({"capture_helper_pid": self._process.pid,
                                          "capture_helper_created_at": psutil.Process(self._process.pid).create_time()})
            self._reader = threading.Thread(target=self._receive, name="capture-helper-replies", daemon=True)
            self._reader.start()
        # Native imports have their own stage. No source reaches the child until
        # containment/registration is established and readiness is acknowledged.
        header, _ = self._await("helper_ready", self._ready_timeout)
        if header.get("kind") != "ready" or not header.get("ok") or header.get("protocol") != 1:
            self.last_failure = "helper_initialization_failed"
            return False
        with self._lock:
            if self._closing:
                return False
            if self._stop_event is not None and self._stop_event.is_set():
                self._abort("open", stopped=True)
            if self._remaining() <= 0.25:
                self._abort("open")
            open_deadline = min(
                time.monotonic() + self._open_timeout, self._startup_deadline - 0.25)
            config_bytes = serialize_capture_config(config, open_deadline_mono=open_deadline)
            self._process.stdin.write(config_bytes)
        header, _ = self._await("open", self._open_timeout)
        self._opened = header.get("kind") == "open" and header.get("ok", False)
        if not self._opened:
            self.last_failure = "open_failed"
        self.stage = "first_read" if self._opened else "open"
        return self._opened

    def set(self, _property, _value):
        # All supported properties are passed in the initial config, in helper.
        return True

    def read(self):
        with self._lock:
            if self._closing or self._process is None or self._process.poll() is not None:
                return False, None
            self._process.stdin.write(b'"read"\n')
        operation = "read" if self._first_frame else "first_read"
        header, payload = self._await(operation, self._read_timeout)
        if header.get("kind") != "frame" or not header.get("ok") or payload is None:
            # Only fixed protocol codes may leave the helper boundary.
            error = header.get("error")
            self.last_failure = error if error in {"read_failed", "read_exception"} else "read_failed"
            return False, None
        self.captured_at_monotonic = header["captured_at"]
        frame = np.frombuffer(payload, dtype=np.uint8).reshape(header["shape"])
        self._first_frame = True
        self._startup_deadline = None
        self.stage = "read"
        return True, frame

    def release(self, timeout=1.0, *, graceful=True):
        deadline = time.monotonic() + max(0.0, timeout)
        if not self._lock.acquire(timeout=max(0.0, timeout)):
            return False
        try:
            self._closing = True
            process = self._process
            if process is None:
                if self._spawn_pending:
                    return False
                from services.session_lifecycle import confirm_process
                confirm_process(getattr(self, '_lifecycle_token', None))
                return True
            if process.poll() is None and graceful:
                try:
                    process.stdin.write(b'"stop"\n')
                    process.wait(timeout=max(0.0, min(timeout * 0.25, deadline - time.monotonic())))
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except (OSError, subprocess.TimeoutExpired):
                    return False
            if self._reader is not None:
                self._reader.join(timeout=max(0.0, deadline - time.monotonic()))
                if self._reader.is_alive():
                    return False
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()
            if self._job is not None:
                self._job.close()
                self._job = None
            if self._owner_state is not None:
                self._owner_state.update({"capture_helper_pid": None, "capture_helper_created_at": None})
            from services.session_lifecycle import confirm_process
            confirm_process(getattr(self, '_lifecycle_token', None))
            return True
        finally:
            self._lock.release()
