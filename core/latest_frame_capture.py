"""Overwrite-only camera acquisition for realtime recognition workers."""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
import math
import threading
import time
from typing import Any

import cv2

from core.capture_process import CaptureCleanupError, CaptureProcess, CaptureStopped, CaptureTimeout


@dataclass(frozen=True)
class CapturedFrame:
    """One immutable reference to a captured frame and its freshness metadata."""

    sequence: int
    captured_at_monotonic: float
    frame: Any


class LatestFrameCapture:
    """Continuously consume a camera stream while retaining only its newest frame.

    Recognition calls :meth:`get_latest` with the last sequence it processed. If
    capture advances while recognition is busy, intermediate frames are replaced
    in the single slot and never form an application queue.
    """

    def __init__(
        self,
        source,
        *,
        width: int | None = None,
        height: int | None = None,
        target_fps: float | None = None,
        external_stop_event=None,
        max_retries: int = 10,
        reconnect_delay: float = 1.0,
        open_timeout: float = 3.0,
        read_timeout: float = 3.0,
        stale_timeout: float = 2.0,
        startup_deadline: float | None = None,
        owner_state=None,
    ):
        self._source = source
        self._width = width
        self._height = height
        self._target_fps = target_fps
        self._external_stop_event = external_stop_event
        self._max_retries = max(0, int(max_retries))
        self._reconnect_delay = max(0.0, float(reconnect_delay))
        self._open_timeout = max(0.05, float(open_timeout))
        self._read_timeout = max(0.05, float(read_timeout))
        self._stale_timeout = max(0.05, float(stale_timeout))
        self._owner_state = owner_state
        if startup_deadline is not None and not math.isfinite(float(startup_deadline)):
            raise ValueError("Invalid camera startup deadline")
        self._startup_deadline = None if startup_deadline is None else float(startup_deadline)

        self._condition = threading.Condition()
        self._capture_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._capture = None
        self._latest = None

        self._sequence = 0
        self._capture_fps = 0.0
        self._fps_window_started = time.monotonic()
        self._fps_window_frames = 0
        self._last_capture_at = None
        self._last_read_ms = 0.0
        self._read_failures = 0
        self._open_failures = 0
        self._reconnect_attempts = 0
        self._consecutive_failures = 0
        self._terminal_failure = False
        self._terminal_reason = None
        self._state = "idle"
        self._last_failure = None
        self._failure_times = deque()
        self._unstable = False
        self._stable_since = None
        self._stable_frames = 0

    def start(self) -> bool:
        """Start the dedicated reader thread and its bounded open/retry lifecycle.

        Opening happens in the reader thread so an initially unavailable source
        gets the same retry policy as a source that disconnects later. A capture
        instance can be started again after its previous reader has fully stopped.
        """
        with self._condition:
            if self._thread and (
                self._thread.is_alive()
                or self._thread.ident is None
            ):
                return not self._terminal_failure and not self._should_stop()
            if not self.cleanup_confirmed:
                return False
            external = self._external_stop_event
            if external is not None and external.is_set():
                self._state = "stopped"
                return False

            self._reset_for_start_locked()
            self._state = "starting"
            thread = threading.Thread(
                target=self._capture_loop,
                name="latest-frame-capture",
                daemon=True,
            )
            self._thread = thread

        try:
            thread.start()
        except Exception:
            with self._condition:
                if thread.ident is None:
                    # No reader/native owner ever started. Do not leave the
                    # pending-thread guard reserving an unused camera forever.
                    self._thread = None
                    self._state = "failed"
                    self._terminal_failure = True
                    self._last_failure = "reader_start_failed"
                    self._condition.notify_all()
            return False
        if not self._should_stop():
            # Give the reader a bounded chance to publish its first frame. The
            # thread continues its retry lifecycle if the source is still down.
            self.get_latest(timeout=0.5)

        with self._condition:
            # A frame published just as an external owner requested shutdown is
            # still a successful start; the owner's loop will then exit cleanly.
            started = self._sequence > 0 or (
                not self._terminal_failure and not self._should_stop()
            )
        if not started and thread.is_alive():
            # Terminal transitions notify waiters before the reader's finally
            # block returns. Briefly reap it so an immediate retry can safely
            # create a fresh reader instead of observing a dying thread.
            thread.join(timeout=0.1)
        return started

    def _reset_for_start_locked(self) -> None:
        """Reset run-scoped state after a previous reader has fully exited."""
        self._stop_event.clear()
        self._latest = None
        self._sequence = 0
        self._capture_fps = 0.0
        self._fps_window_started = time.monotonic()
        self._fps_window_frames = 0
        self._last_capture_at = None
        self._last_read_ms = 0.0
        self._read_failures = 0
        self._open_failures = 0
        self._reconnect_attempts = 0
        self._consecutive_failures = 0
        self._terminal_failure = False
        self._terminal_reason = None
        self._last_failure = None
        self._failure_times.clear()
        self._unstable = False
        self._stable_since = None
        self._stable_frames = 0

    def _observe_failure_locked(self, now):
        self._stable_since = None
        self._stable_frames = 0
        self._failure_times.append(now)
        while self._failure_times and now - self._failure_times[0] > 60:
            self._failure_times.popleft()
        if len(self._failure_times) >= 3:
            self._unstable = True

    def _observe_frame_locked(self, now):
        # One recovered frame never erases repeated instability. Require ten
        # continuous seconds of fresh frames and at least three observations.
        if self._stable_since is None or self._last_capture_at is None or now - self._last_capture_at > self._stale_timeout:
            self._stable_since, self._stable_frames = now, 0
        self._stable_frames += 1
        if now - self._stable_since >= 10 and self._stable_frames >= 3:
            self._unstable = False
            self._failure_times.clear()

    def get_latest(self, after_sequence: int = 0, timeout: float = 0.5):
        """Wait briefly for and return the newest frame newer than ``after_sequence``."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                if self._terminal_failure or self._should_stop():
                    return None
                if (self._latest is not None and self._latest.sequence > after_sequence
                        and time.monotonic() - self._latest.captured_at_monotonic <= self._stale_timeout):
                    return self._latest
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(min(remaining, 0.05))

    def snapshot(self) -> dict:
        """Return non-sensitive acquisition health and freshness counters."""
        with self._condition:
            capture_fps = self._capture_fps
            frame_age = None if self._last_capture_at is None else time.monotonic() - self._last_capture_at
            stale = frame_age is not None and frame_age > self._stale_timeout
            if self._last_capture_at is None or stale:
                capture_fps = 0.0
            return {
                "state": "stale" if stale and self._state == "running" else self._state,
                "frame_stale": stale,
                "frame_age_seconds": frame_age,
                "stale_timeout": self._stale_timeout,
                "capture_sequence": self._sequence,
                "capture_timestamp_monotonic": self._last_capture_at,
                "capture_fps": capture_fps,
                "capture_read_ms": self._last_read_ms,
                "read_failures": self._read_failures,
                "open_failures": self._open_failures,
                "reconnect_attempts": self._reconnect_attempts,
                "consecutive_failures": self._consecutive_failures,
                "last_failure": self._last_failure,
                "terminal_failure": self._terminal_failure,
                "terminal_reason": self._terminal_reason,
                "capture_stage": (self._terminal_reason or
                                  getattr(self._capture, "stage", self._state)),
                "unstable": self._unstable,
                "stability_seconds": 10,
            }

    @property
    def terminal_failure(self) -> bool:
        with self._condition:
            return self._terminal_failure

    @property
    def cleanup_confirmed(self) -> bool:
        """True only when the reader and any native helper are both gone."""
        thread = self._thread
        with self._capture_lock:
            capture = self._capture
        return ((thread is None or (thread.ident is not None and not thread.is_alive()))
                and (capture is None or getattr(capture, "cleanup_confirmed", False) is True))

    def stop(self, timeout: float = 2.0) -> bool:
        """Bound shutdown; callers retain ownership until the return value is True."""
        deadline = time.monotonic() + max(0.0, timeout)
        self._stop_event.set()
        with self._condition:
            if self._state not in {"idle", "stopped", "failed"}:
                self._state = "stopping"
            self._condition.notify_all()

        self._release_capture(timeout=max(0.0, timeout * 0.8))
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

        with self._condition:
            if not self.cleanup_confirmed:
                self._cleanup_failed()
            else:
                self._state = "stopped"
            self._condition.notify_all()
        return self.cleanup_confirmed

    def _capture_loop(self) -> None:
        retries_since_frame = 0
        initial_open = True
        try:
            while not self._should_stop():
                if self._startup_expired():
                    return
                with self._capture_lock:
                    capture = self._capture
                if capture is None:
                    if not initial_open:
                        if retries_since_frame >= self._max_retries:
                            self._set_terminal_failure()
                            return
                        with self._condition:
                            self._state = "reconnecting"
                            self._condition.notify_all()
                        if self._wait_for_stop(self._reconnect_delay):
                            return
                        if self._startup_expired():
                            return
                        with self._condition:
                            self._reconnect_attempts += 1
                        retries_since_frame += 1

                    capture = self._open_capture()
                    initial_open = False
                    if capture is None:
                        with self._condition:
                            self._open_failures += 1
                            self._observe_failure_locked(time.monotonic())
                            self._consecutive_failures += 1
                            if self._last_failure not in {"helper_ready_timeout", "helper_initialization_failed",
                                                          "open_timeout", "cleanup_failed", "startup_timeout"}:
                                self._last_failure = "open_failed"
                            self._condition.notify_all()
                        if self._terminal_failure:
                            return
                    continue

                read_started = time.perf_counter()
                read_raised = False
                try:
                    ok, frame = capture.read()
                except CaptureCleanupError:
                    self._cleanup_failed()
                    return
                except CaptureStopped:
                    return
                except CaptureTimeout as exc:
                    ok, frame = False, None
                    with self._condition:
                        self._last_failure = f"{exc.operation}_timeout"
                except Exception:
                    # Backend failures (notably FFmpeg/RTSP disconnects) can
                    # raise instead of returning ``False``. Treat both forms
                    # as the same recoverable read failure.
                    ok, frame = False, None
                    read_raised = True
                read_ms = (time.perf_counter() - read_started) * 1000.0
                captured_at = getattr(capture, "captured_at_monotonic", None)
                if not isinstance(captured_at, (int, float)):
                    captured_at = time.monotonic()

                if not ok or frame is None or getattr(frame, "size", 0) == 0:
                    with self._condition:
                        self._last_read_ms = read_ms
                        self._read_failures += 1
                        self._observe_failure_locked(time.monotonic())
                        self._consecutive_failures += 1
                        if self._last_failure not in {"read_timeout", "first_read_timeout"}:
                            backend_failure = getattr(capture, "last_failure", None)
                            self._last_failure = ("read_exception" if read_raised else
                                                  backend_failure if backend_failure in {"read_failed", "read_exception"}
                                                  else "read_failed")
                        self._latest = None
                        self._state = "reconnecting"
                        self._condition.notify_all()
                    if not self._release_capture():
                        self._cleanup_failed()
                        return
                    continue

                with self._condition:
                    # The accepted startup allowance applies until the first
                    # fresh frame only; later disconnects retain finite retries.
                    self._startup_deadline = None
                    retries_since_frame = 0
                    self._observe_frame_locked(captured_at)
                    self._sequence += 1
                    self._last_capture_at = captured_at
                    self._last_read_ms = read_ms
                    self._latest = CapturedFrame(self._sequence, captured_at, frame)
                    self._consecutive_failures = 0
                    self._last_failure = None
                    self._state = "running"
                    self._fps_window_frames += 1
                    elapsed = captured_at - self._fps_window_started
                    if elapsed >= 1.0:
                        self._capture_fps = self._fps_window_frames / elapsed
                        self._fps_window_frames = 0
                        self._fps_window_started = captured_at
                    self._condition.notify_all()
        finally:
            clean = self._release_capture()
            with self._condition:
                if not clean:
                    self._cleanup_failed()
                elif self._terminal_failure:
                    self._state = "failed"
                elif self._should_stop():
                    self._state = "stopped"
                self._condition.notify_all()

    def _open_capture(self):
        capture = None
        try:
            capture = CaptureProcess(self._source, width=self._width, height=self._height,
                                     target_fps=self._target_fps, open_timeout=self._open_timeout,
                                     read_timeout=self._read_timeout, owner_state=self._owner_state,
                                     startup_deadline=self._startup_deadline,
                                     stop_event=self._CombinedStop(self))
            with self._capture_lock:
                self._capture = capture
            if self._should_stop():
                self._release_capture()
                return None
            if not capture.isOpened():
                with self._condition:
                    self._last_failure = getattr(capture, "last_failure", None)
                if not self._release_capture():
                    self._cleanup_failed()
                return None
            # Backends may ignore this property, but supported webcam/FFmpeg
            # backends will avoid retaining stale frames in their internal queue.
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if self._width is not None:
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
            if self._height is not None:
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
            if self._target_fps is not None:
                capture.set(cv2.CAP_PROP_FPS, self._target_fps)
            return capture
        except CaptureCleanupError:
            self._cleanup_failed()
            return None
        except CaptureStopped:
            return None
        except CaptureTimeout as exc:
            with self._condition:
                self._last_failure = f"{exc.operation}_timeout"
            if not self._release_capture():
                self._cleanup_failed()
            return None
        except Exception:
            if not self._release_capture():
                self._cleanup_failed()
            return None

    def _release_capture(self, timeout: float = 1.0) -> bool:
        if self._startup_deadline is not None:
            timeout = min(timeout, max(0.0, self._startup_deadline - time.monotonic()))
        with self._capture_lock:
            capture = self._capture
        if capture is not None:
            try:
                if getattr(capture, "bounded_release", False) is True:
                    cleaned = capture.release(timeout=timeout)
                else:
                    # Injected synthetic adapters may retain VideoCapture's
                    # no-argument release signature. Production uses helpers.
                    cleaned = capture.release() is not False
            except Exception:
                cleaned = False
            if not cleaned:
                return False
            with self._capture_lock:
                if self._capture is capture:
                    self._capture = None
        return True

    def _cleanup_failed(self):
        with self._condition:
            self._last_failure = "cleanup_failed"
            self._terminal_failure = True
            self._terminal_reason = "cleanup_failed"
            self._state = "stop_timeout"
            self._latest = None
            self._condition.notify_all()

    def _set_terminal_failure(self) -> None:
        if self._should_stop():
            return
        with self._condition:
            self._terminal_failure = True
            self._terminal_reason = "retry_exhausted"
            self._state = "failed"
            self._condition.notify_all()

    def _startup_expired(self):
        if self._startup_deadline is None or time.monotonic() < self._startup_deadline - 0.25:
            return False
        with self._condition:
            self._terminal_failure = True
            self._terminal_reason = "startup_timeout"
            self._last_failure = "startup_timeout"
            self._state = "failed"
            self._condition.notify_all()
        return True

    class _CombinedStop:
        """Expose both local Stop and the enclosing lease/session cancellation."""
        def __init__(self, owner):
            self.owner = owner

        def is_set(self):
            return self.owner._should_stop()

    def _should_stop(self) -> bool:
        if self._stop_event.is_set():
            return True
        external = self._external_stop_event
        return bool(external is not None and external.is_set())

    def _wait_for_stop(self, seconds: float) -> bool:
        if self._should_stop():
            return True
        deadline = time.monotonic() + max(0.0, seconds)
        if self._startup_deadline is not None:
            deadline = min(deadline, self._startup_deadline - 0.25)
        while not self._should_stop():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # The internal event wakes immediately. Polling at a short bounded
            # interval also observes a multiprocessing/external stop event.
            if self._stop_event.wait(min(remaining, 0.05)):
                return True
        return True
