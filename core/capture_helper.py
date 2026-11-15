"""Private native-camera subprocess. No Flask/config/database imports belong here.

The supervisor sends configuration over stdin, never argv. A separate stdin
watcher exits the entire helper on parent disconnect, including while OpenCV is
blocked in open/read/release. Only this process owns the native camera handle.
"""

from __future__ import annotations

import json
import os
import queue
import struct
import sys
import threading
import time


def open_backend(config, cv2):
    source = config["source"]
    capture = None
    remaining = config.get("open_deadline_mono", float("inf")) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Camera open deadline exceeded")
    open_timeout_ms = min(config["open_timeout_ms"], max(1, int(remaining * 1000))) if remaining != float("inf") else config["open_timeout_ms"]
    # These properties are open-only and supported by FFmpeg/GStreamer, not
    # universal .set() deadlines. Request FFmpeg for network sources; devices
    # and unsupported builds still have the supervisor's process deadline.
    # https://docs.opencv.org/4.10.0/d4/d15/group__videoio__flags__base.html
    if isinstance(source, str) and source.lower().startswith(("rtsp://", "rtsps://", "http://", "https://")):
        try:
            capture = cv2.VideoCapture(source, cv2.CAP_FFMPEG, [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, open_timeout_ms,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, config["read_timeout_ms"],
            ])
        except Exception:
            capture = None
        if capture is not None and not capture.isOpened():
            capture.release()
            capture = None
    # A fallback consumes the original open allowance. It may not restart an
    # exhausted deadline after the first backend or its release returned late.
    if capture is None and time.monotonic() >= config.get("open_deadline_mono", float("inf")):
        raise TimeoutError("Camera open deadline exceeded")
    if capture is None:
        capture = cv2.VideoCapture(source)
    if capture.isOpened():
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        for key, prop in (("width", cv2.CAP_PROP_FRAME_WIDTH),
                          ("height", cv2.CAP_PROP_FRAME_HEIGHT),
                          ("target_fps", cv2.CAP_PROP_FPS)):
            if config.get(key) is not None:
                capture.set(prop, config[key])
    return capture


class _NativeFrameReader:
    """Drain one native handle into an overwrite-only slot.

    The owner thread performs every native operation, including release. The
    protocol thread may spend an arbitrary time writing a frame to stdout;
    that must not prevent the owner from consuming newer camera frames.
    """

    def __init__(self, config, backend_factory, np):
        self._config = config
        self._backend_factory = backend_factory
        self._np = np
        self._condition = threading.Condition()
        self._stop_event = threading.Event()
        self._read_enabled = threading.Event()
        self._thread = None
        self._opened = None
        self._finished = False
        self._sequence = 0
        self._latest = None
        self._failure = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name="native-camera-reader", daemon=False
        )
        self._thread.start()

    def wait_until_open(self):
        with self._condition:
            while self._opened is None:
                self._condition.wait()
            return self._opened

    def begin_reading(self):
        # Deliver the open acknowledgement before a native read can block.
        self._read_enabled.set()

    def get_latest(self, after_sequence):
        with self._condition:
            while not self._stop_event.is_set():
                # Once a read fails, never deliver the older retained image as
                # if the camera were still healthy. Let the supervisor reopen.
                if self._failure is not None:
                    return self._sequence, self._failure, None
                if self._latest is not None and self._latest[0] > after_sequence:
                    return self._latest
                if self._finished:
                    return None
                self._condition.wait()
            return None

    def stop(self):
        self._stop_event.set()
        self._read_enabled.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None and self._thread.ident is not None:
            # Deliberately wait for the native owner here. If open/read/release
            # is blocked, the supervisor's bounded process shutdown kills and
            # reaps this entire helper before permitting another camera owner.
            self._thread.join()

    def _fail(self, reason):
        with self._condition:
            self._latest = None
            self._failure = {"kind": "frame", "ok": False, "error": reason}
            self._condition.notify_all()

    def _run(self):
        capture = None
        try:
            capture = self._backend_factory(self._config)
            with self._condition:
                self._opened = bool(capture.isOpened())
                self._condition.notify_all()
            if not self._opened:
                return
            self._read_enabled.wait()
            while not self._stop_event.is_set():
                started = time.monotonic()
                try:
                    ok, frame = capture.read()
                    captured_at = time.monotonic()
                    if not ok or frame is None or not frame.size:
                        self._fail("read_failed")
                        return
                    if frame.ndim not in (2, 3) or frame.nbytes > 64 * 1024 * 1024:
                        self._fail("read_failed")
                        return
                    # Some backends reuse their read buffer. Detach the latest
                    # frame before the next native read can mutate pixels that
                    # the protocol thread is still transmitting. No resize or
                    # lossy conversion is performed here.
                    frame = self._np.array(frame, dtype=self._np.uint8, order="C", copy=True)
                    if frame.nbytes > 64 * 1024 * 1024:
                        self._fail("read_failed")
                        return
                except Exception:
                    # Never expose backend exception text or source details.
                    self._fail("read_exception")
                    return
                with self._condition:
                    if self._stop_event.is_set():
                        return
                    self._sequence += 1
                    self._latest = (
                        self._sequence,
                        {"kind": "frame", "ok": True, "shape": list(frame.shape),
                         "captured_at": captured_at,
                         "read_ms": (captured_at - started) * 1000},
                        frame,
                    )
                    self._condition.notify_all()
        except Exception:
            self._fail("read_exception")
        finally:
            with self._condition:
                if self._opened is None:
                    self._opened = False
                self._finished = True
                self._condition.notify_all()
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    # Process exit still relinquishes a failed native handle.
                    # The supervisor confirms exit before releasing ownership.
                    pass


def run(backend_factory=None):
    """Run the wire protocol; backend_factory is used by synthetic subprocess tests."""
    if sys.platform.startswith("linux"):
        import ctypes
        import signal
        # Kernel termination covers abrupt owner/server death even if a native
        # call holds the GIL. Verify parent after prctl to close the setup race.
        expected_parent = int(os.environ["CSD_CAPTURE_PARENT_PID"])
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise RuntimeError("Unable to contain capture helper lifetime")
        if os.getppid() != expected_parent:
            os._exit(0)
    # Import native modules before a thread waits on the CRT stdin stream.
    # Some Windows numpy builds otherwise deadlock during DLL initialization.
    # No camera can be opened yet; the supervisor still bounds startup/imports.
    import cv2
    import numpy as np

    commands = queue.Queue(maxsize=4)

    def watch_parent():
        try:
            for line in sys.stdin.buffer:
                commands.put(json.loads(line))
        finally:
            # Do not call release here: a native release may itself be blocked.
            # Process exit closes the OS handle, and the owner confirms death.
            os._exit(0)

    threading.Thread(target=watch_parent, daemon=True, name="capture-parent-watch").start()
    output = sys.stdout.buffer

    def reply(message, frame=None):
        payload = memoryview(frame).cast("B") if frame is not None else b""
        message["nbytes"] = len(payload)
        header = json.dumps(message, separators=(",", ":")).encode("ascii")
        output.write(struct.pack("!I", len(header)))
        output.write(header)
        if payload:
            output.write(payload)
        output.flush()

    reader = None
    try:
        reply({"kind": "ready", "ok": True, "protocol": 1})
        config = commands.get()
        # Capture options are process-local; never mutate the web/worker env.
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        factory = backend_factory or (lambda settings: open_backend(settings, cv2))
        reader = _NativeFrameReader(config, factory, np)
        reader.start()
        opened = reader.wait_until_open()
        reply({"kind": "open", "ok": opened})
        if not opened:
            return
        reader.begin_reading()
        delivered_sequence = 0
        while True:
            command = commands.get()
            if command == "stop":
                return
            if command != "read":
                raise ValueError("Invalid capture command")
            packet = reader.get_latest(delivered_sequence)
            if packet is None:
                return
            delivered_sequence, header, frame = packet
            reply(dict(header), frame)
    except Exception:
        try:
            reply({"kind": "error", "ok": False})
        except Exception:
            pass
    finally:
        if reader is not None:
            reader.stop()


if __name__ == "__main__":
    run()
