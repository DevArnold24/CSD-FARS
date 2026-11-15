"""Encrypted SQLite backup and staging-only restore helpers."""

import datetime
from contextlib import contextmanager, suppress
import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import zipfile
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

import config
from services.private_storage import (
    assert_private_output, ensure_private_directory, private_temporary_directory,
    use_memory_sqlite_temp, normal_path,
)


BACKUP_DIR = os.path.join(config.BASE_DIR, "backups")


def ensure_backup_dir():
    ensure_private_directory(BACKUP_DIR)
    return BACKUP_DIR


def timestamp_slug():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_cipher():
    key = getattr(config, "BACKUP_ARCHIVE_KEY", None)
    if not key:
        raise RuntimeError("BACKUP_ARCHIVE_KEY is not configured.")
    biometric_key = getattr(config, "BIOMETRIC_KEY", None)
    camera_key = os.environ.get("CAMERA_SOURCE_KEY")
    if key in {biometric_key, camera_key}:
        raise RuntimeError("BACKUP_ARCHIVE_KEY must be independent from biometric and camera keys.")
    return Fernet(key.encode() if isinstance(key, str) else key)


def create_sqlite_snapshot(output_path, source_path=None):
    """Create and integrity-check a SQLite snapshot at an explicit path."""
    output_path = assert_private_output(output_path)
    source_path = source_path or config.DATABASE_PATH
    # A missing source must fail, not silently become a newly created empty DB.
    source_uri = Path(source_path).resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(source_uri, timeout=5.0, uri=True)
    try:
        use_memory_sqlite_temp(source)
        # Reserve the new path atomically. Two callers selecting the same output
        # must never both open it for SQLite backup, even in a private directory.
        descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        target = sqlite3.connect(output_path, timeout=5.0)
        try:
            use_memory_sqlite_temp(target)
            source.backup(target)
            row = target.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                raise RuntimeError("SQLite snapshot failed integrity check.")
        finally:
            target.close()
    finally:
        source.close()
    return output_path


def _build_plain_zip(snapshot_path, database_name):
    with open(snapshot_path, "rb") as snapshot:
        snapshot_bytes = snapshot.read()
    manifest = {
        "format": "csd-attendance-backup-v2",
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "database_file": database_name,
        "database_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(database_name, snapshot_bytes)
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True))
    return buffer.getvalue()


@contextmanager
def _staged_bytes(payload, directory):
    """Own one exclusive staging file on the publication filesystem."""
    ensure_private_directory(directory)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".csd-recovery-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        yield temporary
    finally:
        # Cleanup must never remove another caller's shared/guessed filename,
        # mask a publication error, or undo a successfully published archive.
        with suppress(OSError):
            os.unlink(temporary)


def _publish_new_file(temporary, destination):
    """Atomically publish complete bytes without replacing any existing path.

    Both paths share a filesystem. NTFS (the supported Windows installation
    filesystem) and ordinary local Linux filesystems support hard links. Fail
    closed elsewhere instead of falling back to a replacing rename or exposing
    a partially written destination.
    """
    os.link(temporary, destination)


def create_sqlite_backup(label="manual"):
    """Create an encrypted database-only backup and return its final path."""
    cipher = _backup_cipher()
    ensure_backup_dir()
    safe_label = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in label)
    slug = timestamp_slug()
    with private_temporary_directory(BACKUP_DIR) as temp_dir:
        snapshot_path = os.path.join(temp_dir, f"attendance_{slug}.db")
        create_sqlite_snapshot(snapshot_path)
        encrypted = cipher.encrypt(
            _build_plain_zip(snapshot_path, os.path.basename(snapshot_path))
        )
        with _staged_bytes(encrypted, BACKUP_DIR) as temporary:
            for _ in range(10):
                final_path = os.path.join(
                    BACKUP_DIR, f"backup_{safe_label}_{slug}_{uuid4().hex}.enc"
                )
                try:
                    _publish_new_file(temporary, final_path)
                except FileExistsError:
                    continue
                return final_path
            raise FileExistsError("Could not allocate a unique backup filename.")


def _validate_member(info):
    normalized = info.filename.replace("\\", "/")
    if (
        normalized.startswith("/")
        or ".." in normalized.split("/")
        or ":" in normalized.split("/", 1)[0]
    ):
        raise ValueError("Backup contains an unsafe path.")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ValueError("Backup contains a symbolic link.")
    return normalized


def _extract_database_bytes(zip_bytes, allow_legacy_zip=False):
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as archive:
        members = archive.infolist()
        names = [_validate_member(info) for info in members]
        if len(names) != len(set(names)):
            raise ValueError("Backup contains duplicate archive members.")
        by_name = dict(zip(names, members))
        db_names = [name for name in names if name.lower().endswith(".db")]
        if len(db_names) != 1:
            raise ValueError("Backup must contain exactly one SQLite database.")
        database_bytes = archive.read(by_name[db_names[0]])
        if "manifest.json" in by_name:
            try:
                manifest = json.loads(archive.read(by_name["manifest.json"]))
            except (ValueError, UnicodeError) as exc:
                raise ValueError("Backup manifest is invalid.") from exc
            if (
                not isinstance(manifest, dict)
                or manifest.get("format") != "csd-attendance-backup-v2"
                or manifest.get("database_file") != db_names[0]
                or manifest.get("database_sha256")
                != hashlib.sha256(database_bytes).hexdigest()
            ):
                raise ValueError("Backup manifest does not match the database.")
        elif not allow_legacy_zip:
            raise ValueError("Backup manifest is missing.")
        return database_bytes


def _validated_database_bytes(archive_path, allow_legacy_zip=False):
    with open(archive_path, "rb") as source:
        raw = source.read()
    if zipfile.is_zipfile(io.BytesIO(raw)):
        if not allow_legacy_zip:
            raise ValueError("Legacy ZIP restore requires --allow-legacy-zip.")
        zip_bytes = raw
    else:
        try:
            zip_bytes = _backup_cipher().decrypt(raw)
        except InvalidToken as exc:
            raise ValueError("Backup authentication or decryption failed.") from exc
    database_bytes = _extract_database_bytes(zip_bytes, allow_legacy_zip=allow_legacy_zip)
    if not database_bytes.startswith(b"SQLite format 3\x00"):
        raise ValueError("Backup database is not a SQLite database.")
    with private_temporary_directory(BACKUP_DIR) as temp_dir:
        with _staged_bytes(database_bytes, temp_dir) as temp_path:
            conn = sqlite3.connect(temp_path, timeout=5.0)
            try:
                use_memory_sqlite_temp(conn)
                row = conn.execute("PRAGMA integrity_check").fetchone()
            finally:
                conn.close()
            if not row or row[0] != "ok":
                raise ValueError("Restored database failed integrity check.")
            return database_bytes


def verify_sqlite_backup(backup_path):
    try:
        return bool(_validated_database_bytes(backup_path))
    except (OSError, ValueError, RuntimeError, sqlite3.Error, zipfile.BadZipFile):
        return False


def restore_sqlite_backup(archive_path, output_path, allow_legacy_zip=False):
    """Restore to a new staging path; never replace the active database."""
    archive_path = os.path.abspath(archive_path)
    # Check original spelling before Windows normalizes trailing dots/spaces.
    output_path = os.fspath(normal_path(output_path))
    if os.path.normcase(os.path.realpath(output_path)) == os.path.normcase(
        os.path.realpath(config.DATABASE_PATH)
    ):
        raise ValueError("Refusing to overwrite the active database.")
    if os.path.lexists(output_path):
        raise FileExistsError("Restore output already exists.")
    assert_private_output(output_path)
    database_bytes = _validated_database_bytes(
        archive_path, allow_legacy_zip=allow_legacy_zip
    )
    with private_temporary_directory(os.path.dirname(output_path)) as temp_dir:
        with _staged_bytes(database_bytes, temp_dir) as temporary:
            _publish_new_file(temporary, output_path)
    return output_path
