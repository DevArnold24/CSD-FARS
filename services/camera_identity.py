"""Exact local-device identity; never probe or merge network endpoints."""

from contextlib import contextmanager
import sqlite3

from database.db_manager import managed_transaction
from services.camera_security import ENCRYPTED_PREFIX, decrypt_camera_source


class CameraIdentityUnavailable(RuntimeError):
    """A saved local connection cannot be compared safely."""


def local_device_identity(camera_type, source):
    if camera_type != "local":
        return None
    try:
        raw = str("" if source is None else source).strip()
        # Read a legacy numeric identity only to prevent parallel ownership.
        # Capture still requires encryption, and every configuration write stays
        # encrypted. This comparison never turns plaintext into a usable source.
        value = decrypt_camera_source(camera_type, raw).strip() if raw.startswith(ENCRYPTED_PREFIX) else raw
        if not value.isascii() or not value.isdigit():
            raise ValueError("invalid local index")
        return ("local", int(value))
    except Exception:
        # Source/decryption errors can contain credentials; never forward text.
        raise CameraIdentityUnavailable(
            "A saved local camera connection is unreadable. Repair that connection before adding or opening another local camera."
        ) from None


@contextmanager
def _connection(conn):
    if conn is not None:
        yield conn
    else:
        with managed_transaction(commit=False) as opened:
            yield opened


def camera_identity(camera_id, conn=None):
    """Use an exact encrypted local index, otherwise the distinct record ID."""
    if camera_id is None:
        return None
    try:
        record_id = int(camera_id)
    except (TypeError, ValueError):
        record_id = str(camera_id)
    with _connection(conn) as opened:
        try:
            row = opened.execute(
                "SELECT camera_type, source FROM camera_devices WHERE id=?", (camera_id,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: camera_devices" not in str(exc):
                raise
            # Observation still reports exact registered owners in a partial
            # schema. Camera lookup/capacity guards refuse fresh capture there.
            row = None
    if row is not None:
        local = local_device_identity(row["camera_type"], row["source"])
        if local is not None:
            return local
    return ("record", record_id)


def equivalent_camera_ids(camera_id, identity, conn=None):
    """Include pre-existing duplicate records in durable ownership checks."""
    if not identity or identity[0] != "local":
        return [camera_id]
    with _connection(conn) as opened:
        rows = opened.execute(
            "SELECT id,camera_type,source FROM camera_devices WHERE camera_type='local'"
        ).fetchall()
    return [row["id"] for row in rows
            if local_device_identity(row["camera_type"], row["source"]) == identity]


def require_unique_local_source(conn, camera_type, source, *, exclude_id=None, lifecycle_status="Ready"):
    # Retirement retains history while relinquishing the configured device
    # slot. Runtime/durable reservations deliberately still scan every record.
    if lifecycle_status == "Retired":
        return
    identity = local_device_identity(camera_type, source)
    if identity is None:
        return
    for row in conn.execute(
        "SELECT id,camera_type,source FROM camera_devices WHERE camera_type='local' "
        "AND (lifecycle_status<>'Retired' OR is_active<>0)"
    ).fetchall():
        if row["id"] == exclude_id:
            continue
        if local_device_identity(row["camera_type"], row["source"]) == identity:
            raise CameraIdentityUnavailable(
                "This local camera index is already configured. Use the existing camera or choose a different local index."
            )
