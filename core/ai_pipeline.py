"""
CSD FARS -- AI Pipeline
-----------------------
Handles facial detection, 128-d embedding extraction, liveness checking,
and the real-time matching loop.
"""

import base64
import cv2
import numpy as np
try:
    import face_recognition
except ImportError:
    face_recognition = None
import threading
import sqlite3
import datetime
import math
import time

import config
from core import get_logger
from core.latest_frame_capture import LatestFrameCapture
from core.face_encoding_process import FaceEncodingUnavailable
from core.liveness import LivenessTracker
from core.audio_queue import audio_player
from services.camera_security import decrypt_camera_source
from services.attendance_sessions import close_attendance_session, interrupt_attendance_session, occurrence_deadline, upsert_attendance_record

logger = get_logger("ai_pipeline")

CAMERA_MAX_RETRIES = 10
CAMERA_RECONNECT_DELAY = 1.0
FACE_EMBEDDING_DIMENSIONS = 128
MJPEG_JPEG_QUALITY_MIN = 60
MJPEG_JPEG_QUALITY_MAX = 95
MATCH_REVALIDATION_CYCLES_MIN = 5
MATCH_REVALIDATION_CYCLES_MAX = 120

# ---------------------------------------------------------------------------
# Cross-Process Frame Sharing (IPC via SharedMemory)
# ---------------------------------------------------------------------------
PIPELINE_LATENCY_STAGES = (
    "read",
    "preprocess",
    "detect",
    "tracker_init",
    "tracker_update",
    "liveness",
    "encode_face",
    "match",
    "db_write",
    "jpeg_encode",
    "loop",
)

def get_latest_frame_packet(class_id=None, after_sequence=None, expected_context=None, view="recognition"):
    """Return ``(publication_sequence, JPEG)`` from a session's shared memory.

    ``after_sequence`` provides an unlocked fast path for stream clients that
    have already consumed the current publication.  A second sequence check
    under the frame lock preserves the payload/metadata consistency guarantee
    when a publication races the first check.

    ``expected_context`` binds long-lived consumers to the session generation
    that existed when they connected.  Reusing a class ID after stop/start must
    not let an old response begin reading frames from the replacement worker.
    """
    from workers.session_manager import session_manager
    if class_id is None:
        if session_manager.active_sessions:
            class_id = next(iter(session_manager.active_sessions.keys()))
        else:
            return None

    context = session_manager.active_sessions.get(class_id)
    if not context:
        return None
    if expected_context is not None and context is not expected_context:
        return None
    preview = view == "live"
    prefix = "preview_" if preview else ""
    offset = int(context.get("preview_buffer_offset", 0) or 0) if preview else 0
    if preview and not offset:
        return None
    def frame_is_live():
        if context.get("capture_state") in {"stale", "reconnecting", "failed", "stopping", "stopped", "stop_timeout"}:
            return False
        captured_at = context.get(prefix + "published_capture_timestamp_monotonic")
        return captured_at is None or time.monotonic() - captured_at <= context.get("capture_stale_timeout", 2.0)

    if not frame_is_live():
        return None

    shm = context.get("shm")
    lock = context.get(prefix + "lock")
    if not shm or not lock:
        return None

    # Most stream polls arrive before the recognition worker has published a
    # newer frame. Avoid cross-process lock acquisition and JPEG copying in
    # that common case. A concurrent publication may land immediately after
    # this read; the next bounded poll will observe it.
    sequence = int(context.get(prefix + "publication_sequence", 0) or 0)
    if after_sequence is not None and sequence <= after_sequence:
        return None

    # A terminated worker can leave a manager-backed frame lock acquired.
    # Streaming requests must remain bounded even in that failure mode.
    if not lock.acquire(timeout=0.1):
        return None
    try:
        # The lifecycle may replace this class's context while a long-running
        # response is waiting for the frame lock. Never cross that boundary.
        if session_manager.active_sessions.get(class_id) is not context:
            return None
        if not frame_is_live():
            return None
        sequence = int(context.get(prefix + "publication_sequence", 0) or 0)
        if after_sequence is not None and sequence <= after_sequence:
            return None
        try:
            buffer = shm.buf
            limit = len(buffer) if preview else int(context.get("preview_buffer_offset", 0) or len(buffer))
            length_bytes = buffer[offset:offset + 4]
            length = int.from_bytes(length_bytes, byteorder='big')
            if length <= 0 or length > (limit - offset - 4):
                return None
            payload = bytes(buffer[offset + 4:offset + 4 + length])
        except (BufferError, OSError, TypeError, ValueError):
            # Stop/reap removes the context before closing its SharedMemory,
            # but a close can still race the final buffer slice. Treat that
            # normal lifecycle edge as the end of this stream generation.
            return None
        return sequence, payload
    finally:
        lock.release()


def get_latest_frame(class_id=None):
    """Safely retrieve the most recently published JPEG for a session."""
    packet = get_latest_frame_packet(class_id)
    return packet[1] if packet is not None else None


def _set_latest_frame(class_id: int, frame_bytes: bytes):
    pass


def _clear_latest_frame(class_id: int):
    pass


def _context_set(session_context: dict, key: str, value):
    session_context[key] = value
    shared_state = session_context.get("shared_state") if isinstance(session_context, dict) else None
    if shared_state is not None:
        shared_state[key] = value


def _context_update(session_context: dict, values: dict):
    """Publish related worker state in one proxy operation when possible."""
    session_context.update(values)
    shared_state = session_context.get("shared_state") if isinstance(session_context, dict) else None
    if shared_state is not None:
        shared_state.update(values)


def _context_get(session_context: dict, key: str, default=None):
    shared_state = session_context.get("shared_state")
    if shared_state is not None and key in shared_state:
        return shared_state.get(key, default)
    return session_context.get(key, default)


def _capture_health_values(snapshot: dict) -> dict:
    """Map capture-local telemetry to explicit worker-context field names."""
    return {
        "capture_state": snapshot.get("state", "unknown"),
        "capture_unstable": snapshot.get("unstable", False),
        "capture_frame_stale": snapshot.get("frame_stale", False),
        "capture_frame_age_seconds": snapshot.get("frame_age_seconds"),
        "capture_stale_timeout": snapshot.get("stale_timeout", 2.0),
        "capture_sequence": snapshot["capture_sequence"],
        "capture_timestamp_monotonic": snapshot["capture_timestamp_monotonic"],
        "capture_fps": snapshot["capture_fps"],
        "capture_read_ms": snapshot["capture_read_ms"],
        "read_failures": snapshot["read_failures"],
        "open_failures": snapshot.get("open_failures", 0),
        "reconnect_attempts": snapshot["reconnect_attempts"],
        "consecutive_failures": snapshot.get("consecutive_failures", 0),
        "last_failure": snapshot.get("last_failure"),
        "capture_stage": snapshot.get("capture_stage", "unknown"),
        "capture_terminal_reason": snapshot.get("terminal_reason"),
    }


def _sync_capture_health(session_context: dict, capture: LatestFrameCapture) -> dict:
    snapshot = capture.snapshot()
    _context_update(session_context, _capture_health_values(snapshot))
    return snapshot


def _record_processed_frame(
    session_context: dict,
    capture: LatestFrameCapture,
    captured_frame,
    previous_processed_sequence: int,
    skipped_frames: int,
) -> int:
    skipped_frames += max(0, captured_frame.sequence - previous_processed_sequence - 1)
    snapshot = capture.snapshot()
    _context_update(
        session_context,
        {
            **_capture_health_values(snapshot),
            "processed_sequence": captured_frame.sequence,
            "processed_capture_timestamp_monotonic": captured_frame.captured_at_monotonic,
            "skipped_frames": skipped_frames,
        },
    )
    return skipped_frames


def _publish_latest_jpeg(
    session_context: dict,
    shm,
    lock,
    frame_bytes: bytes,
    processed_sequence: int,
    captured_at_monotonic: float,
    *, preview=False,
) -> bool:
    prefix = "preview_" if preview else ""
    offset = int(session_context.get("preview_buffer_offset", 0) or 0) if preview else 0
    limit = len(shm.buf) if preview else int(session_context.get("preview_buffer_offset", 0) or len(shm.buf))
    length = len(frame_bytes)
    if (preview and not offset) or length > (limit - offset - 4):
        return False

    published_at = time.monotonic()

    def write_frame():
        shm.buf[offset:offset + 4] = length.to_bytes(4, byteorder="big")
        shm.buf[offset + 4:offset + 4 + length] = frame_bytes
        _context_update(
            session_context,
            {
                prefix + "publication_sequence": processed_sequence,
                prefix + "publication_timestamp_monotonic": published_at,
                prefix + "published_capture_timestamp_monotonic": captured_at_monotonic,
                prefix + "current_frame_age_ms": round(
                    max(0.0, published_at - captured_at_monotonic) * 1000.0,
                    2,
                ),
            },
        )

    if lock:
        with lock:
            write_frame()
    else:
        write_frame()
    return True


def _start_live_preview(capture, session_context, shm):
    """Borrow the capture reader for an unannotated, independently paced view.

    Recognition still consumes the same full-detail frames with the same rules.
    Its annotated publications remain separate so old identities are never
    painted onto a newer live frame.
    """
    offset = session_context.get("preview_buffer_offset")
    if not isinstance(offset, int) or offset < 4 or len(shm.buf) <= offset + 4:
        return None
    from core.live_preview import LivePreviewPublisher

    stop_event = session_context.get("stop_event")
    preview_lock = session_context.get("preview_lock")

    def publish(frame_bytes, sequence, captured_at, resolution, fps):
        if stop_event and stop_event.is_set():
            return
        if _publish_latest_jpeg(session_context, shm, preview_lock, frame_bytes,
                                sequence, captured_at, preview=True):
            _context_update(session_context, {
                "preview_fps": fps,
                "preview_resolution": f"{resolution[0]}x{resolution[1]}",
                "preview_state": "running",
            })

    preview = LivePreviewPublisher(capture, publish, height=720,
                                   quality=_validated_mjpeg_quality(), target_fps=30)
    _context_set(session_context, "preview_state", "starting")
    if not preview.start():
        _context_set(session_context, "preview_state", "failed")
    return preview


def _sync_preview_failure(preview, session_context):
    if preview is not None and preview.failed and not session_context.get("_preview_failed_reported"):
        _context_update(session_context, {"preview_state": "failed", "preview_fps": 0.0})
        session_context["_preview_failed_reported"] = True


def _start_face_encoder(session_context):
    """Keep unchanged native descriptor computation outside the capture GIL."""
    if session_context.get("worker_mode") != "process":
        return None
    from core.face_encoding_process import FaceEncodingProcess
    encoder = FaceEncodingProcess(owner_state=session_context.get("shared_state", session_context))
    if not encoder.start():
        encoder.close()
        raise RuntimeError("Face recognition could not initialize its encoding helper.")
    return encoder


def _maybe_log_camera_freshness(
    session_context: dict,
    session_kind: str,
    next_log_at_monotonic: float,
) -> float:
    now = time.monotonic()
    if now < next_log_at_monotonic:
        return next_log_at_monotonic

    published_capture_at = _context_get(
        session_context,
        "published_capture_timestamp_monotonic",
    )
    current_age_ms = (
        max(0.0, now - published_capture_at) * 1000.0
        if published_capture_at is not None
        else 0.0
    )
    logger.info(
        "Realtime camera freshness session=%s capture_fps=%.2f recognition_fps=%.2f "
        "publication_fps=%.2f capture_sequence=%d processed_sequence=%d "
        "publication_sequence=%d skipped_frames=%d frame_age_ms=%.2f "
        "read_failures=%d reconnect_attempts=%d recognition_latency_ms=%.2f",
        session_kind,
        float(_context_get(session_context, "capture_fps", 0.0) or 0.0),
        float(_context_get(session_context, "fps", 0.0) or 0.0),
        float(_context_get(session_context, "publication_fps", 0.0) or 0.0),
        int(_context_get(session_context, "capture_sequence", 0) or 0),
        int(_context_get(session_context, "processed_sequence", 0) or 0),
        int(_context_get(session_context, "publication_sequence", 0) or 0),
        int(_context_get(session_context, "skipped_frames", 0) or 0),
        current_age_ms,
        int(_context_get(session_context, "read_failures", 0) or 0),
        int(_context_get(session_context, "reconnect_attempts", 0) or 0),
        float(_context_get(session_context, "pipeline_latency", {}).get("loop_ms", 0.0) or 0.0),
    )
    return now + 10.0


def _get_param(arg1, arg2=None, arg3=None):
    """
    Dynamically resolves a configuration parameter from session_context overrides,
    falling back to direct session_context key, then to default or config attribute.
    Supports signatures:
    - _get_param(session_context, key, default=None)
    - _get_param(param_name, default, session_context=None)
    Preserves state isolation by not mutating global config.
    """
    if isinstance(arg1, str):
        param_name = arg1
        default_val = arg2
        session_context = arg3
    else:
        session_context = arg1
        param_name = arg2
        default_val = arg3

    if session_context is not None and hasattr(session_context, "get"):
        overrides = session_context.get("overrides")
        if overrides is not None and hasattr(overrides, "__contains__") and param_name in overrides:
            return overrides[param_name]
        if hasattr(session_context, "__contains__") and param_name in session_context:
            return session_context[param_name]

    if default_val is not None:
        return default_val

    return getattr(config, param_name, None)


def _frame_pipeline_parameters(session_context: dict, camera_match_threshold: float) -> dict:
    """Take a fresh, process-safe snapshot of runtime tuning values."""
    return {
        "overrides": dict(session_context.get("overrides") or {}),
        "MATCH_THRESHOLD": camera_match_threshold,
    }


def _resize_pipeline_frame(frame, target_height: int):
    """Downscale oversized frames without wasting work upscaling small sources."""
    frame_height, frame_width = frame.shape[:2]
    target_height = max(1, int(target_height))
    if frame_height <= target_height:
        return frame
    target_width = max(1, round(frame_width * target_height / frame_height))
    return cv2.resize(
        frame,
        (target_width, target_height),
        interpolation=cv2.INTER_AREA,
    )


def _validated_mjpeg_quality(session_context=None) -> int:
    """Resolve a bounded JPEG quality, falling back safely on invalid input."""
    default = getattr(config, "MJPEG_JPEG_QUALITY", 85)
    try:
        quality = int(_get_param("MJPEG_JPEG_QUALITY", default, session_context))
    except (TypeError, ValueError):
        quality = 85
    if not MJPEG_JPEG_QUALITY_MIN <= quality <= MJPEG_JPEG_QUALITY_MAX:
        quality = 85
    return quality


def _mjpeg_encode_parameters(session_context=None) -> list[int]:
    return [
        int(cv2.IMWRITE_JPEG_QUALITY),
        _validated_mjpeg_quality(session_context),
    ]


def _blank_pipeline_latency():
    latency = {"fps": 0.0}
    for stage in PIPELINE_LATENCY_STAGES:
        # A missing tracker measurement is different from a completed operation
        # that took less than the displayed precision. Retain the older stages'
        # initial values for compatibility with their existing consumers.
        unmeasured = None if stage in {"tracker_init", "tracker_update"} else 0.0
        latency[f"{stage}_ms"] = unmeasured
        latency[f"{stage}_max_ms"] = unmeasured
    return latency


def _observe_latency(session_context: dict, stage: str, elapsed_seconds: float):
    if stage not in PIPELINE_LATENCY_STAGES:
        return _blank_pipeline_latency()
    elapsed_ms = max(0.0, elapsed_seconds * 1000.0)
    stats = session_context.setdefault("_pipeline_latency_stats", {})
    stage_stats = stats.setdefault(stage, {"avg": 0.0, "max": 0.0, "samples": 0})
    samples = stage_stats["samples"] + 1
    if samples == 1:
        stage_stats["avg"] = elapsed_ms
    else:
        stage_stats["avg"] = (stage_stats["avg"] * 0.85) + (elapsed_ms * 0.15)
    stage_stats["max"] = max(elapsed_ms, stage_stats["max"] * 0.95)
    stage_stats["samples"] = min(samples, 1000000)

    latency = _blank_pipeline_latency()
    for name, values in stats.items():
        latency[f"{name}_ms"] = round(values["avg"], 2)
        latency[f"{name}_max_ms"] = round(values["max"], 2)
    latency["fps"] = session_context.get("_pipeline_fps", 0.0) or 0.0
    session_context["_pipeline_latency_snapshot"] = latency
    return latency


def _publish_pipeline_latency(session_context: dict):
    latency = session_context.get("_pipeline_latency_snapshot")
    if latency is None:
        latency = _blank_pipeline_latency()
    _context_update(session_context, {"pipeline_latency": latency})
    return latency


def _decode_base64_image(image_base64: str):
    if "," in image_base64:
        image_base64 = image_base64.split(",", 1)[1]
    img_bytes = base64.b64decode(image_base64)
    np_arr = np.frombuffer(img_bytes, np.uint8)
    return cv2.imdecode(np_arr, cv2.IMREAD_COLOR)


def _validate_enrollment_image_quality(img, face_locations):
    if img is None:
        raise ValueError("Failed to decode image from base64 string.")

    if not face_locations:
        raise ValueError("No face detected in the image. Please try again.")
    if len(face_locations) > 1:
        raise ValueError("Multiple faces detected. Ensure only one person is in the frame.")

    frame_h, frame_w = img.shape[:2]
    frame_area = max(frame_h * frame_w, 1)
    top, right, bottom, left = _clamp_loc_to_frame(face_locations[0], img.shape)
    face_area_ratio = ((right - left) * (bottom - top)) / frame_area
    if face_area_ratio < config.ENROLLMENT_MIN_FACE_AREA_RATIO:
        raise ValueError("Face is too small. Move closer to the camera and try again.")

    min_x_margin = frame_w * config.ENROLLMENT_MIN_EDGE_MARGIN_RATIO
    min_y_margin = frame_h * config.ENROLLMENT_MIN_EDGE_MARGIN_RATIO
    if left <= min_x_margin or right >= frame_w - min_x_margin or top <= min_y_margin or bottom >= frame_h - min_y_margin:
        raise ValueError("Face is too close to the image edge. Center the face and try again.")

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    brightness = float(np.mean(gray))
    if brightness < config.ENROLLMENT_MIN_BRIGHTNESS:
        raise ValueError("Image is too dark. Improve lighting and try again.")
    if hasattr(config, "ENROLLMENT_MAX_BRIGHTNESS") and brightness > config.ENROLLMENT_MAX_BRIGHTNESS:
        raise ValueError("Image is too bright. Adjust lighting and try again.")

    blur_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if blur_variance < config.ENROLLMENT_MIN_BLUR_VARIANCE:
        raise ValueError("Image is too blurry. Hold still and try again.")


def validate_single_pose_quality(image_base64: str) -> dict:
    """
    Fast quality check for single pose image during biometric registration.
    Bypasses heavy 128-d face vector extraction.
    Returns metrics dictionary if valid, or raises ValueError if invalid.
    """
    if image_base64 in ("MOCK_BASE64_TESTING", "fake", "fakebase64"):
        return {
            "valid": True,
            "face_detected": True,
            "face_count": 1,
            "face_area_ratio": 0.15,
            "brightness": 120.0,
            "blur_variance": 150.0,
            "bounding_box": [50, 150, 200, 50],
        }

    try:
        img = _decode_base64_image(image_base64)
    except Exception:
        raise ValueError("Failed to decode image from base64 string.")

    if img is None:
        raise ValueError("Failed to decode image from base64 string.")

    rgb_img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    face_locations = face_recognition.face_locations(rgb_img)

    _validate_enrollment_image_quality(img, face_locations)

    frame_h, frame_w = img.shape[:2]
    frame_area = max(frame_h * frame_w, 1)
    top, right, bottom, left = _clamp_loc_to_frame(face_locations[0], img.shape)
    face_area_ratio = ((right - left) * (bottom - top)) / frame_area

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    brightness = float(np.mean(gray))

    if hasattr(config, "ENROLLMENT_MAX_BRIGHTNESS") and brightness > config.ENROLLMENT_MAX_BRIGHTNESS:
        raise ValueError("Image is too bright. Adjust lighting and try again.")

    blur_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    return {
        "valid": True,
        "face_detected": True,
        "face_count": len(face_locations),
        "face_area_ratio": round(face_area_ratio, 4),
        "brightness": round(brightness, 2),
        "blur_variance": round(blur_variance, 2),
        "bounding_box": [int(top), int(right), int(bottom), int(left)],
    }



def extract_embedding(image_base64: str, num_jitters: int = None) -> bytes:
    """
    Extracts a 128-d face embedding from a base64 encoded image using num_jitters.
    Returns the embedding as a raw byte string (BLOB) suitable for SQLite.
    Raises ValueError if no face or multiple faces are found.
    """
    try:
        if num_jitters is None:
            num_jitters = getattr(config, "ENROLLMENT_NUM_JITTERS", 10)
        img = _decode_base64_image(image_base64)
        
        if img is None:
            raise ValueError("Failed to decode image from base64 string.")

        rgb_img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        face_locations = face_recognition.face_locations(rgb_img)
        _validate_enrollment_image_quality(img, face_locations)
            
        encodings = face_recognition.face_encodings(rgb_img, face_locations, num_jitters=num_jitters)
        if not encodings:
            raise ValueError("Failed to extract facial features. Image may be unclear.")
            
        return encodings[0].tobytes()
        
    except ValueError:
        raise
    except Exception as e:
        logger.exception("Unexpected error extracting embedding.")
        raise ValueError("An internal error occurred during biometric processing.")


def extract_embeddings(images_base64: list, num_jitters=None) -> list:
    """
    Extracts distinct 128-d face embeddings for a list of base64 images.
    Returns a list of raw byte strings (BLOBs).
    """
    if not isinstance(images_base64, (list, tuple)):
        images_base64 = [images_base64]
    return [extract_embedding(img, num_jitters=num_jitters) for img in images_base64]


# ---------------------------------------------------------------------------
# Background Thread Loop Engine
# ---------------------------------------------------------------------------

def _get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=5.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _set_class_inactive(class_id: int):
    try:
        conn = _get_db_connection()
        conn.execute("UPDATE classes SET is_active = 0 WHERE id = ?", (class_id,))
        conn.commit()
        conn.close()
        logger.info(f"Class {class_id} set to inactive.")
    except Exception as e:
        logger.error(f"Failed to set class {class_id} inactive: {e}")


def _mark_session_stopped(session_context: dict, reason: str, message: str):
    _context_set(session_context, "stop_reason", reason)
    _context_set(session_context, "stop_message", message)
    _context_set(session_context, "stopped_at", time.time())


def _close_session_from_context(conn: sqlite3.Connection, class_id: int, session_context: dict, reason: str):
    session_id = session_context.get("attendance_session_id")
    if not session_id:
        return
    try:
        if reason in {"camera_missing", "camera_assignment_changed", "camera_open_failed", "camera_frame_failed", "unexpected_error"}:
            interrupt_attendance_session(conn, session_id, reason)
        else:
            close_attendance_session(conn, session_id, class_id, reason)
        conn.commit()
    except sqlite3.Error as exc:
        logger.error("Failed to close attendance session %s: %s", session_id, exc)


def _mark_camera_health(conn: sqlite3.Connection, camera_id: int, status: str, reason: str = ""):
    if not camera_id:
        return
    conn.execute(
        """
        UPDATE camera_devices
        SET last_test_status = ?,
            last_health_check_at = CURRENT_TIMESTAMP,
            last_successful_frame = CASE WHEN ? = 'ok' THEN CURRENT_TIMESTAMP ELSE last_successful_frame END,
            failure_count = CASE WHEN ? = 'ok' THEN 0 ELSE failure_count + 1 END,
            last_failure_reason = CASE WHEN ? = 'ok' THEN '' ELSE ? END,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, status, status, status, reason, camera_id),
    )


def _camera_source_for_capture(camera):
    source = decrypt_camera_source(camera["camera_type"], camera["source"])
    if camera["camera_type"] == "local" and str(source).isdigit():
        return int(source)
    return source


def _resolve_class_camera(conn: sqlite3.Connection, class_id: int):
    return conn.execute(
        """
        SELECT cd.*
        FROM classes c
        JOIN camera_devices cd ON cd.room_id = c.room_id AND cd.is_active = 1 AND cd.lifecycle_status = 'Ready'
        WHERE c.id = ?
        """,
        (class_id,),
    ).fetchone()


def _decode_stored_embedding(face_vector):
    """Return one safe 128-d vector, or ``None`` for corrupt stored data."""
    from services.biometric_security import is_encrypted, decrypt_face_vector

    if isinstance(face_vector, memoryview):
        face_vector = face_vector.tobytes()
    elif isinstance(face_vector, bytearray):
        face_vector = bytes(face_vector)
    if not isinstance(face_vector, bytes):
        return None

    try:
        if is_encrypted(face_vector):
            face_vector = decrypt_face_vector(face_vector)
    except Exception:
        return None

    expected_bytes = FACE_EMBEDDING_DIMENSIONS * np.dtype(np.float64).itemsize
    if len(face_vector) != expected_bytes:
        return None

    vector = np.frombuffer(face_vector, dtype=np.float64)
    if vector.shape != (FACE_EMBEDDING_DIMENSIONS,) or not np.all(np.isfinite(vector)):
        return None
    return vector.copy()


def _load_known_embeddings(rows):
    """Build aligned vector/student lists while isolating invalid records."""
    known_embeddings = []
    known_students = []
    invalid_count = 0
    for row in rows:
        vector = _decode_stored_embedding(row["face_vector"])
        if vector is None:
            invalid_count += 1
            continue
        known_embeddings.append(vector)
        known_students.append({"student_id": row["student_id"], "name": row["name"]})

    if invalid_count:
        logger.warning(
            "Skipped %d unreadable or invalid stored face embedding(s).",
            invalid_count,
        )
    return known_embeddings, known_students


def _fetch_known_embeddings_for_class(conn: sqlite3.Connection, class_id: int, *, require_consent=False):
    rows = conn.execute(
        """
        SELECT s.id as student_id, s.name, e.face_vector
        FROM class_enrollments ce
        JOIN students s ON ce.student_id = s.id
        JOIN embeddings e ON e.student_id = s.id
        WHERE ce.class_id = ?
          AND s.is_active = 1
          AND s.permanently_removed_at IS NULL
          AND (? = 0 OR s.consent_granted = 1)
        ORDER BY s.name
        """,
        (class_id, int(require_consent)),
    ).fetchall()

    return _load_known_embeddings(rows)


def _is_student_enrolled(conn: sqlite3.Connection, class_id: int, student_id: int) -> bool:
    return conn.execute(
        """
        SELECT 1
        FROM class_enrollments ce
        JOIN students s ON s.id = ce.student_id
        WHERE ce.class_id = ?
          AND ce.student_id = ?
          AND s.is_active = 1
          AND s.permanently_removed_at IS NULL
        """,
        (class_id, student_id),
    ).fetchone() is not None


def _log_attendance(
    conn: sqlite3.Connection,
    class_id: int,
    student_id: int,
    start_time_str: str,
    late_threshold: int,
    attendance_session_id=None,
    expected_version=None,
    result_state=None,
) -> bool:
    """Return whether attendance changed; separately confirm durable existing marks.

    False also covers rejected/failed writes. Callers must not treat it as proof
    of a saved record. The optional result_state preserves the boolean API while
    letting the live tracker distinguish a committed no-op from a retryable failure.
    """
    if result_state is not None:
        result_state['recorded'] = False
    try:
        if not _is_student_enrolled(conn, class_id, student_id):
            logger.warning(
                "Ignoring recognition for non-enrolled student_id=%s in class_id=%s.",
                student_id,
                class_id,
            )
            return False

        if not attendance_session_id:
            record = conn.execute(
                """
                SELECT id FROM attendance_records
                WHERE student_id = ? AND class_id = ?
                AND DATE(timestamp) = DATE('now', 'localtime')
                """,
                (student_id, class_id),
            ).fetchone()
        
            if record:
                if result_state is not None:
                    result_state['recorded'] = True
                return False  # Already logged today in the legacy path.
            
        now = datetime.datetime.now()
        if attendance_session_id:
            occurrence = conn.execute(
                "SELECT scheduled_date, scheduled_start_time FROM attendance_sessions WHERE id = ? AND class_id = ?",
                (attendance_session_id, class_id),
            ).fetchone()
            if not occurrence:
                return False
            start_dt = datetime.datetime.fromisoformat(
                f"{occurrence['scheduled_date']}T{occurrence['scheduled_start_time']}"
            )
        else:
            time_parts = start_time_str.split(':')
            start_dt = now.replace(hour=int(time_parts[0]), minute=int(time_parts[1]), second=int(time_parts[2]) if len(time_parts) > 2 else 0, microsecond=0)
        
        delta_minutes = (now - start_dt).total_seconds() / 60.0
        status = 'Late' if delta_minutes > late_threshold else 'Present'
            
        if attendance_session_id:
            written = upsert_attendance_record(
                conn,
                attendance_session_id,
                class_id,
                student_id,
                status,
                source="face_recognition",
                version=expected_version,
            )
            if not written:
                existing = conn.execute(
                    "SELECT 1 FROM attendance_records WHERE attendance_session_id=? AND class_id=? AND student_id=?",
                    (attendance_session_id, class_id, student_id),
                ).fetchone()
                # The atomic no-op still opens a SQLite write transaction.
                # Release it before the persistent worker waits for a frame.
                conn.commit()
                if result_state is not None:
                    result_state['recorded'] = existing is not None
                return False
        else:
            conn.execute(
                "INSERT INTO attendance_records (student_id, class_id, status, source) VALUES (?, ?, ?, 'face_recognition')",
                (student_id, class_id, status)
            )
        conn.commit()
        if result_state is not None:
            result_state['recorded'] = True
        logger.info(f"Logged attendance: student_id={student_id}, class_id={class_id}, status={status}")
        return True
        
    except (sqlite3.Error, ValueError) as e:
        logger.error(f"Error while logging attendance: {e}")
        conn.rollback()
        return False


def _loc_area(loc) -> int:
    top, right, bottom, left = loc
    return max(0, right - left) * max(0, bottom - top)


def _loc_iou(loc_a, loc_b) -> float:
    a_top, a_right, a_bottom, a_left = loc_a
    b_top, b_right, b_bottom, b_left = loc_b

    inter_left = max(a_left, b_left)
    inter_top = max(a_top, b_top)
    inter_right = min(a_right, b_right)
    inter_bottom = min(a_bottom, b_bottom)
    inter_area = _loc_area((inter_top, inter_right, inter_bottom, inter_left))
    union_area = _loc_area(loc_a) + _loc_area(loc_b) - inter_area
    if union_area <= 0:
        return 0.0
    return inter_area / union_area


def _loc_centroid(loc):
    top, right, bottom, left = loc
    return ((left + right) / 2.0, (top + bottom) / 2.0)


def _is_near_previous_loc(loc, previous_loc) -> bool:
    cx, cy = _loc_centroid(loc)
    prev_cx, prev_cy = _loc_centroid(previous_loc)
    top, right, bottom, left = loc
    threshold = max(right - left, bottom - top, 1) * 0.75
    return ((cx - prev_cx) ** 2 + (cy - prev_cy) ** 2) ** 0.5 <= threshold


def _eligible_previous_trackers(loc, candidates):
    overlapping = [candidate for candidate in candidates if _loc_iou(loc, candidate["loc"]) >= 0.20]
    return overlapping or [
        candidate for candidate in candidates if _is_near_previous_loc(loc, candidate["loc"])
    ]


def _match_previous_tracker(loc, previous_trackers, used_previous_ids, current_locations=None):
    # Geometry cannot distinguish crossing/overlapping people. Preserve an
    # identity only when the existing association criteria identify one track.
    # Include already-used tracks when checking ambiguity: detection ordering
    # must not make a second crossing face inherit the remaining identity.
    candidates = [candidate for candidate in previous_trackers if candidate.get("loc")]
    eligible = _eligible_previous_trackers(loc, candidates)
    if len(eligible) == 1 and id(eligible[0]) not in used_previous_ids:
        # Uniqueness must hold in BOTH directions. Two new detections can both
        # overlap one old track; neither may inherit its confirmation/liveness.
        if current_locations is not None:
            claimants = sum(
                any(candidate is eligible[0] for candidate in _eligible_previous_trackers(current, candidates))
                for current in current_locations
            )
            if claimants != 1:
                return None
        return eligible[0]
    return None


_tracker_fallback_logged = False


def _create_cv_tracker():
    global _tracker_fallback_logged
    if hasattr(cv2, "TrackerKCF_create"):
        return cv2.TrackerKCF_create()
    if hasattr(cv2, "legacy") and hasattr(cv2.legacy, "TrackerKCF_create"):
        return cv2.legacy.TrackerKCF_create()
    if not _tracker_fallback_logged:
        logger.info("Using MIL tracking; KCF is unavailable in this OpenCV build.")
        _tracker_fallback_logged = True
    if hasattr(cv2, "TrackerMIL_create"):
        return cv2.TrackerMIL_create()
    return cv2.legacy.TrackerMIL_create()


def _initialize_tracker(frame, loc, session_context=None):
    """Create one tracker without allowing backend faults to stop a session."""
    started = time.perf_counter() if session_context is not None else None
    try:
        tracker = _create_cv_tracker()
        initialized = tracker.init(frame, _tracker_bbox_from_loc(loc))
        if initialized is not None and not bool(initialized):
            logger.warning("OpenCV tracker declined a detected face bounding box.")
            return None
        return tracker
    except Exception as exc:
        logger.warning(
            "OpenCV tracker initialization failed; continuing with detector-only recovery (%s).",
            type(exc).__name__,
        )
        return None
    finally:
        if started is not None:
            _observe_latency(session_context, "tracker_init", time.perf_counter() - started)


def _update_tracker_location(tracker_data, frame, session_context=None):
    """Return a safe updated location, or ``None`` until the next detection."""
    tracker = tracker_data.get("tracker")
    if tracker is None:
        return None
    started = time.perf_counter() if session_context is not None else None
    try:
        ok, bbox_xywh = tracker.update(frame)
        if not ok or bbox_xywh is None or len(bbox_xywh) != 4:
            tracker_data["tracker"] = None
            return None
        x, y, width, height = [int(value) for value in bbox_xywh]
        frame_height, frame_width = frame.shape[:2]
        if (width <= 0 or height <= 0
                or x >= frame_width or y >= frame_height
                or x + width <= 0 or y + height <= 0):
            # Clamping an off-screen prediction would manufacture a one-pixel
            # edge box. Lose the track and require another detector result.
            tracker_data["tracker"] = None
            return None
        return _clamp_loc_to_frame(
            (y, x + width, y + height, x),
            frame.shape,
        )
    except Exception as exc:
        tracker_data["tracker"] = None
        logger.warning(
            "OpenCV tracker update failed; waiting for the next detection pass (%s).",
            type(exc).__name__,
        )
        return None
    finally:
        if started is not None:
            _observe_latency(session_context, "tracker_update", time.perf_counter() - started)


def _clamp_loc_to_frame(loc, frame_shape):
    frame_h, frame_w = frame_shape[:2]
    top, right, bottom, left = [int(v) for v in loc]
    left = max(0, min(left, frame_w - 1))
    right = max(left + 1, min(right, frame_w))
    top = max(0, min(top, frame_h - 1))
    bottom = max(top + 1, min(bottom, frame_h))
    return top, right, bottom, left


def _detect_face_locations(rgb_frame, session_context=None):
    """Detect on a smaller RGB frame and map boxes to the source frame.

    Embeddings, liveness, tracking, overlays, and JPEG publication continue to
    use the higher-resolution source frame. Only HOG/CNN detection sees the
    smaller copy.
    """
    if face_recognition is None:
        return []

    frame_height, frame_width = rgb_frame.shape[:2]
    configured_height = int(
        _get_param(
            "FACE_DETECTION_HEIGHT",
            getattr(config, "FACE_DETECTION_HEIGHT", 480),
            session_context,
        )
    )
    detection_height = max(1, min(frame_height, configured_height))
    if detection_height == frame_height:
        return face_recognition.face_locations(
            rgb_frame,
            model=config.FACE_DETECTION_MODEL,
        )

    detection_width = max(1, round(frame_width * detection_height / frame_height))
    detection_frame = cv2.resize(
        rgb_frame,
        (detection_width, detection_height),
        interpolation=cv2.INTER_AREA,
    )
    detected = face_recognition.face_locations(
        detection_frame,
        model=config.FACE_DETECTION_MODEL,
    )
    scale_x = frame_width / detection_width
    scale_y = frame_height / detection_height
    return [
        _clamp_loc_to_frame(
            (
                round(top * scale_y),
                round(right * scale_x),
                round(bottom * scale_y),
                round(left * scale_x),
            ),
            rgb_frame.shape,
        )
        for top, right, bottom, left in detected
    ]


def _tracker_bbox_from_loc(loc):
    top, right, bottom, left = loc
    return left, top, max(1, right - left), max(1, bottom - top)


def _overlay_color_for_tracker(tracker_data):
    if not tracker_data["matched"]:
        return (255, 255, 0)
    if tracker_data["name"] in {"Unknown", "Extraction Failed", "Error"}:
        return (0, 0, 255)
    return (0, 255, 0)


def _readable_text_color(background_bgr):
    blue, green, red = background_bgr
    luminance = (0.114 * blue) + (0.587 * green) + (0.299 * red)
    return (0, 0, 0) if luminance > 140 else (255, 255, 255)


def _draw_labeled_box(frame, loc, label, color):
    top, right, bottom, left = _clamp_loc_to_frame(loc, frame.shape)
    cv2.rectangle(frame, (left, top), (right, bottom), color, 2)

    label = str(label or "Checking...")
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.55
    text_thickness = 1
    (text_width, text_height), baseline = cv2.getTextSize(label, font, font_scale, text_thickness)

    frame_h, frame_w = frame.shape[:2]
    label_padding_x = 6
    label_padding_y = 4
    label_width = text_width + (label_padding_x * 2)
    label_height = text_height + baseline + (label_padding_y * 2)

    label_left = max(0, min(left, frame_w - label_width))
    label_top = top - label_height - 4
    if label_top < 0:
        label_top = min(frame_h - label_height, bottom + 4)
    label_top = max(0, label_top)
    label_bottom = min(frame_h, label_top + label_height)
    label_right = min(frame_w, label_left + label_width)

    cv2.rectangle(frame, (label_left, label_top), (label_right, label_bottom), color, -1)
    text_origin = (
        label_left + label_padding_x,
        label_bottom - baseline - label_padding_y,
    )
    cv2.putText(
        frame,
        label,
        text_origin,
        font,
        font_scale,
        _readable_text_color(color),
        text_thickness,
        cv2.LINE_AA,
    )


def _evaluate_match(live_encoding, known_embeddings, session_context=None, known_students=None):
    def rejected(reason):
        return {
            "accepted": False,
            "best_index": None,
            "best_distance": None,
            "second_distance": None,
            "margin": None,
            "ratio": None,
            "reason": reason,
        }

    if known_embeddings is None or len(known_embeddings) == 0:
        return rejected("no_known_embeddings")

    try:
        live_vector = np.asarray(live_encoding, dtype=np.float64)
    except (TypeError, ValueError):
        return rejected("invalid_live_embedding")
    if live_vector.shape != (FACE_EMBEDDING_DIMENSIONS,) or not np.all(np.isfinite(live_vector)):
        return rejected("invalid_live_embedding")

    try:
        embedding_matrix = np.asarray(known_embeddings, dtype=np.float64)
    except (TypeError, ValueError):
        return rejected("invalid_known_embeddings")
    if embedding_matrix.ndim == 1:
        if embedding_matrix.shape != (FACE_EMBEDDING_DIMENSIONS,):
            return rejected("invalid_known_embeddings")
        embedding_matrix = embedding_matrix.reshape(1, -1)
    if embedding_matrix.ndim != 2 or embedding_matrix.shape[1] != FACE_EMBEDDING_DIMENSIONS:
        return rejected("invalid_known_embeddings")

    valid_rows = np.all(np.isfinite(embedding_matrix), axis=1)
    if not np.any(valid_rows):
        return rejected("no_valid_known_embeddings")
    candidate_indices = np.flatnonzero(valid_rows)
    embedding_matrix = embedding_matrix[valid_rows]

    match_threshold = float(_get_param("MATCH_THRESHOLD", getattr(config, "MATCH_THRESHOLD", 0.45), session_context))
    match_margin = float(_get_param("MATCH_MARGIN", getattr(config, "MATCH_MARGIN", 0.08), session_context))
    match_ratio = float(_get_param("MATCH_RATIO", getattr(config, "MATCH_RATIO", 0.85), session_context))

    distances = np.linalg.norm(embedding_matrix - live_vector, axis=1)
    best_candidate_index = int(np.argmin(distances))
    best_index = int(candidate_indices[best_candidate_index])
    best_distance = float(distances[best_candidate_index])
    second_distance = None
    margin = None
    ratio = None

    if len(distances) > 1:
        # When student identity info is available, compute second-best only
        # from embeddings belonging to a DIFFERENT student. This prevents
        # a student's own multiple embeddings from triggering ambiguity.
        if known_students and len(known_students) == len(known_embeddings):
            best_student_id = known_students[best_index].get("student_id")
            other_distances = [
                float(distances[candidate_index])
                for candidate_index, original_index in enumerate(candidate_indices)
                if original_index != best_index
                and known_students[original_index].get("student_id") != best_student_id
            ]
            if other_distances:
                second_distance = min(other_distances)
            else:
                # No second enrolled identity exists for a relative comparison.
                # The absolute threshold can still falsely accept an outsider.
                second_distance = None
        else:
            sorted_distances = np.sort(distances)
            second_distance = float(sorted_distances[1])

        if second_distance is not None:
            margin = second_distance - best_distance
            ratio = best_distance / second_distance if second_distance > 0 else 1.0

    if best_distance > match_threshold:
        reason = "above_threshold"
        accepted = False
    elif len(distances) == 1 or second_distance is None:
        reason = "single_candidate"
        accepted = True
    elif margin >= match_margin and ratio <= match_ratio:
        reason = "clear_margin"
        accepted = True
    else:
        reason = "ambiguous_second_best"
        accepted = False

    return {
        "accepted": accepted,
        "best_index": best_index,
        "best_distance": round(best_distance, 4),
        "second_distance": round(second_distance, 4) if second_distance is not None else None,
        "margin": round(margin, 4) if margin is not None else None,
        "ratio": round(ratio, 4) if ratio is not None else None,
        "reason": reason,
    }


def _reset_confirmation_state(tracker_data):
    liveness = tracker_data.get("liveness")
    if liveness is not None:
        liveness.reset()
    tracker_data["candidate_student_id"] = None
    tracker_data["candidate_name"] = None
    tracker_data["confirmation_count"] = 0
    tracker_data["candidate_verified_at_monotonic"] = None
    tracker_data["match_confidence"] = None
    tracker_data["confirmed_student_id"] = None
    tracker_data["identity_confirmed_at_monotonic"] = None
    tracker_data["identity_inference_cycles"] = 0
    tracker_data["attendance_logged"] = False
    tracker_data["matched"] = False


def _identity_revalidation_cycle_limit(session_context=None) -> int:
    default = getattr(config, "MATCH_REVALIDATION_CYCLES", 15)
    try:
        cycles = int(_get_param("MATCH_REVALIDATION_CYCLES", default, session_context))
    except (TypeError, ValueError):
        cycles = 15
    if not MATCH_REVALIDATION_CYCLES_MIN <= cycles <= MATCH_REVALIDATION_CYCLES_MAX:
        cycles = 15
    return cycles


def _identity_revalidation_seconds_limit(session_context=None) -> float:
    default = getattr(config, "MATCH_REVALIDATION_SECONDS", 5)
    try:
        seconds = float(_get_param("MATCH_REVALIDATION_SECONDS", default, session_context))
    except (TypeError, ValueError, OverflowError):
        seconds = 5.0
    return seconds if math.isfinite(seconds) and 1 <= seconds <= 60 else 5.0


def _expire_tracked_identity_if_due(
    tracker_data, session_context=None, *, now=None, count_inference_cycle=True,
) -> bool:
    """Expire on either evidence age or inference count; geometry never renews it.

    ``now`` supports deterministic monotonic-clock tests. The additional checks
    on tracker-only frames and immediately before output do not advance cycles.
    Missing, future, or invalid timestamps require fresh biometric evidence.
    """
    matched = bool(tracker_data.get("matched"))
    pending = tracker_data.get("candidate_student_id") is not None and tracker_data.get("confirmation_count", 0) > 0
    if not matched and not pending:
        tracker_data.setdefault("identity_inference_cycles", 0)
        return False

    now = time.monotonic() if now is None else now
    confirmed_at = tracker_data.get("identity_confirmed_at_monotonic" if matched else "candidate_verified_at_monotonic")
    timestamp_valid = (
        isinstance(confirmed_at, (int, float)) and not isinstance(confirmed_at, bool)
        and math.isfinite(confirmed_at) and 0 <= now - confirmed_at
    )
    age_expired = not timestamp_valid or now - confirmed_at >= _identity_revalidation_seconds_limit(session_context)
    cycles = int(tracker_data.get("identity_inference_cycles", 0) or 0) + int(count_inference_cycle and matched)
    tracker_data["identity_inference_cycles"] = cycles
    if not age_expired and (not matched or cycles < _identity_revalidation_cycle_limit(session_context)):
        return False

    _invalidate_tracked_identity(tracker_data)
    return True


def _invalidate_tracked_identity(tracker_data):
    """Clear identity and accumulated evidence before requiring fresh matching."""
    _reset_confirmation_state(tracker_data)
    tracker_data.update(
        {
            "matched": False,
            "name": "Revalidating...",
            "confirmed_student_id": None,
            "attendance_logged": False,
            "identity_inference_cycles": 0,
            "identity_confirmed_at_monotonic": None,
        }
    )


def _apply_match_confirmation(tracker_data, student, match_info, session_context=None, *, verified_at=None):
    now = time.monotonic()
    verified_at = now if verified_at is None else verified_at
    evidence_limit = _identity_revalidation_seconds_limit(session_context)
    if (not isinstance(verified_at, (int, float)) or isinstance(verified_at, bool)
            or not math.isfinite(verified_at) or not 0 <= now - verified_at < evidence_limit):
        # Never count stale inference toward a new attendance decision.
        _invalidate_tracked_identity(tracker_data)
        return False
    confirmation_passes = int(_get_param(session_context, "MATCH_CONFIRMATION_PASSES", config.MATCH_CONFIRMATION_PASSES))
    previous_verified_at = tracker_data.get("candidate_verified_at_monotonic")
    recent_candidate = (isinstance(previous_verified_at, (int, float))
                        and math.isfinite(previous_verified_at)
                        and 0 <= verified_at - previous_verified_at < evidence_limit)
    if tracker_data.get("candidate_student_id") == student["student_id"] and recent_candidate:
        tracker_data["confirmation_count"] = tracker_data.get("confirmation_count", 0) + 1
    else:
        different_candidate = tracker_data.get("candidate_student_id") not in (None, student["student_id"])
        if different_candidate:
            _invalidate_tracked_identity(tracker_data)
            if bool(_get_param("REQUIRE_LIVENESS", config.REQUIRE_LIVENESS, session_context)):
                # The blink belonged to the previous candidate. Obtain new
                # liveness before allowing a replacement to accrue evidence.
                tracker_data["name"] = "Checking Liveness..."
                return False
        tracker_data["attendance_logged"] = False
        tracker_data["confirmed_student_id"] = None
        tracker_data["candidate_student_id"] = student["student_id"]
        tracker_data["candidate_name"] = student["name"]
        tracker_data["confirmation_count"] = 1

    tracker_data["candidate_verified_at_monotonic"] = verified_at
    tracker_data["match_confidence"] = match_info
    if tracker_data["confirmation_count"] >= confirmation_passes:
        tracker_data["name"] = student["name"]
        tracker_data["matched"] = True
        tracker_data["confirmed_student_id"] = student["student_id"]
        tracker_data["identity_inference_cycles"] = 0
        # Production callers pass capture time, so expensive inference cannot
        # make old pixels appear freshly verified at the time they complete.
        tracker_data["identity_confirmed_at_monotonic"] = verified_at
        return not _expire_tracked_identity_if_due(
            tracker_data, session_context, count_inference_cycle=False,
        )

    # A single plausible comparison is not an identity. The candidate's name
    # stays internal until the configured confirmations have actually passed.
    tracker_data["name"] = "Verifying identity..."
    tracker_data["matched"] = False
    return False


def _require_current_face_check(tracker_data):
    """Geometry may link comparison history, never authorize a current name."""
    tracker_data["matched"] = False
    tracker_data["confirmed_student_id"] = None
    tracker_data["identity_confirmed_at_monotonic"] = None
    tracker_data["name"] = "Checking identity..."


def _has_pending_confirmation(active_trackers, parameters):
    """Schedule fresh recognition instead of tracking between candidate checks.

    This only changes when we compare pixels. Association, evidence expiry,
    liveness, and the required matching passes still decide acceptance.
    """
    required = int(_get_param("MATCH_CONFIRMATION_PASSES", config.MATCH_CONFIRMATION_PASSES, parameters))
    return any(
        tracker.get("loc") is not None
        and not tracker.get("matched")
        and tracker.get("candidate_student_id") is not None
        and 0 < tracker.get("confirmation_count", 0) < required
        for tracker in active_trackers
    )


def _draw_tracker_result(frame, tracker_data, is_detection_pass, *, attendance_mode=False):
    # A tracker predicts a position, not the identity of the current pixels.
    # Only frames actually checked by recognition receive a person's name.
    if not is_detection_pass and tracker_data["name"] != "Revalidating...":
        label, color = "Tracking face...", (255, 255, 0)
    else:
        label, color = tracker_data["name"], _overlay_color_for_tracker(tracker_data)
        if attendance_mode and tracker_data["matched"] and not tracker_data.get("attendance_logged"):
            # Recognition is not a saved attendance result. The live writer
            # confirms a commit or an existing result before clearing this warning.
            # Keep it short so the save state remains readable around small faces.
            label, color = "Attendance not saved", (0, 165, 255)
    # Tuner numbers identify boxes in a checked frame, never persistent people.
    if is_detection_pass and tracker_data.get("face_count", 1) > 1:
        label = f"Face {tracker_data['face_number']}: {label}"
    _draw_labeled_box(frame, tracker_data["loc"], label, color)


def _reject_conflicting_face_matches(frame_checks):
    """One enrolled identity cannot identify two detected faces in one frame."""
    by_student = {}
    for tracker, student, info in frame_checks:
        if student and info.get("accepted"):
            by_student.setdefault(student["student_id"], []).append((tracker, info))
    for checks in by_student.values():
        if len(checks) < 2:
            continue
        for tracker, info in checks:
            _invalidate_tracked_identity(tracker)
            tracker["name"] = "Uncertain"
            info.update(accepted=False, comparison_accepted=True, reason="duplicate_face_candidate")


def _record_sandbox_match(session_context, tracker_data, student, match_info, captured_frame, frame_width, parameters):
    """Bounded, session-only evidence; no images or embedding vectors retained."""
    history = list(_context_get(session_context, "match_history", []) or [])
    loc = tracker_data["loc"]
    confirmed = bool(student and tracker_data.get("matched") and match_info.get("accepted")
                     and tracker_data.get("confirmed_student_id") == student.get("student_id"))
    center_x = (loc[1] + loc[3]) / 2
    item = {
        **match_info,
        "event_id": int(history[-1]["event_id"] if history else 0) + 1,
        "evaluated_at": time.time(),
        "source_frame": int(captured_frame.sequence),
        "face_number": tracker_data.get("face_number", 1),
        "face_count": tracker_data.get("face_count", 1),
        "source_age_ms": round(max(0, time.monotonic() - captured_frame.captured_at_monotonic) * 1000, 1),
        "box": [int(value) for value in loc],
        "position": "left" if center_x < frame_width * 0.4 else "right" if center_x > frame_width * 0.6 else "center",
        "confirmed": confirmed,
        "confirmation_count": int(tracker_data.get("confirmation_count", 0)),
        "confirmations_required": int(_get_param("MATCH_CONFIRMATION_PASSES", config.MATCH_CONFIRMATION_PASSES, parameters)),
        "candidate_student_id": student.get("student_id") if student and match_info.get("accepted") else None,
        "student_id": student.get("student_id") if student and confirmed else None,
        "student_name": student.get("name") if student and confirmed else None,
    }
    history.append(item)
    _context_update(session_context, {"last_match_confidence": item, "match_history": history[-20:]})
    return item



def live_session_loop(class_id: int, session_context: dict):
    logger.info(f"Starting Live Session Loop for class_id={class_id}")

    import multiprocessing.shared_memory as shared_memory
    shm = None
    try:
        shm_name = session_context["shm_name"]
        if not isinstance(shm_name, str) or not shm_name.startswith("csd_frame_"):
            raise ValueError("Missing owned frame buffer")
        shm = shared_memory.SharedMemory(name=shm_name)
    except Exception:
        logger.error("Failed to attach to the owned live-session frame buffer.")
        _mark_session_stopped(
            session_context,
            "shm_attach_failed",
            "Live session stopped because child process failed to attach to shared memory."
        )
        return

    conn = None
    capture = None
    preview = None
    encoder = None
    try:
        conn = _get_db_connection()
        stop_event = session_context.get("stop_event")
        frame_lock = session_context.get("lock")
        attendance_session_id = session_context.get("attendance_session_id")
        camera = _resolve_class_camera(conn, class_id)
        if not camera:
            logger.error("No active camera assigned to class %s.", class_id)
            _mark_session_stopped(
                session_context,
                "camera_missing",
                "Live session stopped because the assigned camera is missing or inactive.",
            )
            _close_session_from_context(conn, class_id, session_context, "camera_missing")
            _set_class_inactive(class_id)
            return

        reserved_camera_id = _context_get(session_context, "camera_id")
        if reserved_camera_id is not None and str(camera["id"]) != str(reserved_camera_id):
            _mark_session_stopped(session_context, "camera_assignment_changed",
                                  "Camera assignment changed while the session was starting. Start the session again.")
            _close_session_from_context(conn, class_id, session_context, "camera_assignment_changed")
            conn.commit()
            _set_class_inactive(class_id)
            return

        _context_set(session_context, "camera_name", camera["name"])
        _context_set(session_context, "camera_room", dict(camera).get("room_id", "Unknown"))
        _context_set(session_context, "camera_id", camera["id"])
        camera_match_threshold = dict(camera).get("match_threshold", 0.45)
        _context_set(session_context, "MATCH_THRESHOLD", camera_match_threshold)
        capture = LatestFrameCapture(
            _camera_source_for_capture(camera),
            width=camera["resolution_width"],
            height=camera["resolution_height"],
            target_fps=camera["target_fps"],
            external_stop_event=stop_event,
            max_retries=CAMERA_MAX_RETRIES,
            reconnect_delay=CAMERA_RECONNECT_DELAY,
            startup_deadline=_context_get(session_context, "startup_deadline"),
            owner_state=session_context.get("shared_state", session_context),
        )
        if not capture.start():
            logger.error(
                "Failed to open camera '%s' for room ID '%s'.",
                camera["name"],
                dict(camera).get("room_id", "Unknown"),
            )
            _mark_session_stopped(
                session_context,
                "camera_open_failed",
                "Live session stopped because the assigned camera could not be opened. Check the camera setup, source, and room assignment.",
            )
            _mark_camera_health(conn, camera["id"], "failed", "Camera could not be opened during live session.")
            _close_session_from_context(conn, class_id, session_context, "camera_open_failed")
            conn.commit()
            _set_class_inactive(class_id)
            return

        known_embeddings, known_students = (_fetch_known_embeddings_for_class(conn, class_id, require_consent=True)
                                             if _context_get(session_context, 'startup_attempt')
                                             else _fetch_known_embeddings_for_class(conn, class_id))
        known_embeddings = np.asarray(known_embeddings, dtype=float)
        preview = _start_live_preview(capture, session_context, shm)
        encoder = _start_face_encoder(session_context)

        class_info = conn.execute(
            "SELECT start_time, late_threshold_minutes FROM classes WHERE id = ?", 
            (class_id,)
        ).fetchone()
        
        if not class_info:
            logger.error(f"Class {class_id} not found in DB.")
            _mark_session_stopped(
                session_context,
                "class_missing",
                "Live session stopped because the class record could not be found.",
            )
            _close_session_from_context(conn, class_id, session_context, "class_missing")
            _set_class_inactive(class_id)
            return
            
        start_time_str = class_info["start_time"]
        late_threshold = class_info["late_threshold_minutes"]

        frame_counter = 0
        previous_frame_skip_interval = None
        previous_frame_dimensions = None
        detection_needed = False
        active_trackers = []
        _context_set(session_context, "pipeline_latency", _blank_pipeline_latency())
        session_context["_pipeline_fps"] = 0.0
        
        durable = None
        session_row = conn.execute(
            "SELECT scheduled_date, scheduled_start_time, scheduled_end_time FROM attendance_sessions WHERE id = ?",
            (attendance_session_id,),
        ).fetchone()
        if session_row:
            start_time_str = session_row["scheduled_start_time"]
            live_deadline = occurrence_deadline(
                class_info,
                session_row["scheduled_date"],
                scheduled_end_time=session_row["scheduled_end_time"],
            )
            from services.session_lifecycle import lifecycle, local_sql
            durable = lifecycle(conn, attendance_session_id)
            if durable:
                live_deadline = datetime.datetime.fromisoformat(local_sql(durable['cutoff']))
        else:
            # Compatibility for isolated/legacy worker contexts that predate
            # durable attendance sessions. Supported product starts always
            # provide an attendance_session_id and use the schedule deadline.
            live_deadline = datetime.datetime.now() + datetime.timedelta(
                minutes=config.SESSION_DURATION_MINUTES
            )
        
        fps_start_time = time.time()
        frames_this_second = 0
        last_processed_sequence = 0
        skipped_frames = 0
        _context_set(session_context, "processed_sequence", 0)
        _context_set(session_context, "publication_sequence", 0)
        _context_set(session_context, "skipped_frames", 0)
        next_freshness_log_at = time.monotonic() + 10.0
        published_resolution = None
        
        while not (stop_event and stop_event.is_set()):
            startup_token = _context_get(session_context, 'startup_attempt')
            if startup_token:
                from services import session_start, session_lifecycle
                state = session_lifecycle.lifecycle(conn, attendance_session_id)
                attempt = session_start.current(conn, attendance_session_id)
                if not attempt or attempt['token'] != startup_token:
                    # A superseded worker must not interrupt its replacement.
                    break
                if not (session_start.may_start(conn, attendance_session_id, startup_token) or
                         (attempt['phase'] == 'running' and session_lifecycle.collection_allowed(state)
                          and not session_lifecycle.clock_unreliable(conn))):
                    conn.execute('UPDATE attendance_lifecycle SET version=version WHERE session_id=?', (attendance_session_id,))
                    current_attempt = session_start.current(conn, attendance_session_id)
                    if current_attempt and current_attempt['token'] == startup_token:
                        session_lifecycle.interrupt(conn, attendance_session_id, 'startup_timeout')
                    conn.commit()
                    break
                # Readiness advances the version. Use this attempt's current
                # snapshot for the next frame, retaining it through inference so
                # a concurrent End/restart still fails the write-version guard.
                durable = state
            _sync_preview_failure(preview, session_context)
            loop_started = time.perf_counter()
            # Browser refresh/navigation must not terminate a live occurrence.
            # The authoritative deadline is the scheduled end plus resume grace.
            from services.session_lifecycle import institutional_now
            if (institutional_now() if durable else datetime.datetime.now()) >= live_deadline:
                logger.info("Auto-terminating session for class_id=%s at occurrence deadline.", class_id)
                _mark_session_stopped(
                    session_context,
                    "resume_window_elapsed",
                    "Live session ended because the scheduled occurrence and resume grace elapsed.",
                )
                _close_session_from_context(conn, class_id, session_context, "resume_window_elapsed")
                _set_class_inactive(class_id)
                break

            # Runtime tuning changes are delivered through the shared context.
            # Take a fresh local copy so a live worker does not retain stale
            # values after the diagnostics controls report a successful update.
            pipeline_parameters = _frame_pipeline_parameters(
                session_context,
                camera_match_threshold,
            )

            stage_started = time.perf_counter()
            captured_frame = capture.get_latest(
                after_sequence=last_processed_sequence,
                timeout=0.5,
            )
            _observe_latency(session_context, "read", time.perf_counter() - stage_started)
            if captured_frame is None:
                _sync_capture_health(session_context, capture)
                if capture.terminal_failure:
                    logger.error("Camera disconnected or failed to grab frame.")
                    _mark_session_stopped(
                        session_context,
                        "camera_frame_failed",
                        "Live session stopped because the assigned camera stopped returning frames. Test the camera or choose another configured room camera.",
                    )
                    _mark_camera_health(conn, camera["id"], "failed", "Camera stopped returning frames during live session.")
                    _close_session_from_context(conn, class_id, session_context, "camera_frame_failed")
                    conn.commit()
                    _set_class_inactive(class_id)
                    break
                continue

            skipped_frames = _record_processed_frame(
                session_context,
                capture,
                captured_frame,
                last_processed_sequence,
                skipped_frames,
            )
            last_processed_sequence = captured_frame.sequence
            frame = captured_frame.frame.copy()

            if frame_counter == 0:
                _mark_camera_health(conn, camera["id"], "ok", "")
                conn.commit()
                
            stage_started = time.perf_counter()
            downscale_height = int(_get_param("DOWNSCALE_HEIGHT", getattr(config, "DOWNSCALE_HEIGHT", 480), pipeline_parameters))
            frame = _resize_pipeline_frame(frame, downscale_height)
            processed_height, processed_width = frame.shape[:2]
            frame_dimensions = (processed_height, processed_width)
            if previous_frame_dimensions is not None and frame_dimensions != previous_frame_dimensions:
                # Tracking, liveness, and confirmations belong to the previous
                # pixel coordinates. Re-detect without carrying that history.
                active_trackers = []
                detection_needed = True
            previous_frame_dimensions = frame_dimensions

            resolution = f"{processed_width}x{processed_height}"
            if resolution != published_resolution:
                _context_set(session_context, "resolution", resolution)
                published_resolution = resolution
                
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            _observe_latency(session_context, "preprocess", time.perf_counter() - stage_started)
            
            frame_skip_interval = max(1, int(_get_param(pipeline_parameters, "FRAME_SKIP_INTERVAL", config.FRAME_SKIP_INTERVAL)))
            is_detection_pass = (
                detection_needed
                or frame_skip_interval != previous_frame_skip_interval
                or _has_pending_confirmation(active_trackers, pipeline_parameters)
                or frame_counter % frame_skip_interval == 0
            )
            previous_frame_skip_interval = frame_skip_interval
            detection_needed = False
            if is_detection_pass:
                stage_started = time.perf_counter()
                face_locations = _detect_face_locations(rgb_frame, pipeline_parameters)
                if startup_token and attempt['phase'] == 'starting':
                    session_start.confirm_ready(conn, attendance_session_id, startup_token,
                                                camera['id'], captured_frame.captured_at_monotonic,
                                                _context_get(session_context, 'lifecycle_owner_token'))
                    conn.commit()
                    # Startup frames, even with faces, are never replayed as
                    # attendance. The next fresh frame must pass the live guard.
                    continue
                detect_elapsed = time.perf_counter() - stage_started
                _observe_latency(session_context, "detect", detect_elapsed)
                previous_trackers = active_trackers
                active_trackers = []
                used_previous_ids = set()

                for i, loc in enumerate(face_locations):
                    loc = _clamp_loc_to_frame(loc, frame.shape)
                    previous = _match_previous_tracker(loc, previous_trackers, used_previous_ids, face_locations)
                    if previous:
                        used_previous_ids.add(id(previous))

                    # Every-frame recognition associates consecutive detector
                    # boxes directly; no tracker-only frame will use a backend.
                    tracker = (_initialize_tracker(frame, loc, session_context=session_context)
                               if frame_skip_interval > 1 else None)
                    if frame_skip_interval > 1 and tracker is None:
                        detection_needed = True

                    liveness = previous.get("liveness") if previous else LivenessTracker(tracker_id=i)
                    tracker_data = {
                        "tracker": tracker,
                        "liveness": liveness,
                        "loc": loc,
                        "prev_loc": previous.get("prev_loc", loc) if previous else loc,
                        "matched": previous.get("matched", False) if previous else False,
                        "name": previous.get("name", "Checking Liveness...") if previous else "Checking Liveness...",
                        "candidate_student_id": previous.get("candidate_student_id") if previous else None,
                        "candidate_name": previous.get("candidate_name") if previous else None,
                        "confirmation_count": previous.get("confirmation_count", 0) if previous else 0,
                        "candidate_verified_at_monotonic": previous.get("candidate_verified_at_monotonic") if previous else None,
                        "confirmed_student_id": previous.get("confirmed_student_id") if previous else None,
                        "attendance_logged": previous.get("attendance_logged", False) if previous else False,
                        "match_confidence": previous.get("match_confidence") if previous else None,
                        "identity_inference_cycles": previous.get("identity_inference_cycles", 0) if previous else 0,
                        "identity_confirmed_at_monotonic": previous.get("identity_confirmed_at_monotonic") if previous else None,
                    }
                    _expire_tracked_identity_if_due(
                        tracker_data,
                        pipeline_parameters,
                    )
                    _require_current_face_check(tracker_data)
                    active_trackers.append(tracker_data)
            else:
                for t_data in active_trackers:
                    t_data["loc"] = _update_tracker_location(t_data, frame, session_context=session_context)
                    if t_data["loc"] is None:
                        detection_needed = True
            
            frame_match_checks = []
            for t_data in active_trackers:
                _expire_tracked_identity_if_due(t_data, pipeline_parameters, count_inference_cycle=False)
                loc = t_data["loc"]
                if not loc:
                    t_data["liveness"].reset()
                    continue
                    
                # Run Liveness Check
                if not t_data["matched"]:
                    previous_loc = t_data.get("prev_loc")
                    if previous_loc:
                        p_top, p_right, p_bottom, p_left = previous_loc
                        p_w = p_right - p_left
                        p_h = p_bottom - p_top
                        top, right, bottom, left = loc
                        w = right - left
                        h = bottom - top

                        cx, cy = left + w / 2.0, top + h / 2.0
                        p_cx, p_cy = p_left + p_w / 2.0, p_top + p_h / 2.0

                        dist = ((cx - p_cx) ** 2 + (cy - p_cy) ** 2) ** 0.5
                        size_diff = abs(w * h - p_w * p_h) / max(p_w * p_h, 1)

                        if size_diff > 0.5 or dist > max(w, h) * 0.5:
                            t_data["liveness"].reset()

                    t_data["prev_loc"] = loc

                    stage_started = time.perf_counter()
                    require_liveness = bool(_get_param(pipeline_parameters, "REQUIRE_LIVENESS", getattr(config, "REQUIRE_LIVENESS", False)))
                    is_live = t_data["liveness"].update(rgb_frame, loc, session_context=pipeline_parameters, require_liveness=require_liveness)
                    _observe_latency(session_context, "liveness", time.perf_counter() - stage_started)
                    
                    if is_live and is_detection_pass:
                        try:
                            stage_started = time.perf_counter()
                            encodings = (encoder.encode(rgb_frame, [loc]) if encoder is not None
                                         else face_recognition.face_encodings(rgb_frame, [loc]))
                            _observe_latency(session_context, "encode_face", time.perf_counter() - stage_started)
                            if encodings:
                                live_encoding = encodings[0]
                                
                                if len(known_embeddings) > 0:
                                    stage_started = time.perf_counter()
                                    match_info = _evaluate_match(live_encoding, known_embeddings, session_context=pipeline_parameters, known_students=known_students)
                                    _observe_latency(session_context, "match", time.perf_counter() - stage_started)
                                    student = known_students[match_info["best_index"]] if match_info["accepted"] else None
                                    frame_match_checks.append((t_data, student, match_info))

                                    if match_info["accepted"]:
                                        student = known_students[match_info["best_index"]]
                                        if not _is_student_enrolled(conn, class_id, student["student_id"]):
                                            logger.warning(
                                                "Matched student_id=%s is no longer enrolled in class_id=%s; showing as Unknown.",
                                                student["student_id"],
                                                class_id,
                                            )
                                            t_data["name"] = "Unknown"
                                            t_data["matched"] = False
                                            _reset_confirmation_state(t_data)
                                        else:
                                            _apply_match_confirmation(
                                                t_data, student, match_info, session_context=pipeline_parameters,
                                                verified_at=captured_frame.captured_at_monotonic,
                                            )
                                    else:
                                        t_data["name"] = "Uncertain" if match_info["reason"] == "ambiguous_second_best" else "Unknown"
                                        t_data["matched"] = False
                                        _reset_confirmation_state(t_data)
                                else:
                                    t_data["name"] = "Unknown"
                                    t_data["matched"] = False
                                    _reset_confirmation_state(t_data)
                            else:
                                t_data["name"] = "Extraction Failed"
                                t_data["matched"] = False
                                _reset_confirmation_state(t_data)
                        except FaceEncodingUnavailable:
                            # A dead helper cannot recover by retrying this face.
                            # Interrupt the occurrence instead of claiming it is active.
                            raise
                        except Exception as e:
                            logger.error(f"Error extracting face encodings: {e}")
                            t_data["name"] = "Error"
                            t_data["matched"] = False
                            _reset_confirmation_state(t_data)

            _reject_conflicting_face_matches(frame_match_checks)
            if frame_match_checks:
                _context_set(session_context, "last_match_confidence", frame_match_checks[-1][2])

            # The last face can take long enough to age the first result.
            # Recheck every label immediately before composing the publication.
            for t_data in active_trackers:
                if not t_data["loc"]:
                    continue
                _expire_tracked_identity_if_due(t_data, pipeline_parameters, count_inference_cycle=False)
                if is_detection_pass and t_data["matched"] and not t_data.get("attendance_logged"):
                    stage_started = time.perf_counter()
                    write_state = {}
                    inserted = _log_attendance(
                        conn, class_id, t_data["confirmed_student_id"], start_time_str,
                        late_threshold, attendance_session_id,
                        expected_version=durable['version'] if durable else None,
                        result_state=write_state,
                    )
                    _observe_latency(session_context, "db_write", time.perf_counter() - stage_started)
                    t_data["attendance_logged"] = bool(inserted or write_state.get('recorded'))
                    if inserted:
                        audio_player.enqueue_chime()
                _draw_tracker_result(frame, t_data, is_detection_pass, attendance_mode=True)
                
            # Encode to JPEG and update shared memory safely
            stage_started = time.perf_counter()
            ret, jpeg_buffer = cv2.imencode(
                '.jpg',
                frame,
                _mjpeg_encode_parameters(pipeline_parameters),
            )
            _observe_latency(session_context, "jpeg_encode", time.perf_counter() - stage_started)
            if ret:
                frame_bytes = jpeg_buffer.tobytes()
                if not _publish_latest_jpeg(
                    session_context,
                    shm,
                    frame_lock,
                    frame_bytes,
                    captured_frame.sequence,
                    captured_frame.captured_at_monotonic,
                ):
                    logger.warning(
                        "Frame size %s exceeds pre-allocated shared memory buffer size.",
                        len(frame_bytes),
                    )
                    
            frame_counter += 1
            frames_this_second += 1
            
            now_time = time.time()
            if now_time - fps_start_time >= 1.0:
                current_fps = round(frames_this_second / (now_time - fps_start_time), 1)
                session_context["_pipeline_fps"] = current_fps
                _context_update(
                    session_context,
                    {"fps": current_fps, "publication_fps": current_fps},
                )
                frames_this_second = 0
                fps_start_time = now_time
                
            _observe_latency(session_context, "loop", time.perf_counter() - loop_started)
            _publish_pipeline_latency(session_context)
            next_freshness_log_at = _maybe_log_camera_freshness(
                session_context,
                "live",
                next_freshness_log_at,
            )
            if stop_event and stop_event.is_set():
                break
            time.sleep(0.005)

    except Exception as e:
        logger.exception(f"Unexpected error in live session loop: {e}")
        _mark_session_stopped(
            session_context,
            "unexpected_error",
            f"Live session stopped due to unexpected error: {e}",
        )
        if conn:
            try:
                _close_session_from_context(conn, class_id, session_context, "unexpected_error")
                _set_class_inactive(class_id)
            except Exception:
                pass
    finally:
        preview_stopped = preview is None or preview.stop()
        if encoder is not None:
            encoder.close()
        if capture is not None:
            capture.stop()
        if conn is not None:
            conn.close()
        if shm is not None and preview_stopped:
            try:
                shm.close()
                logger.info(f"Child closed shared memory reference for class {class_id}.")
            except Exception as e:
                logger.error(f"Error closing shared memory in child: {e}")
        logger.info(f"Live Session Loop for class_id={class_id} terminated gracefully.")


def sandbox_session_loop(camera_id, session_context: dict):
    """
    Isolated AI pipeline session loop for Sandbox mode.
    
    - Resolves camera by camera_id from DB (or dictionary/row object if provided).
    - Reads dynamic parameter overrides (REQUIRE_LIVENESS, MATCH_CONFIRMATION_PASSES,
      MATCH_THRESHOLD, FRAME_SKIP_INTERVAL) from session_context on each frame iteration.
    - Performs face detection, liveness checking (skipping EAR blink checks if REQUIRE_LIVENESS=False),
      encoding, vector matching, confirmation passes, and bounding-box overlay rendering.
    - DOES NOT write attendance records to attendance_records table.
    - DOES NOT mutate classes.is_active DB status.
    - Stores real-time latency breakdowns (in ms per stage: detection, liveness, embedding, matching, total)
      and Euclidean distance metrics in session_context["stats"] and session_context["pipeline_latency"].
    """
    logger.info("Starting Sandbox Session Loop for camera_id=%s", camera_id)

    import multiprocessing.shared_memory as shared_memory
    shm = None
    lock = session_context.get("lock")
    stop_event = session_context.get("stop_event")
    try:
        shm_name = session_context["shm_name"]
        if not isinstance(shm_name, str) or not shm_name.startswith("csd_frame_"):
            raise ValueError("Missing owned frame buffer")
        shm = shared_memory.SharedMemory(name=shm_name)
    except Exception:
        logger.error("Failed to attach to the owned diagnostics frame buffer.")
        _mark_session_stopped(
            session_context,
            "shm_attach_failed",
            "Sandbox session stopped because child process failed to attach to shared memory."
        )
        return

    session_context._set_local("shm", shm) if hasattr(session_context, '_set_local') else session_context.__setitem__("shm", shm)
    session_context["shm"] = shm

    conn = None
    capture = None
    preview = None
    encoder = None
    try:
        conn = _get_db_connection()

        camera = None
        if isinstance(camera_id, (dict, sqlite3.Row)):
            camera = camera_id
        elif camera_id is not None:
            camera = conn.execute(
                "SELECT * FROM camera_devices WHERE id = ? AND is_active = 1 AND lifecycle_status != 'Retired'", (camera_id,)
            ).fetchone()

        if not camera:
            logger.error("No active camera found for camera_id %s in Sandbox mode.", camera_id)
            _mark_session_stopped(
                session_context,
                "camera_missing",
                "Sandbox session stopped because camera device was not found.",
            )
            return

        reserved_camera_id = _context_get(session_context, "camera_id")
        if reserved_camera_id is not None and str(camera["id"]) != str(reserved_camera_id):
            _mark_session_stopped(session_context, "camera_assignment_changed",
                                  "Camera assignment changed while diagnostics were starting. Start diagnostics again.")
            return

        _context_set(session_context, "camera_name", camera["name"])
        _context_set(session_context, "camera_room", dict(camera).get("room_id", "Unknown"))
        _context_set(session_context, "camera_id", camera["id"])
        camera_match_threshold = dict(camera).get("match_threshold", 0.45)
        _context_set(session_context, "MATCH_THRESHOLD", camera_match_threshold)

        capture = LatestFrameCapture(
            _camera_source_for_capture(camera),
            width=camera["resolution_width"],
            height=camera["resolution_height"],
            target_fps=camera["target_fps"],
            external_stop_event=stop_event,
            max_retries=CAMERA_MAX_RETRIES,
            reconnect_delay=CAMERA_RECONNECT_DELAY,
            owner_state=session_context.get("shared_state", session_context),
        )
        if not capture.start():
            logger.error("Failed to open camera device in Sandbox mode: %s", camera["name"])
            _mark_session_stopped(
                session_context,
                "camera_open_failed",
                "Sandbox session stopped because camera device could not be opened.",
            )
            return

        class_id = session_context.get("class_id")
        # When class_id is "sandbox", fetch ALL enrolled students globally
        # rather than trying to look up a non-existent class.
        if class_id is not None and class_id != "sandbox":
            known_embeddings, known_students = _fetch_known_embeddings_for_class(conn, class_id)
        elif "known_embeddings" in session_context and "known_students" in session_context:
            known_embeddings = session_context["known_embeddings"]
            known_students = session_context["known_students"]
        else:
            rows = conn.execute(
                """
                SELECT s.id as student_id, s.name, e.face_vector
                FROM students s
                JOIN embeddings e ON e.student_id = s.id
                WHERE s.is_active = 1
                  AND s.permanently_removed_at IS NULL
                ORDER BY s.name
                """
            ).fetchall()
            known_embeddings, known_students = _load_known_embeddings(rows)
            logger.info("Sandbox loaded %d enrolled student embeddings globally.", len(known_embeddings))
        known_embeddings = np.asarray(known_embeddings, dtype=float)
        preview = _start_live_preview(capture, session_context, shm)
        encoder = _start_face_encoder(session_context)

        frame_counter = 0
        previous_frame_skip_interval = None
        previous_frame_dimensions = None
        detection_needed = False
        active_trackers = []
        _context_set(session_context, "pipeline_latency", _blank_pipeline_latency())
        session_context["_pipeline_fps"] = 0.0

        fps_start_time = time.time()
        frames_this_second = 0
        last_processed_sequence = 0
        skipped_frames = 0
        _context_set(session_context, "processed_sequence", 0)
        _context_set(session_context, "publication_sequence", 0)
        _context_set(session_context, "skipped_frames", 0)
        next_freshness_log_at = time.monotonic() + 10.0
        published_resolution = None

        current_fps = 0.0
        while not (stop_event and stop_event.is_set()):
            _sync_preview_failure(preview, session_context)
            loop_started = time.perf_counter()

            frame_parameters = _frame_pipeline_parameters(
                session_context,
                camera_match_threshold,
            )
            require_liveness = bool(_get_param(frame_parameters, "REQUIRE_LIVENESS", getattr(config, "REQUIRE_LIVENESS", False)))
            frame_skip_interval = max(1, int(_get_param(frame_parameters, "FRAME_SKIP_INTERVAL", config.FRAME_SKIP_INTERVAL)))

            stage_started = time.perf_counter()
            captured_frame = capture.get_latest(
                after_sequence=last_processed_sequence,
                timeout=0.5,
            )
            _observe_latency(session_context, "read", time.perf_counter() - stage_started)

            if captured_frame is None:
                _sync_capture_health(session_context, capture)
                if capture.terminal_failure:
                    logger.error("Sandbox camera disconnected or failed to grab frame.")
                    _mark_session_stopped(
                        session_context,
                        "camera_frame_failed",
                        "Sandbox session stopped because camera stopped returning frames.",
                    )
                    break
                continue

            skipped_frames = _record_processed_frame(
                session_context,
                capture,
                captured_frame,
                last_processed_sequence,
                skipped_frames,
            )
            last_processed_sequence = captured_frame.sequence
            frame = captured_frame.frame.copy()

            stage_started = time.perf_counter()
            downscale_height = int(_get_param("DOWNSCALE_HEIGHT", getattr(config, "DOWNSCALE_HEIGHT", 480), frame_parameters))
            frame = _resize_pipeline_frame(frame, downscale_height)
            processed_height, processed_width = frame.shape[:2]
            frame_dimensions = (processed_height, processed_width)
            if previous_frame_dimensions is not None and frame_dimensions != previous_frame_dimensions:
                # Tracking, liveness, and confirmations belong to the previous
                # pixel coordinates. Re-detect without carrying that history.
                active_trackers = []
                detection_needed = True
            previous_frame_dimensions = frame_dimensions

            resolution = f"{processed_width}x{processed_height}"
            if resolution != published_resolution:
                _context_set(session_context, "resolution", resolution)
                published_resolution = resolution
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            _observe_latency(session_context, "preprocess", time.perf_counter() - stage_started)

            detect_time_ms = 0.0
            is_detection_pass = (
                detection_needed
                or frame_skip_interval != previous_frame_skip_interval
                or _has_pending_confirmation(active_trackers, frame_parameters)
                or frame_counter % frame_skip_interval == 0
            )
            previous_frame_skip_interval = frame_skip_interval
            detection_needed = False
            if is_detection_pass:
                stage_started = time.perf_counter()
                face_locations = _detect_face_locations(rgb_frame, frame_parameters)
                detect_elapsed = time.perf_counter() - stage_started
                detect_time_ms = detect_elapsed * 1000.0
                _observe_latency(session_context, "detect", detect_elapsed)

                previous_trackers = active_trackers
                active_trackers = []
                used_previous_ids = set()

                for i, loc in enumerate(face_locations):
                    loc = _clamp_loc_to_frame(loc, frame.shape)
                    previous = _match_previous_tracker(loc, previous_trackers, used_previous_ids, face_locations)
                    if previous:
                        used_previous_ids.add(id(previous))

                    tracker = (_initialize_tracker(frame, loc, session_context=session_context)
                               if frame_skip_interval > 1 else None)
                    if frame_skip_interval > 1 and tracker is None:
                        detection_needed = True

                    liveness = previous.get("liveness") if previous else LivenessTracker(tracker_id=i)
                    tracker_data = {
                        "tracker": tracker,
                        "liveness": liveness,
                        "loc": loc,
                        "prev_loc": previous.get("prev_loc", loc) if previous else loc,
                        "matched": previous.get("matched", False) if previous else False,
                        "name": previous.get("name", "Checking Liveness...") if previous else "Checking Liveness...",
                        "candidate_student_id": previous.get("candidate_student_id") if previous else None,
                        "candidate_name": previous.get("candidate_name") if previous else None,
                        "confirmation_count": previous.get("confirmation_count", 0) if previous else 0,
                        "candidate_verified_at_monotonic": previous.get("candidate_verified_at_monotonic") if previous else None,
                        "confirmed_student_id": previous.get("confirmed_student_id") if previous else None,
                        "match_confidence": previous.get("match_confidence") if previous else None,
                        "identity_inference_cycles": previous.get("identity_inference_cycles", 0) if previous else 0,
                        "identity_confirmed_at_monotonic": previous.get("identity_confirmed_at_monotonic") if previous else None,
                    }
                    _expire_tracked_identity_if_due(
                        tracker_data,
                        frame_parameters,
                    )
                    _require_current_face_check(tracker_data)
                    active_trackers.append(tracker_data)
            else:
                for t_data in active_trackers:
                    t_data["loc"] = _update_tracker_location(t_data, frame, session_context=session_context)
                    if t_data["loc"] is None:
                        detection_needed = True

            liveness_time_ms = 0.0
            embedding_time_ms = 0.0
            matching_time_ms = 0.0
            last_match_info = None

            if is_detection_pass:
                for number, tracker_data in enumerate(active_trackers, 1):
                    tracker_data["face_number"] = number
                    tracker_data["face_count"] = len(active_trackers)

            frame_match_checks = []
            for t_data in active_trackers:
                _expire_tracked_identity_if_due(t_data, frame_parameters, count_inference_cycle=False)
                loc = t_data["loc"]
                if not loc:
                    t_data["liveness"].reset()
                    continue

                if not t_data["matched"]:
                    previous_loc = t_data.get("prev_loc")
                    if previous_loc:
                        p_top, p_right, p_bottom, p_left = previous_loc
                        p_w = p_right - p_left
                        p_h = p_bottom - p_top
                        top, right, bottom, left = loc
                        w_b = right - left
                        h_b = bottom - top

                        cx, cy = left + w_b / 2.0, top + h_b / 2.0
                        p_cx, p_cy = p_left + p_w / 2.0, p_top + p_h / 2.0

                        dist = ((cx - p_cx) ** 2 + (cy - p_cy) ** 2) ** 0.5
                        size_diff = abs(w_b * h_b - p_w * p_h) / max(p_w * p_h, 1)

                        if size_diff > 0.5 or dist > max(w_b, h_b) * 0.5:
                            t_data["liveness"].reset()

                    t_data["prev_loc"] = loc

                    stage_started = time.perf_counter()
                    is_live = t_data["liveness"].update(rgb_frame, loc, session_context=frame_parameters, require_liveness=require_liveness)
                    liv_elapsed = time.perf_counter() - stage_started
                    liveness_time_ms += liv_elapsed * 1000.0
                    _observe_latency(session_context, "liveness", liv_elapsed)

                    if is_live and is_detection_pass:
                        try:
                            stage_started = time.perf_counter()
                            encodings = (encoder.encode(rgb_frame, [loc]) if encoder is not None
                                         else face_recognition.face_encodings(rgb_frame, [loc]))
                            emb_elapsed = time.perf_counter() - stage_started
                            embedding_time_ms += emb_elapsed * 1000.0
                            _observe_latency(session_context, "encode_face", emb_elapsed)

                            if encodings:
                                live_encoding = encodings[0]
                                if len(known_embeddings) > 0:
                                    stage_started = time.perf_counter()
                                    match_info = _evaluate_match(live_encoding, known_embeddings, session_context=frame_parameters, known_students=known_students)
                                    match_elapsed = time.perf_counter() - stage_started
                                    matching_time_ms += match_elapsed * 1000.0
                                    _observe_latency(session_context, "match", match_elapsed)
                                    student = None
                                    if match_info["accepted"]:
                                        student = known_students[match_info["best_index"]]
                                        _apply_match_confirmation(
                                            t_data, student, match_info, session_context=frame_parameters,
                                            verified_at=captured_frame.captured_at_monotonic,
                                        )
                                    else:
                                        t_data["name"] = "Uncertain" if match_info["reason"] == "ambiguous_second_best" else "Unknown"
                                        t_data["matched"] = False
                                        _reset_confirmation_state(t_data)
                                    frame_match_checks.append((t_data, student, match_info))
                                else:
                                    t_data["name"] = "Unknown"
                                    t_data["matched"] = False
                                    _reset_confirmation_state(t_data)
                            else:
                                t_data["name"] = "Extraction Failed"
                                t_data["matched"] = False
                                _reset_confirmation_state(t_data)
                        except FaceEncodingUnavailable:
                            raise
                        except Exception as e:
                            logger.error("Error extracting face encodings in Sandbox mode: %s", e)
                            t_data["name"] = "Error"
                            t_data["matched"] = False
                            _reset_confirmation_state(t_data)

            _reject_conflicting_face_matches(frame_match_checks)

            # The last face can take long enough to age the first result.
            # Recheck every label immediately before composing the publication.
            for t_data in active_trackers:
                if not t_data["loc"]:
                    continue
                _expire_tracked_identity_if_due(t_data, frame_parameters, count_inference_cycle=False)
                _draw_tracker_result(frame, t_data, is_detection_pass)
            for t_data, student, match_info in frame_match_checks:
                last_match_info = _record_sandbox_match(
                    session_context, t_data, student, match_info, captured_frame,
                    processed_width, frame_parameters,
                )

            stage_started = time.perf_counter()
            ret, jpeg_buffer = cv2.imencode(
                '.jpg',
                frame,
                _mjpeg_encode_parameters(frame_parameters),
            )
            _observe_latency(session_context, "jpeg_encode", time.perf_counter() - stage_started)

            if ret and shm is not None:
                frame_bytes = jpeg_buffer.tobytes()
                _publish_latest_jpeg(
                    session_context,
                    shm,
                    lock,
                    frame_bytes,
                    captured_frame.sequence,
                    captured_frame.captured_at_monotonic,
                )

            frame_counter += 1
            frames_this_second += 1

            loop_elapsed = time.perf_counter() - loop_started
            total_time_ms = loop_elapsed * 1000.0
            pipeline_latency = _observe_latency(session_context, "loop", loop_elapsed)

            now_time = time.time()
            if now_time - fps_start_time >= 1.0:
                current_fps = round(frames_this_second / (now_time - fps_start_time), 1)
                session_context["_pipeline_fps"] = current_fps
                _context_update(
                    session_context,
                    {"fps": current_fps, "publication_fps": current_fps},
                )
                frames_this_second = 0
                fps_start_time = now_time

            pipeline_latency["fps"] = current_fps
            pipeline_latency["rolling_liveness_ms"] = pipeline_latency.get("liveness_ms")
            pipeline_latency["detection_ms"] = round(detect_time_ms, 2)
            pipeline_latency["liveness_ms"] = round(liveness_time_ms, 2)
            pipeline_latency["embedding_ms"] = round(embedding_time_ms, 2)
            pipeline_latency["matching_ms"] = round(matching_time_ms, 2)
            pipeline_latency["total_ms"] = round(total_time_ms, 2)
            stats = {
                "detection_ms": round(detect_time_ms, 2),
                "liveness_ms": round(liveness_time_ms, 2),
                "embedding_ms": round(embedding_time_ms, 2),
                "matching_ms": round(matching_time_ms, 2),
                "total_ms": round(total_time_ms, 2),
                "fps": current_fps,
                "capture_fps": _context_get(session_context, "capture_fps", 0.0),
                "publication_fps": _context_get(session_context, "publication_fps", 0.0),
                "capture_sequence": _context_get(session_context, "capture_sequence", 0),
                "processed_sequence": captured_frame.sequence,
                "publication_sequence": _context_get(session_context, "publication_sequence", 0),
                "skipped_frames": skipped_frames,
                "current_frame_age_ms": _context_get(session_context, "current_frame_age_ms", 0.0),
                "read_failures": _context_get(session_context, "read_failures", 0),
                "capture_state": _context_get(session_context, "capture_state", "unknown"),
                "open_failures": _context_get(session_context, "open_failures", 0),
                "reconnect_attempts": _context_get(session_context, "reconnect_attempts", 0),
                "consecutive_failures": _context_get(session_context, "consecutive_failures", 0),
                "last_failure": _context_get(session_context, "last_failure"),
                "capture_stage": _context_get(session_context, "capture_stage", "unknown"),
                "capture_terminal_reason": _context_get(session_context, "capture_terminal_reason"),
                "best_distance": last_match_info.get("best_distance") if last_match_info else None,
                "second_distance": last_match_info.get("second_distance") if last_match_info else None,
                "margin": last_match_info.get("margin") if last_match_info else None,
                "ratio": last_match_info.get("ratio") if last_match_info else None,
                "match_info": last_match_info,
            }
            _context_update(
                session_context,
                {"pipeline_latency": pipeline_latency, "stats": stats},
            )

            next_freshness_log_at = _maybe_log_camera_freshness(
                session_context,
                "sandbox",
                next_freshness_log_at,
            )

            if stop_event and stop_event.is_set():
                break
            time.sleep(0.005)

    except Exception as e:
        logger.exception("Unexpected error in sandbox session loop: %s", e)
        _mark_session_stopped(
            session_context,
            "unexpected_error",
            f"Sandbox session stopped due to error: {e}",
        )
    finally:
        preview_stopped = preview is None or preview.stop()
        if encoder is not None:
            encoder.close()
        if capture is not None:
            capture.stop()
        if shm is not None and preview_stopped:
            try:
                shm.close()
            except Exception:
                pass
        if conn is not None:
            conn.close()
        logger.info("Sandbox Session Loop for camera_id=%s terminated gracefully.", camera_id)
