"""
CSD FARS -- Streaming Generator
-------------------------------
Provides the MJPEG streaming generator required by Flask to serve
the live webcam feed to the browser UI.
"""

import time
from core.ai_pipeline import get_latest_frame_packet


def _get_session_context(class_id):
    """Return the parent-process context currently registered for a session."""
    from workers.session_manager import session_manager

    return session_manager.active_sessions.get(class_id)


def generate_mjpeg_stream(class_id, access_check=None, access_check_interval=0.5, view="recognition"):
    """
    Return an iterator for one exact session generation's JPEG stream.

    Resolve the context before returning so the HTTP request is bound to the
    exact worker generation that existed when the route handled it,
    rather than whichever worker happens to exist on the iterator's first
    socket write.
    """
    bound_context = _get_session_context(class_id)
    if bound_context is None:
        return iter(())

    return _generate_bound_mjpeg_stream(
        class_id,
        bound_context,
        access_check,
        access_check_interval,
        view,
    )


def _generate_bound_mjpeg_stream(
    class_id,
    bound_context,
    access_check,
    access_check_interval,
    view,
):
    """Yield multipart frames while ``bound_context`` remains current."""
    last_sequence = None
    last_access_check = 0.0
    try:
        while True:
            # A class ID may be reused by a resumed/restarted occurrence. The
            # existing HTTP response belongs only to the worker that was
            # present when iteration began; the browser can establish a fresh
            # response for a replacement worker.
            if _get_session_context(class_id) is not bound_context:
                return
            if access_check is not None:
                now = time.monotonic()
                if now - last_access_check >= access_check_interval:
                    if not access_check():
                        return
                    last_access_check = now
            packet = get_latest_frame_packet(
                class_id,
                after_sequence=last_sequence,
                expected_context=bound_context,
                **({"view": "live"} if view == "live" else {}),
            )

            if _get_session_context(class_id) is not bound_context:
                return
            if packet is not None and packet[0] != last_sequence:
                sequence, frame = packet
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n'
                       + f'Content-Length: {len(frame)}\r\n'.encode('ascii')
                       + f'X-Frame-Sequence: {sequence}\r\n\r\n'.encode('ascii')
                       + frame + b'\r\n')
                last_sequence = sequence
            else:
                # Poll at a bounded cadence, but publish only when recognition
                # has written a new sequence to shared memory.
                time.sleep(0.02)
    except GeneratorExit:
        pass
