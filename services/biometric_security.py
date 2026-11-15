"""
Biometric Encryption Service
----------------------------
Handles encryption, decryption, and detection of encrypted face vector blobs.
"""

from cryptography.fernet import Fernet
import config

class BiometricSecurityError(Exception):
    """Custom exception for biometric security errors."""
    pass

_fernet_instance = None

def _get_fernet():
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    
    key = getattr(config, "BIOMETRIC_KEY", None)
    if not key:
        raise BiometricSecurityError("Biometric encryption key (BIOMETRIC_KEY) is not set in configuration.")
    
    try:
        if isinstance(key, str):
            key_bytes = key.encode('utf-8')
        else:
            key_bytes = key
        _fernet_instance = Fernet(key_bytes)
        return _fernet_instance
    except Exception as e:
        raise BiometricSecurityError(f"Failed to initialize Fernet with the provided key: {e}") from e

def encrypt_face_vector(raw_blob: bytes) -> bytes:
    """Encrypts raw vector bytes using cryptography.fernet.Fernet. Returns the encrypted token bytes."""
    if not isinstance(raw_blob, bytes):
        raise BiometricSecurityError("Input raw_blob must be bytes.")
    try:
        fernet = _get_fernet()
        return fernet.encrypt(raw_blob)
    except BiometricSecurityError:
        raise
    except Exception as e:
        raise BiometricSecurityError(f"Encryption failed: {e}") from e

def decrypt_face_vector(encrypted_blob: bytes) -> bytes:
    """Decrypts cryptography.fernet.Fernet encrypted bytes. Returns the original raw bytes."""
    if not isinstance(encrypted_blob, bytes):
        raise BiometricSecurityError("Input encrypted_blob must be bytes.")
    try:
        fernet = _get_fernet()
        return fernet.decrypt(encrypted_blob)
    except BiometricSecurityError:
        raise
    except Exception as e:
        raise BiometricSecurityError(f"Decryption failed: {e}") from e

def is_encrypted(blob: bytes) -> bool:
    """Returns True if the blob starts with b'gAAAAA' (Fernet token prefix), False otherwise."""
    if not isinstance(blob, bytes):
        return False
    return blob.startswith(b"gAAAAA")
