"""Camera source encryption helpers."""

import os

import config


ENCRYPTED_PREFIX = "enc:v1:"
CAMERA_SOURCE_KEY_ENV = "CAMERA_SOURCE_KEY"


class CameraSourceEncryptionError(RuntimeError):
    """Raised when camera source encryption cannot be performed safely."""


def _load_env_if_needed():
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(config.ENV_PATH)


def _get_fernet():
    _load_env_if_needed()
    key = os.environ.get(CAMERA_SOURCE_KEY_ENV, "").strip()
    if not key:
        raise CameraSourceEncryptionError(
            "CAMERA_SOURCE_KEY is required to store or read encrypted RTSP camera sources."
        )
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise CameraSourceEncryptionError(
            "cryptography is required for RTSP camera source encryption. Install project requirements."
        ) from exc
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:
        raise CameraSourceEncryptionError("CAMERA_SOURCE_KEY is invalid.") from exc


def should_encrypt_camera_source(camera_type=None):
    """Return whether this camera type requires encrypted at-rest storage."""
    return True


def encrypt_camera_source(camera_type, source):
    """Encrypt a camera source when policy requires it."""
    source = str(source or "").strip()
    if source.startswith(ENCRYPTED_PREFIX):
        return source
    token = _get_fernet().encrypt(source.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTED_PREFIX}{token}"


def decrypt_camera_source(camera_type, source):
    """Return the usable camera source for OpenCV."""
    source = str(source or "").strip()
    if not source.startswith(ENCRYPTED_PREFIX):
        raise CameraSourceEncryptionError("Missing encryption prefix on RTSP source.")
    token = source[len(ENCRYPTED_PREFIX):]
    return _get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")


def mask_camera_source(camera_type, source):
    """Return a display-safe camera source string."""
    return "rtsp://***"
