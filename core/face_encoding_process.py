"""Supervise unchanged face-descriptor computation outside the worker's GIL."""

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

from core.capture_lifetime import CaptureJob


MAX_FRAME_BYTES = 64 * 1024 * 1024
VECTOR_BYTES = 128 * 8


class FaceEncodingError(RuntimeError):
    """No descriptor was produced; callers must retain ordinary no-match behavior."""


class FaceEncodingUnavailable(FaceEncodingError):
    """Fatal helper failure: the attendance worker must interrupt truthfully."""


class FaceEncodingTimeout(FaceEncodingUnavailable):
    """The helper exceeded its bounded startup or complete request deadline."""


class FaceEncodingCleanupError(FaceEncodingUnavailable):
    """The helper's death has not been established; retain owner registration."""


def contain_registered_encoder(state, timeout=2.0):
    """Contain a dead worker's registered descriptor helper without PID reuse risk."""
    pid = state.get("encoding_helper_pid")
    created_at = state.get("encoding_helper_created_at")
    if not pid:
        return True
    if created_at is None:
        return False
    try:
        process = psutil.Process(int(pid))
        if process.create_time() == created_at:
            process.kill()
            process.wait(timeout=max(0.0, timeout))
    except psutil.NoSuchProcess:
        pass
    except (psutil.Error, OSError, ValueError):
        return False
    state.update({"encoding_helper_pid": None, "encoding_helper_created_at": None,
                  "encoding_helper_contained": True})
    return True


def _validate_input(frame, locations):
    if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8
            or frame.ndim != 3 or frame.shape[2] != 3
            or frame.shape[0] < 1 or frame.shape[1] < 1
            or frame.nbytes > MAX_FRAME_BYTES):
        raise ValueError("Encoding requires a bounded uint8 RGB frame")
    if not isinstance(locations, (list, tuple)) or len(locations) > 1:
        raise ValueError("Encoding supports one known face location")
    parsed = []
    for location in locations:
        if (not isinstance(location, (list, tuple)) or len(location) != 4
                or any(isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer))
                       for value in location)):
            raise ValueError("Invalid face location")
        top, right, bottom, left = (int(value) for value in location)
        if not (0 <= top < bottom <= frame.shape[0] and 0 <= left < right <= frame.shape[1]):
            raise ValueError("Face location is outside the RGB frame")
        parsed.append([top, right, bottom, left])
    return parsed


class FaceEncodingProcess:
    """Popen-based adapter usable inside a daemon multiprocessing worker.

    Only transient RGB pixels and one box enter the helper. It receives neither
    recognition thresholds nor identities, application keys, or database access.
    A writer thread makes the frame transfer part of the request deadline.
    """

    def __init__(self, owner_state=None, timeout=10):
        if isinstance(timeout, bool) or not math.isfinite(float(timeout)) or float(timeout) <= 0:
            raise ValueError("Encoding timeout must be positive and finite")
        self._timeout = float(timeout)
        self._owner_state = owner_state
        self._helper_script = Path(__file__).with_name("face_encoding_helper.py")
        self._process = None
        self._spawn_pending = False
        self._job = None
        self._reader = None
        self._writer = None
        self._responses = queue.Queue(maxsize=3)
        self._operation_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._closing = False
        self._ready = False
        self._request_id = 0
        self.last_failure = None

    @property
    def cleanup_confirmed(self):
        return (not self._spawn_pending
                and (self._process is None or self._process.poll() is not None)
                and (self._reader is None or not self._reader.is_alive())
                and (self._writer is None or not self._writer.is_alive()))

    def _command(self):
        script = str(self._helper_script)
        if sys.platform == "win32" and getattr(sys, "_base_executable", sys.executable) != sys.executable:
            # Bypass the venv launcher: the supervised PID must own native work.
            paths = [entry for entry in sys.path if "site-packages" in entry]
            bootstrap = (f"import sys,runpy;sys.path[:0]={paths!r};"
                         "sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')")
            return [sys._base_executable, "-u", "-c", bootstrap, script]
        return [sys.executable, "-u", script]

    def _receive(self, process):
        def read_exact(length):
            value = bytearray(length)
            view = memoryview(value)
            offset = 0
            while offset < length:
                count = process.stdout.readinto(view[offset:])
                if not count:
                    raise EOFError
                offset += count
            return value

        try:
            while True:
                size = struct.unpack("!I", read_exact(4))[0]
                if not 0 < size <= 8192:
                    raise ValueError
                header = json.loads(read_exact(size))
                if not isinstance(header, dict):
                    raise ValueError
                length = header.get("nbytes")
                if type(length) is not int or not 0 <= length <= VECTOR_BYTES:
                    raise ValueError
                payload = read_exact(length) if length else b""
                self._responses.put_nowait((header, payload))
        except (EOFError, OSError, ValueError, RecursionError, queue.Full):
            try:
                self._responses.put_nowait(({"kind": "closed", "ok": False}, b""))
            except queue.Full:
                pass

    def _fail(self, reason, *, timed_out=False):
        self.last_failure = reason
        if not self.close(timeout=2):
            raise FaceEncodingCleanupError("Face encoding helper cleanup is not confirmed") from None
        if timed_out:
            raise FaceEncodingTimeout("Face encoding deadline exceeded") from None
        raise FaceEncodingUnavailable("Face encoding helper is unavailable") from None

    def _await(self, deadline):
        if time.monotonic() >= deadline:
            self._fail("encoding_timeout", timed_out=True)
        try:
            return self._responses.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            self._fail("encoding_timeout", timed_out=True)

    def start(self):
        """Preload the same installed face-recognition model under containment."""
        with self._operation_lock:
            with self._state_lock:
                if self._closing:
                    return False
                if self._process is not None:
                    return self._ready and self._process.poll() is None
                deadline = time.monotonic() + self._timeout
                allowed = {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "TEMP", "TMP",
                           "USERPROFILE", "HOME", "LANG", "LC_ALL"}
                helper_env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
                # Each camera already has its own helper. Bound native math
                # threads before import so concurrent cameras do not each
                # create a full CPU pool; never inherit an ambient override.
                helper_env["OPENBLAS_NUM_THREADS"] = "1"
                helper_env["CSD_FACE_ENCODING_PARENT_PID"] = str(os.getpid())
                helper_env["PYTHON_DOTENV_DISABLED"] = "1"
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
                try:
                    from services.session_lifecycle import reserve_process, identify_process
                    self._lifecycle_token = reserve_process(self._owner_state.get('attendance_session_id') if self._owner_state is not None else None, 'encoding')
                    command = self._command()
                    self._spawn_pending = True
                    try:
                        self._process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                                         bufsize=0, env=helper_env, **options)
                    except FileNotFoundError:
                        # A missing executable proves no helper was created.
                        # Other OS errors may follow CreateProcess; retain
                        # those PID-less intents just like arbitrary errors.
                        self._spawn_pending = False
                        raise
                    self._spawn_pending = False
                    identify_process(self._lifecycle_token, self._process.pid)
                    if sys.platform == "win32":
                        self._job = CaptureJob(self._process)
                    # No frame is sent until OS containment and registration succeed.
                    if self._owner_state is not None:
                        self._owner_state.update({
                            "encoding_helper_pid": self._process.pid,
                            "encoding_helper_created_at": psutil.Process(self._process.pid).create_time(),
                            "encoding_helper_contained": False,
                        })
                    self._reader = threading.Thread(target=self._receive, args=(self._process,),
                                                    daemon=True, name="encoding-helper-replies")
                    self._reader.start()
                except Exception:
                    self.last_failure = "encoding_start_failed"
                    if not self.close(timeout=2):
                        raise FaceEncodingCleanupError("Face encoding helper cleanup is not confirmed") from None
                    return False
            header, payload = self._await(deadline)
            if (header.get("kind") != "ready" or header.get("ok") is not True
                    or type(header.get("protocol")) is not int or header["protocol"] != 1 or payload):
                self._fail("encoding_protocol_error")
            with self._state_lock:
                self._ready = not self._closing and self._process.poll() is None
            return self._ready

    def encode(self, rgb_frame, locations):
        """Call face_recognition.face_encodings(frame, [location]) unchanged."""
        parsed_locations = _validate_input(rgb_frame, locations)
        if not parsed_locations:
            return []
        with self._operation_lock:
            if not self.start():
                raise FaceEncodingUnavailable("Face encoding helper is unavailable")
            deadline = time.monotonic() + self._timeout
            self._request_id += 1
            request_id = self._request_id
            payload = rgb_frame.tobytes(order="C")
            header = json.dumps({"kind": "encode", "id": request_id,
                                 "shape": list(rgb_frame.shape), "locations": parsed_locations,
                                 "nbytes": len(payload)}, separators=(",", ":")).encode("ascii")
            envelope = struct.pack("!I", len(header)) + header
            process = self._process
            sent = threading.Event()
            write_failed = []

            def send():
                try:
                    for data in (envelope, payload):
                        view = memoryview(data)
                        while view:
                            count = process.stdin.write(view[:65536])
                            if not count:
                                raise OSError
                            view = view[count:]
                    process.stdin.flush()
                except (OSError, ValueError):
                    write_failed.append(True)
                finally:
                    sent.set()

            with self._state_lock:
                if self._closing:
                    raise FaceEncodingUnavailable("Face encoding helper is unavailable")
                self._writer = threading.Thread(target=send, daemon=True, name="encoding-helper-request")
                try:
                    self._writer.start()
                except Exception:
                    self._fail("encoding_writer_start_failed")
            if not sent.wait(timeout=max(0.0, deadline - time.monotonic())):
                self._fail("encoding_write_timeout", timed_out=True)
            if write_failed:
                self._fail("encoding_write_failed")
            header, payload = self._await(deadline)
            if (header.get("kind") != "encoded" or type(header.get("id")) is not int
                    or header["id"] != request_id or type(header.get("ok")) is not bool):
                self._fail("encoding_protocol_error")
            if header["ok"] is False:
                if payload:
                    self._fail("encoding_protocol_error")
                self.last_failure = "encoding_failed"
                raise FaceEncodingError("Face encoding did not produce a descriptor")
            count = header.get("count")
            if type(count) is not int or count not in (0, 1) or len(payload) != count * VECTOR_BYTES:
                self._fail("encoding_protocol_error")
            if count == 0:
                self.last_failure = None
                return []
            vector = np.frombuffer(payload, dtype="<f8").astype(np.float64, copy=True)
            if vector.shape != (128,) or not np.isfinite(vector).all():
                self._fail("encoding_invalid_vector")
            self.last_failure = None
            return [vector]

    def close(self, timeout=2):
        """Kill and reap within the deadline; unknown cleanup retains registration."""
        deadline = time.monotonic() + max(0.0, timeout)
        if not self._state_lock.acquire(timeout=max(0.0, timeout)):
            return False
        try:
            self._closing = True
            self._ready = False
            process = self._process
            if process is None and self._spawn_pending:
                return False
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except (OSError, subprocess.TimeoutExpired):
                    return False
            for thread in (self._writer, self._reader):
                if (thread is not None and thread is not threading.current_thread()
                        and thread.ident is not None):
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))
                    if thread.is_alive():
                        return False
            if process is not None:
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        pipe.close()
            if self._job is not None:
                self._job.close()
                self._job = None
            if self._owner_state is not None:
                self._owner_state.update({"encoding_helper_pid": None, "encoding_helper_created_at": None,
                                          "encoding_helper_contained": True})
            from services.session_lifecycle import confirm_process
            confirm_process(getattr(self, '_lifecycle_token', None))
            return True
        except Exception:
            # Owner-state IPC can fail during server teardown; retain the claim
            # and avoid propagating process-specific exception details.
            return False
        finally:
            self._state_lock.release()
