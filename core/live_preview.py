"""Publish a smooth, unannotated preview from an already-owned camera capture."""

from __future__ import annotations

import math
import threading
import time

import cv2


class LivePreviewPublisher:
    """Encode the newest capture independently of recognition work.

    ``capture`` is borrowed: this class never starts, stops, or opens a camera.
    ``publish`` receives ``(jpeg, capture_sequence, captured_at_monotonic,
    (width, height), publication_fps)``. It must publish into a latest-only slot,
    rather than append to a queue. Frames contain no recognition annotations.

    A successful ``stop`` confirms the thread and all its callbacks have ended.
    Native encoding and the supplied callback cannot be forcibly interrupted;
    a timed-out stop returns False until a later stop confirms their completion.
    """

    def __init__(
        self,
        capture,
        publish,
        *,
        height=720,
        quality=85,
        target_fps=20,
        monotonic=time.monotonic,
        cadence_clock=time.perf_counter,
        sleeper=time.sleep,
        stale_timeout=2.0,
    ):
        if not callable(publish):
            raise TypeError("Preview publication callback must be callable")
        height = int(height)
        quality = int(quality)
        target_fps = float(target_fps)
        stale_timeout = float(stale_timeout)
        if height < 1 or not 1 <= quality <= 100:
            raise ValueError("Invalid preview dimensions or JPEG quality")
        if not math.isfinite(target_fps) or not 0 < target_fps <= 60:
            raise ValueError("Preview frame rate must be between 0 and 60")
        if not math.isfinite(stale_timeout) or stale_timeout <= 0:
            raise ValueError("Preview freshness deadline must be positive")

        self._capture = capture
        self._publish = publish
        self._height = height
        self._quality = quality
        self._interval = 1.0 / target_fps
        self._clock = monotonic
        # Windows/Python 3.11 monotonic timestamps may advance in coarse ticks.
        # Use the high-resolution clock only for cadence, keeping capture-age
        # comparisons in the capture's original monotonic clock domain.
        self._cadence_clock = cadence_clock
        self._sleep = sleeper
        self._stale_timeout = stale_timeout
        self._stop_event = threading.Event()
        self._failed = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._thread = None

    @property
    def failed(self):
        """Whether an unexpected capture, encoding, or publication error occurred."""
        return self._failed.is_set()

    def start(self):
        """Start once, or restart only after the previous thread fully ended."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return not self._stop_event.is_set() and not self.failed
            self._stop_event.clear()
            self._failed.clear()
            thread = threading.Thread(
                target=self._run, name="live-camera-preview", daemon=True
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                self._failed.set()
                return False
        return True

    def stop(self, timeout=2.0):
        """Fence further publication and wait a bounded time for confirmed exit."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lifecycle_lock:
            self._stop_event.set()
            thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return not thread.is_alive()

    def _fresh(self, captured_at):
        age = self._clock() - captured_at
        return math.isfinite(age) and 0 <= age <= self._stale_timeout

    def _run(self):
        sequence = 0
        next_due = self._cadence_clock()
        fps_started = None
        fps_frames = 0
        publication_fps = 0.0
        try:
            while not self._stop_event.is_set():
                remaining = next_due - self._cadence_clock()
                if remaining > 0:
                    # Python 3.11's high-resolution sleep avoids Windows event
                    # waits rounding a 50 ms frame interval up to ~62.5 ms.
                    # Short slices keep stop responsive without a system-wide
                    # timer-resolution change or a CPU spin loop.
                    self._sleep(min(remaining, 0.02))
                    continue

                captured = self._capture.get_latest(
                    after_sequence=sequence, timeout=0.2
                )
                if self._stop_event.is_set():
                    return
                if captured is None or captured.sequence <= sequence:
                    # A stopped/failed capture can return immediately instead
                    # of waiting its timeout. Avoid a busy loop while its owner
                    # coordinates shutdown or recovery.
                    self._stop_event.wait(0.01)
                    continue
                sequence = captured.sequence
                if not self._fresh(captured.captured_at_monotonic):
                    continue

                # Account for copying, encoding, and publication in the frame
                # interval. A slow iteration establishes a new deadline rather
                # than bursting through missed slots or replaying old frames.
                next_due = self._cadence_clock() + self._interval
                source = captured.frame
                source_height, source_width = source.shape[:2]
                if source_height > self._height:
                    width = max(1, round(source_width * self._height / source_height))
                    frame = cv2.resize(
                        source, (width, self._height), interpolation=cv2.INTER_AREA
                    )
                else:
                    # Encoding receives its own array even when no resize is
                    # needed. Other consumers may still be using the original.
                    frame = source.copy()
                encoded, buffer = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self._quality]
                )
                if not encoded:
                    continue
                jpeg = buffer.tobytes()
                if self._stop_event.is_set() or not self._fresh(
                    captured.captured_at_monotonic
                ):
                    continue

                now = self._cadence_clock()
                if fps_started is None:
                    fps_started = now
                else:
                    fps_frames += 1
                    elapsed = now - fps_started
                    if elapsed >= 1.0:
                        publication_fps = fps_frames / elapsed
                        fps_started = now
                        fps_frames = 0
                self._publish(
                    jpeg,
                    captured.sequence,
                    captured.captured_at_monotonic,
                    (frame.shape[1], frame.shape[0]),
                    publication_fps,
                )
        except Exception:
            # Exceptions may contain sensitive camera information. Expose only
            # a failure flag; the owner decides how to recover this worker.
            self._failed.set()
