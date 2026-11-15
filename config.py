"""
Centralized Configuration
-------------------------
All tunable system parameters are defined in this module.
Pipeline and UI code must import from here exclusively.
"""

from dotenv import load_dotenv
import os

load_dotenv()


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int_env(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(BASE_DIR, "database", "attendance.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "database", "schema.sql")
ENV_PATH = os.path.join(BASE_DIR, ".env")
LOG_FILE = os.path.join(BASE_DIR, "system.log")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
AUDIT_FALLBACK_LOG = os.path.join(BASE_DIR, "audit_fallback.log")

# Biometric key for database-level encryption
BIOMETRIC_KEY = os.environ.get("BIOMETRIC_KEY")


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5000"))
FLASK_HOST = os.environ.get("FLASK_HOST", "127.0.0.1")
FLASK_DEBUG = _env_bool("FLASK_DEBUG", False)
INSTITUTION_UTC_OFFSET_MINUTES = _bounded_int_env(
    "INSTITUTION_UTC_OFFSET_MINUTES", 480, -720, 840
)

# ---------------------------------------------------------------------------
# AI Pipeline -- Frame Processing
# ---------------------------------------------------------------------------
FRAME_SKIP_INTERVAL = 3  # Run heavy inference on every Nth frame
DOWNSCALE_HEIGHT = 720  # Target height in pixels for frame downscaling
MJPEG_JPEG_QUALITY = _bounded_int_env(
    "MJPEG_JPEG_QUALITY", 85, 60, 95
)  # Explicit LAN-stream quality; lower than OpenCV's bandwidth-heavy default.
# Run CPU-heavy face detection on a smaller copy, then map the boxes back to
# the recognition/output frame (up to 720p). This preserves native detail while
# avoiding HOG detection over pixels that do not improve the final embedding.
FACE_DETECTION_HEIGHT = 480

# ---------------------------------------------------------------------------
# AI Pipeline -- Face Matching
# ---------------------------------------------------------------------------
MATCH_THRESHOLD = 0.5  # Euclidean distance ceiling for a positive match
MATCH_MARGIN = 0.2  # Best match must beat second-best by this distance
MATCH_RATIO = 0.85                # Or best/second-best distance ratio must be at most this
MATCH_CONFIRMATION_PASSES = 2  # Stable detection passes required before attendance write
MATCH_REVALIDATION_CYCLES = _bounded_int_env(
    "MATCH_REVALIDATION_CYCLES", 15, 5, 120
)  # Detection passes before a geometry-carried identity must be encoded again.
MATCH_REVALIDATION_SECONDS = _bounded_int_env(
    "MATCH_REVALIDATION_SECONDS", 5, 1, 60
)  # Maximum age of biometric evidence, even when inference slows or skips frames.
FACE_DETECTION_MODEL = "hog"     # HOG for CPU; switch to "cnn" if GPU is available

# ---------------------------------------------------------------------------
# AI Pipeline -- Enrollment Image Quality
# ---------------------------------------------------------------------------
ENROLLMENT_NUM_JITTERS = 10              # Re-sampling passes during face encoding for enrollment
ENROLLMENT_MIN_FACE_AREA_RATIO = 0.03    # Face box must cover at least 3% of image area
ENROLLMENT_MIN_EDGE_MARGIN_RATIO = 0.02  # Face box must not touch the image edge
ENROLLMENT_MIN_BRIGHTNESS = 45.0         # Mean grayscale brightness floor
ENROLLMENT_MAX_BRIGHTNESS = 220.0        # Mean grayscale brightness ceiling (overexposed above this)
ENROLLMENT_MIN_BLUR_VARIANCE = 40.0      # Laplacian variance blur floor
ENROLLMENT_NUM_JITTERS = 10              # Number of re-sampling jitters during enrollment face encoding

# ---------------------------------------------------------------------------
# Attendance Logic & Session Control
# ---------------------------------------------------------------------------
LATE_THRESHOLD_MINUTES = 15       # Minutes after class start_time before status = 'Late'
SESSION_DURATION_MINUTES = 60     # Maximum real-time duration a session loop can run before auto-terminating
SESSION_RESUME_GRACE_MINUTES = _bounded_int_env(
    "SESSION_RESUME_GRACE_MINUTES", 60, 0, 240
)
HEARTBEAT_TIMEOUT_SECONDS = 30    # Maximum seconds without a heartbeat before auto-terminating
SESSION_WORKER_MODE = os.environ.get("SESSION_WORKER_MODE", "process").strip().lower()
if SESSION_WORKER_MODE not in {"process", "thread"}:
    raise RuntimeError("SESSION_WORKER_MODE must be 'process' or 'thread'.")

# ---------------------------------------------------------------------------
# Backup and purge archive encryption
# ---------------------------------------------------------------------------
BACKUP_ARCHIVE_KEY = os.environ.get("BACKUP_ARCHIVE_KEY")
PURGE_ARCHIVE_KEY = os.environ.get("PURGE_ARCHIVE_KEY")
ENABLE_MAINTENANCE_PURGE = _env_bool("ENABLE_MAINTENANCE_PURGE", False)
# PRAGMA secure_delete=ON ensures deleted rows are overwritten with zeros
# before VACUUM reclaims disk pages, preventing forensic recovery.
PURGE_ENABLE_SECURE_DELETE = True

# ---------------------------------------------------------------------------
# Audio Feedback
# ---------------------------------------------------------------------------
AUDIO_CHIME_DURATION = 0.5        # Maximum duration of a single success chime (seconds)
AUDIO_CHIME_PATH = os.path.join(BASE_DIR, "static", "audio", "success.wav")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_MAX_BYTES = 5 * 1024 * 1024   # 5 MB per log file before rotation
LOG_BACKUP_COUNT = 3              # Number of rotated log backups to retain
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# Liveness Detection
# ---------------------------------------------------------------------------
REQUIRE_LIVENESS = _env_bool("REQUIRE_LIVENESS", False)
LIVENESS_TTL_SECONDS = 10.0
EAR_BLINK_THRESHOLD = 0.21       # Eye Aspect Ratio below which a blink is registered
EAR_CONSEC_FRAMES = 1            # Consecutive frames below threshold to confirm a blink
