"""Safe first-run initialization and readiness checks for a client laptop.

This module deliberately does not import the application at module load time.
During initialization it loads the application modules from ``--root`` with
``config.DATABASE_PATH`` redirected to a staging database.  The real database
and ``.env`` are published only after the staged installation passes all
checks.
"""

from __future__ import annotations

import argparse
import ast
import base64
import binascii
import builtins
import getpass
import hashlib
import hmac
import importlib
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Mapping
from uuid import uuid4


FORMAT_VERSION = 1
EXPECTED_MIGRATIONS = tuple(f"{number:03d}" for number in range(1, 34))
REQUIRED_KEYS = (
    "SECRET_KEY",
    "CAMERA_SOURCE_KEY",
    "BIOMETRIC_KEY",
    "BACKUP_ARCHIVE_KEY",
    "PURGE_ARCHIVE_KEY",
)
FERNET_KEYS = REQUIRED_KEYS[1:]
SAFE_RUNTIME_VALUES = {
    "SESSION_WORKER_MODE": "process",
    "FLASK_HOST": "127.0.0.1",
    "FLASK_DEBUG": "false",
}
MARKER_NAME = ".client-state"
RUN_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
VALID_MARKER_STATUSES = {
    "preparing",
    "prepared",
    "database_published",
    "complete",
}
DATABASE_BINDING_ACTION = "client_install_bound"
DATABASE_BINDING_FORMAT = 1

# These are the logical objects required by application code after migrations
# 001-033.  Migration labels alone are not proof that the corresponding schema
# still exists, so the read-only client check validates the usable shape too.
REQUIRED_TABLE_COLUMNS = {
    "attendance_early_resumes": ("id", "session_id", "actor", "reason", "accepted_at", "deadline", "original_cutoff", "original_end_operation", "original_receipt"),
    "attendance_reopening": ("session_id", "actor", "reason", "accepted_at", "deadline", "original_cutoff", "original_end_operation", "original_receipt"),
    "login_notice_acknowledgments": ("user_id", "notice_version", "acknowledged_at", "remember"),
    "attendance_opening_settings": ("id", "minutes", "version", "updated_at"),
    "attendance_opening_history": ("id", "operation_id", "actor", "previous_minutes", "minutes", "previous_version", "version", "created_at"),
    "attendance_opening_snapshots": ("session_id", "opens_at", "minutes", "policy_version"),
    "camera_capacity_limits": ("id", "recognition", "viewers", "secondary", "updated_by_user_id", "updated_at"),
    "attendance_start_attempts": ("id", "token", "session_id", "mode", "phase", "camera_id", "accepted_at", "deadline_mono", "server_boot", "ready_at", "failure", "dispatch", "config_digest", "coverage_digest", "camera_digest", "overrides", "dispatch_owner"),
    "attendance_start_operations": ("operation_id", "class_id", "actor", "session_id", "attempt_id", "intent", "accepted_at"),
    "attendance_lifecycle": (
        "session_id", "policy_version", "cutoff", "late_at", "roster_signature",
        "collection", "version", "end_operation", "end_actor", "end_reason", "ended_at",
        "closed_at", "cleanup", "episode", "episode_started", "attempts", "next_attempt",
        "exhausted", "in_flight", "last_error", "interruption_reason",
    ),
    "attendance_lifecycle_events": ("id", "session_id", "event", "at", "actor", "detail"),
    "attendance_process_owners": ("token", "session_id", "kind", "pid", "created", "confirmed"),
    "attendance_server_owner": ("id", "pid", "created"),
    "academic_programs": ("id", "name", "is_active"),
    "attendance_change_audit": (
        "id", "attendance_session_id", "attendance_record_id", "class_id",
        "student_id", "actor_user_id", "previous_status", "new_status",
        "previous_reason", "new_reason", "change_reason", "changed_at", "operation_kind",
    ),
    "attendance_display_grants": (
        "id", "attendance_session_id", "grant_hash", "enabled_by_user_id",
        "enabled_at", "revoked_at", "revoked_by_user_id", "revoke_reason",
    ),
    "attendance_records": (
        "id", "attendance_session_id", "student_id", "class_id", "timestamp",
        "status", "source", "reason", "updated_by_user_id", "updated_at",
    ),
    "attendance_sessions": (
        "id", "class_id", "teacher_id", "camera_id", "scheduled_date",
        "scheduled_start_time", "scheduled_end_time", "occurrence_key",
        "roster_anchor_at", "roster_anchor_order", "actual_started_at",
        "actual_ended_at", "status", "close_reason", "created_at",
    ),
    "audit_logs": (
        "id", "attendance_record_id", "teacher_id", "previous_status",
        "new_status", "reason", "timestamp",
    ),
    "buildings": (
        "id", "name", "normalized_name", "is_active", "created_at", "updated_at",
    ),
    "camera_devices": (
        "id", "room_id", "name", "camera_type", "source", "placement_notes",
        "lifecycle_status", "resolution_width", "resolution_height", "target_fps",
        "is_default", "is_active", "last_test_status", "last_test_at",
        "last_successful_frame", "failure_count", "last_failure_reason",
        "last_health_check_at", "created_at", "updated_at", "match_threshold",
    ),
    "class_enrollment_periods": (
        "id", "class_id", "student_id", "enrolled_at", "unenrolled_at",
        "enrolled_order", "unenrolled_order",
    ),
    "class_enrollments": ("id", "class_id", "student_id", "enrolled_at"),
    "class_schedule_periods": (
        "id", "class_id", "effective_from", "effective_until", "days_of_week",
        "start_time", "end_time", "created_at",
    ),
    "classes": (
        "id", "teacher_id", "camera_id", "room_id", "course_name", "class_code",
        "section", "days_of_week", "start_time", "end_time",
        "late_threshold_minutes", "is_active", "is_archived", "effective_start_date",
    ),
    "embeddings": ("id", "student_id", "face_vector", "created_at"),
    "floors": (
        "id", "building_id", "name", "normalized_name", "sort_order", "is_active",
        "created_at", "updated_at",
    ),
    "lifecycle_event_sequence": ("singleton", "last_value"),
    "pending_student_registrations": (
        "id", "student_id", "name", "program_id", "year_level", "consent_granted",
        "face_vector", "submitted_by_user_id", "submitted_at", "status",
        "reviewed_by_user_id", "reviewed_at", "review_note",
    ),
    "recognition_settings": (
        "id", "match_threshold", "match_margin", "match_confirmation_passes",
        "frame_skip_interval", "downscale_height", "liveness_ttl_seconds",
        "require_liveness", "updated_by_user_id", "updated_at",
    ),
    "rooms": (
        "id", "floor_id", "room_code", "room_name", "building", "floor",
        "department", "room_type", "capacity", "is_active", "notes", "created_at",
        "updated_at",
    ),
    "schema_migrations": ("version", "name", "applied_at"),
    "students": (
        "id", "student_id", "name", "program_id", "year_level", "consent_granted",
        "is_active", "permanently_removed_at",
    ),
    "system_audit_logs": ("id", "actor_user_id", "action", "details", "created_at"),
    "users": (
        "id", "staff_id", "username", "password_hash", "role",
        "requires_password_change", "is_active", "failed_login_attempts",
        "locked_until", "tuner_preferences",
    ),
}

# name: (owning table, unique, partial, ordered key columns).  Expression
# columns are represented by None and additionally checked by SQL token below.
REQUIRED_INDEXES = {
    "idx_attendance_change_audit_session": (
        "attendance_change_audit", False, False, ("attendance_session_id", "changed_at")
    ),
    "idx_attendance_display_one_active_grant": (
        "attendance_display_grants", True, True, ("attendance_session_id",)
    ),
    "idx_attendance_session_student": (
        "attendance_records", False, False, ("attendance_session_id", "student_id")
    ),
    "idx_attendance_sessions_canonical_occurrence": (
        "attendance_sessions", True, True, ("class_id", "occurrence_key")
    ),
    "idx_attendance_sessions_class_date": (
        "attendance_sessions", False, False, ("class_id", "scheduled_date", "status")
    ),
    "idx_attendance_student_class": (
        "attendance_records", False, False, ("student_id", "class_id", "timestamp")
    ),
    "idx_camera_devices_one_active_per_room": (
        "camera_devices", True, True, ("room_id",)
    ),
    "idx_camera_devices_room_active": (
        "camera_devices", False, False, ("room_id", "is_active")
    ),
    "idx_camera_devices_single_default": (
        "camera_devices", True, True, ("is_default",)
    ),
    "idx_class_enrollment_period_active": (
        "class_enrollment_periods", True, True, ("class_id", "student_id")
    ),
    "idx_class_enrollment_period_occurrence": (
        "class_enrollment_periods", False, False,
        ("class_id", "enrolled_at", "unenrolled_at", "student_id"),
    ),
    "idx_class_schedule_period_resolution": (
        "class_schedule_periods", False, False,
        ("class_id", "effective_from", "effective_until"),
    ),
    "idx_classes_camera": ("classes", False, False, ("camera_id",)),
    "idx_classes_room": ("classes", False, False, ("room_id",)),
    "idx_classes_schedule_resources": (
        "classes", False, False,
        ("is_archived", "room_id", "teacher_id", "start_time", "end_time"),
    ),
    "idx_classes_teacher_code": (
        "classes", True, False, ("teacher_id", "class_code")
    ),
    "idx_classes_teacher_code_normalized": (
        "classes", True, False, ("teacher_id", None)
    ),
    "idx_embeddings_student": ("embeddings", False, False, ("student_id",)),
    "idx_floors_building_order": (
        "floors", False, False, ("building_id", "sort_order", "normalized_name")
    ),
    "idx_pending_student_status_submitted": (
        "pending_student_registrations", False, False, ("status", "submitted_at")
    ),
    "idx_pending_student_unique_pending": (
        "pending_student_registrations", True, True, ("student_id",)
    ),
    "idx_rooms_active_code": ("rooms", False, False, ("is_active", "room_code")),
    "idx_rooms_code_normalized": ("rooms", True, False, (None,)),
    "idx_rooms_floor_active": (
        "rooms", False, False, ("floor_id", "is_active", "room_code")
    ),
}
REQUIRED_INDEX_SQL_TOKENS = {
    "idx_classes_teacher_code_normalized": "lower(trim(class_code))",
    "idx_rooms_code_normalized": "lower(trim(room_code))",
}

# name: (owning table, semantic SQL tokens).  Tokens prevent a same-named
# no-op trigger from satisfying the logical schema check.
REQUIRED_TRIGGERS = {
    "trg_attendance_occurrence_date_insert": (
        "attendance_sessions", ("beforeinsertonattendance_sessions", "attendanceoccurrencealreadyexistsforclassdate")
    ),
    "trg_attendance_occurrence_date_update": (
        "attendance_sessions", ("beforeupdateofclass_id,scheduled_date,occurrence_keyonattendance_sessions", "attendanceoccurrencealreadyexistsforclassdate")
    ),
    "trg_attendance_session_assign_order": (
        "attendance_sessions", ("afterinsertonattendance_sessions", "updatelifecycle_event_sequence")
    ),
    "trg_class_enrollment_period_delete": (
        "class_enrollments", ("beforedeleteonclass_enrollments", "updateclass_enrollment_periods")
    ),
    "trg_class_enrollment_period_insert": (
        "class_enrollments", ("afterinsertonclass_enrollments", "insertintoclass_enrollment_periods")
    ),
    "trg_class_schedule_period_insert": (
        "classes", ("afterinsertonclasses", "insertintoclass_schedule_periods")
    ),
    "trg_embeddings_require_eligible_student_insert": (
        "embeddings", ("beforeinsertonembeddings", "biometricenrollmentrequiresanactiveconsentingstudent")
    ),
    "trg_embeddings_require_eligible_student_update": (
        "embeddings", ("beforeupdateonembeddings", "biometricenrollmentrequiresanactiveconsentingstudent")
    ),
    "trg_rooms_floor_required_insert": (
        "rooms", ("beforeinsertonrooms", "new.floor_idisnull", "rooms.floor_idisrequired")
    ),
    "trg_rooms_floor_required_update": (
        "rooms", ("beforeupdateoffloor_idonrooms", "new.floor_idisnull", "rooms.floor_idisrequired")
    ),
    "trg_students_close_ineligible_enrollments": (
        "students", ("afterupdateofis_active,permanently_removed_atonstudents", "deletefromclass_enrollments")
    ),
    "trg_students_purge_ineligible_embeddings": (
        "students", ("afterupdateofis_active,consent_granted,permanently_removed_atonstudents", "deletefromembeddings")
    ),
    "trg_users_preserve_active_dean": (
        "users", ("beforeupdateofrole,is_activeonusers", "atleastoneactivedeanisrequired")
    ),
}


class SetupError(RuntimeError):
    """An expected, operator-actionable setup error."""


class SetupStorageError(SetupError):
    """Windows could not verify private live storage; do not write a receipt."""


class SetupInputRequired(SetupError):
    """Initialization needs operator input; it must not read a hidden console."""


class SetupInputError(SetupError):
    """Rejected input before any state publication; the same form may retry."""


@dataclass(frozen=True, repr=False)
class DeanCredentials:
    """Initial Dean credentials whose representation never reveals a password."""

    username: str
    password: str

    def __repr__(self) -> str:
        return f"DeanCredentials(username={self.username!r}, password=<redacted>)"


@dataclass(frozen=True)
class SetupResult:
    status: str
    message: str


@dataclass(frozen=True)
class InstallPaths:
    root: Path
    environment: Path
    database: Path
    marker: Path

    def staging_environment(self, run_id: str) -> Path:
        return self.root / f".env.client-{run_id}.stage"

    def staging_database(self, run_id: str) -> Path:
        return self.root / "database" / f".attendance.client-{run_id}.db"


def _path_present(path: Path) -> bool:
    """Return true for regular entries and dangling links alike."""

    return os.path.lexists(path)


def _require_python_311() -> None:
    if sys.version_info[:2] != (3, 11):
        raise SetupError(
            "Client setup requires 64-bit Python 3.11.x; no files were changed."
        )
    if sys.maxsize <= 2**32:
        raise SetupError(
            "Client setup requires 64-bit Python 3.11.x; no files were changed."
        )


def _migration_versions_from_source(path: Path) -> tuple[str, ...]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise SetupError("migrate.py could not be validated; no files were changed.") from exc

    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "MIGRATIONS" for target in targets):
            continue
        value = node.value
        if not isinstance(value, (ast.List, ast.Tuple)):
            break
        versions: list[str] = []
        for entry in value.elts:
            if not isinstance(entry, ast.Tuple) or not entry.elts:
                break
            first = entry.elts[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                break
            versions.append(first.value)
        else:
            return tuple(versions)
        break
    raise SetupError("The migration manifest is unreadable; no files were changed.")


def _assert_private_storage(root: os.PathLike[str] | str) -> None:
    """Native read-only Windows gate; never launches or reconfigures a shell."""
    if os.name != "nt":
        return
    try:
        if __package__:
            from . import windows_storage
        else:
            import windows_storage
        windows_storage.assert_private_installation(root)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        raise SetupStorageError(
            "Private installation permissions could not be verified. Keep existing files "
            "unchanged and ask your technician for an owner-private local destination. "
            "No permissions were changed."
        ) from exc


def _resolve_root(root: os.PathLike[str] | str) -> InstallPaths:
    candidate = Path(root).expanduser()
    try:
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SetupError("The supplied client root does not exist or is inaccessible.") from exc
    if not candidate.is_dir():
        raise SetupError("The supplied client root is not a directory.")
    if os.name == "nt" and str(candidate).startswith("\\\\"):
        raise SetupError("Client setup must run from a local drive, not a network path.")

    required_files = (
        candidate / "config.py",
        candidate / "migrate.py",
        candidate / "security" / "login_notice.py",
        candidate / "database" / "schema.sql",
        candidate / "database" / "db_manager.py",
        candidate / "core" / "__init__.py",
        candidate / "services" / "audit.py",
        candidate / "services" / "biometric_security.py",
        candidate / "services" / "session_lifecycle.py",
        candidate / "services" / "session_start.py",
        candidate / "services" / "camera_capacity.py",
        candidate / "services" / "attendance_settings.py",
    )
    if any(not path.is_file() for path in required_files):
        raise SetupError(
            "The selected folder is not a complete CSD Attendance client bundle."
        )
    versions = _migration_versions_from_source(candidate / "migrate.py")
    if versions != EXPECTED_MIGRATIONS:
        raise SetupError(
            "This client bundle does not contain the expected migrations 001 through 033."
        )
    return InstallPaths(
        root=candidate,
        environment=candidate / ".env",
        database=candidate / "database" / "attendance.db",
        marker=candidate / MARKER_NAME,
    )


def _password_policy(password: str) -> tuple[bool, str]:
    if not isinstance(password, str) or not password:
        return False, "Password cannot be empty."
    if len(password) < 8:
        return False, "Password must be at least 8 characters."
    if not any(character.isupper() for character in password):
        return False, "Password must contain at least one uppercase letter."
    if not any(character.islower() for character in password):
        return False, "Password must contain at least one lowercase letter."
    if not any(character.isdigit() for character in password):
        return False, "Password must contain at least one digit."
    if not any(not character.isalnum() for character in password):
        return False, "Password must contain at least one special character."
    return True, ""


def _passwords_match(password: str, confirmation: str) -> bool:
    """Compare console passwords in constant time without rejecting Unicode."""

    try:
        password_bytes = password.encode("utf-8")
        confirmation_bytes = confirmation.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SetupError(
            "The password contains an unsupported Unicode character; no files were changed."
        ) from exc
    return secrets.compare_digest(password_bytes, confirmation_bytes)


def _validate_dean_credentials(credentials: object) -> DeanCredentials:
    if not isinstance(credentials, DeanCredentials):
        raise SetupError("The Dean credential prompt returned invalid data; no files were changed.")
    username = credentials.username
    if (
        not isinstance(username, str)
        or username != username.strip()
        or not username
        or len(username) > 128
        or any(ord(character) < 32 for character in username)
    ):
        raise SetupError("The Dean username is invalid. No files were changed.")
    valid_password, reason = _password_policy(credentials.password)
    if not valid_password:
        raise SetupError(f"{reason} No files were changed.")
    return credentials


def prompt_dean_credentials() -> DeanCredentials:
    """Prompt and validate credentials without echoing or printing secrets."""

    print("Create the first active Dean account.")
    print("Use at least 8 characters, with an uppercase letter, a lowercase letter, a number and a special character.")
    username = builtins.input("Dean username: ").strip()
    if not username or len(username) > 128 or any(ord(character) < 32 for character in username):
        raise SetupError("The Dean username must be 1-128 printable characters.")
    password = getpass.getpass("Dean password: ")
    confirmation = getpass.getpass("Confirm Dean password: ")
    if not _passwords_match(password, confirmation):
        raise SetupError("The two Dean passwords did not match; no files were changed.")
    valid, reason = _password_policy(password)
    if not valid:
        raise SetupError(f"{reason} No files were changed.")
    return _validate_dean_credentials(
        DeanCredentials(username=username, password=password)
    )


def read_dean_credentials(stream: BinaryIO) -> DeanCredentials:
    """Read the setup window's bounded UTF-8 document from an anonymous pipe.

    No credential values are included in errors or returned on stdout. Validation
    happens before initialize creates a marker, keys or a database. The window's
    inline validation is only a convenience; this boundary validates again.
    """
    def unique_fields(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("Duplicate field")
            result[name] = value
        return result

    try:
        raw = stream.read(8193)
        if not isinstance(raw, bytes) or not raw or len(raw) > 8192:
            raise ValueError("Invalid size")
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields)
        if not isinstance(document, dict) or set(document) != {
            "username", "password", "confirmation"
        } or not all(isinstance(value, str) for value in document.values()):
            raise ValueError("Invalid fields")
    except (ValueError, TypeError, UnicodeError, OSError):
        raise SetupInputError(
            "The administrator details could not be read. Return to setup and try again."
        ) from None
    try:
        if not _passwords_match(document["password"], document["confirmation"]):
            raise SetupError("The two passwords do not match. Check them and try again.")
        return _validate_dean_credentials(DeanCredentials(
            username=document["username"], password=document["password"]
        ))
    except SetupError as exc:
        raise SetupInputError(str(exc)) from None


def _parse_env(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise SetupError("The local .env file is missing or is not a regular file.")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SetupError("The local .env file could not be read safely.") from exc
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in raw_line:
            raise SetupError(f"The local .env file has an invalid line at {line_number}.")
        name, value = raw_line.split("=", 1)
        name = name.strip()
        if not name or name in values:
            raise SetupError("The local .env file has an empty or duplicate setting name.")
        values[name] = value.strip()
    return values


def _decode_fernet_key(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError, binascii.Error) as exc:
        raise SetupError("A required local encryption key is invalid.") from exc


def _validate_env_values(values: Mapping[str, str]) -> None:
    missing = [name for name in REQUIRED_KEYS if not values.get(name)]
    if missing:
        raise SetupError("The local .env file is missing required security keys.")

    secret_key = values["SECRET_KEY"]
    if len(secret_key) < 32 or len(set(secret_key)) < 8:
        raise SetupError("The local session key is invalid or too weak.")
    decoded_keys = []
    for name in FERNET_KEYS:
        decoded = _decode_fernet_key(values[name])
        if len(decoded) != 32:
            raise SetupError("A required local encryption key has the wrong length.")
        decoded_keys.append(values[name])
    if len(set(decoded_keys)) != len(decoded_keys):
        raise SetupError("The local encryption keys must be independent.")
    if secret_key in set(decoded_keys):
        raise SetupError("The local session and encryption keys must be independent.")

    for name, expected in SAFE_RUNTIME_VALUES.items():
        if values.get(name) != expected:
            raise SetupError(
                "The local runtime settings must use process workers, loopback, and debug disabled."
            )


def _key_fingerprint(values: Mapping[str, str], run_id: str) -> str:
    """Bind the marker to one key set and install ID without binding its path.

    A complete marker, .env, and database can therefore be restored together
    to a new local folder.  No individual artifact can be adopted separately.
    """

    payload = {
        "format": FORMAT_VERSION,
        "run_id": run_id,
        "keys": {name: values[name] for name in REQUIRED_KEYS},
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _key_canaries(values: Mapping[str, str], run_id: str) -> dict[str, str]:
    """Return non-secret HMAC canaries for every required local key."""

    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise SetupError("The client installation identifier is invalid.")
    canaries: dict[str, str] = {}
    for name in REQUIRED_KEYS:
        if name in FERNET_KEYS:
            key_material = _decode_fernet_key(values[name])
        else:
            key_material = values[name].encode("utf-8")
        message = (
            f"CSD Attendance client key canary v{DATABASE_BINDING_FORMAT}\0"
            f"{run_id}\0{name}"
        ).encode("utf-8")
        canaries[name] = hmac.new(key_material, message, hashlib.sha256).hexdigest()
    return canaries


def _database_binding_payload(
    values: Mapping[str, str], run_id: str
) -> dict[str, object]:
    return {
        "format": DATABASE_BINDING_FORMAT,
        "install_id": run_id,
        "key_canaries": _key_canaries(values, run_id),
    }


def _validate_database_binding(
    connection: sqlite3.Connection,
    values: Mapping[str, str],
    run_id: str,
) -> None:
    rows = connection.execute(
        "SELECT details FROM system_audit_logs WHERE action = ? ORDER BY id",
        (DATABASE_BINDING_ACTION,),
    ).fetchall()
    binding_error = (
        "The attendance database does not belong to this client installation or key set."
    )
    if len(rows) != 1:
        raise SetupError(binding_error)
    try:
        payload = json.loads(rows[0][0])
    except (TypeError, json.JSONDecodeError) as exc:
        raise SetupError(binding_error) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "format", "install_id", "key_canaries"
    }:
        raise SetupError(binding_error)
    install_id = payload.get("install_id")
    if (
        payload.get("format") != DATABASE_BINDING_FORMAT
        or not isinstance(install_id, str)
        or not secrets.compare_digest(install_id, run_id)
    ):
        raise SetupError(binding_error)
    stored_canaries = payload.get("key_canaries")
    if not isinstance(stored_canaries, dict) or set(stored_canaries) != set(REQUIRED_KEYS):
        raise SetupError(binding_error)
    expected_canaries = _key_canaries(values, run_id)
    for name in REQUIRED_KEYS:
        stored = stored_canaries.get(name)
        if (
            not isinstance(stored, str)
            or not re.fullmatch(r"[0-9a-f]{64}", stored)
            or not secrets.compare_digest(stored, expected_canaries[name])
        ):
            raise SetupError(binding_error)


def _ensure_database_binding(
    connection: sqlite3.Connection,
    values: Mapping[str, str],
    run_id: str,
) -> None:
    rows = connection.execute(
        "SELECT id FROM system_audit_logs WHERE action = ? ORDER BY id",
        (DATABASE_BINDING_ACTION,),
    ).fetchall()
    if not rows:
        details = json.dumps(
            _database_binding_payload(values, run_id),
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "INSERT INTO system_audit_logs (actor_user_id, action, details) "
            "VALUES (NULL, ?, ?)",
            (DATABASE_BINDING_ACTION, details),
        )
    _validate_database_binding(connection, values, run_id)


def _new_env_values() -> dict[str, str]:
    fernet_values: list[str] = []
    while len(fernet_values) < len(FERNET_KEYS):
        value = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        if value not in fernet_values:
            fernet_values.append(value)
    values = {
        "SECRET_KEY": secrets.token_hex(32),
        **dict(zip(FERNET_KEYS, fernet_values, strict=True)),
        **SAFE_RUNTIME_VALUES,
        "FLASK_PORT": "5000",
        "INSTITUTION_UTC_OFFSET_MINUTES": "480",
        "ENTRANCE_DISPLAY_TOKEN": "",
        "ENTRANCE_DISPLAY_SESSION_HOURS": "12",
        "CCTV_HOST": "",
        "CCTV_PORT": "80",
        "CCTV_USERNAME": "",
        "CCTV_PASSWORD": "",
        "CCTV_SNAPSHOT_PATH": "/ISAPI/Streaming/channels/101/picture",
        "CCTV_SUBSTREAM_SNAPSHOT_PATH": "/ISAPI/Streaming/channels/102/picture",
        "CCTV_VIEWER_PORT": "3000",
        "CCTV_VIEWER_BIND_HOST": "127.0.0.1",
        "CCTV_VIEWER_USERNAME": "",
        "CCTV_VIEWER_PASSWORD": "",
        "CCTV_VIEWER_ALLOW_LAN": "false",
        "CCTV_VIEWER_BEHIND_HTTPS_PROXY": "false",
    }
    _validate_env_values(values)
    return values


def _render_env(values: Mapping[str, str]) -> str:
    ordered = (
        *REQUIRED_KEYS,
        "ENTRANCE_DISPLAY_TOKEN",
        "ENTRANCE_DISPLAY_SESSION_HOURS",
        "FLASK_DEBUG",
        "FLASK_HOST",
        "FLASK_PORT",
        "INSTITUTION_UTC_OFFSET_MINUTES",
        "SESSION_WORKER_MODE",
        "CCTV_HOST",
        "CCTV_PORT",
        "CCTV_USERNAME",
        "CCTV_PASSWORD",
        "CCTV_SNAPSHOT_PATH",
        "CCTV_SUBSTREAM_SNAPSHOT_PATH",
        "CCTV_VIEWER_PORT",
        "CCTV_VIEWER_BIND_HOST",
        "CCTV_VIEWER_USERNAME",
        "CCTV_VIEWER_PASSWORD",
        "CCTV_VIEWER_ALLOW_LAN",
        "CCTV_VIEWER_BEHIND_HTTPS_PROXY",
    )
    return "".join(f"{name}={values.get(name, '')}\n" for name in ordered)


def _write_new_file(path: Path, content: str) -> None:
    # Never expose a partially written marker or environment at its final name.
    # A killed writer may leave an unpublished temporary; it grants no ownership
    # and is never adopted. A subsequent invocation can safely publish anew.
    temporary = path.with_name(f".client-publish-{uuid4().hex}.tmp")
    created = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Atomic no-clobber publication. Windows explicitly flushes the move;
        # POSIX flushes the directory after linking the durable file.
        if os.name == "nt":
            import ctypes
            move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
            move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
            move.restype = ctypes.c_int
            if not move(str(temporary), str(path), 0x8):  # WRITE_THROUGH, no REPLACE_EXISTING
                error = ctypes.get_last_error()
                if error in (80, 183):
                    raise FileExistsError(error, "destination exists")
                raise OSError(error, "atomic setup publication failed")
        else:
            os.link(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except FileExistsError as exc:
        raise SetupError("A setup staging file already exists; nothing was overwritten.") from exc
    except OSError as exc:
        raise SetupError("A setup staging file could not be written safely.") from exc
    finally:
        # This invocation created this unpredictable, exclusive name. Never
        # remove a temporary from another run or infer ownership from content.
        try:
            if created:
                temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_marker(path: Path) -> dict[str, object] | None:
    if not _path_present(path):
        return None
    if not path.is_file() or path.is_symlink():
        raise SetupError("The client setup marker is not a regular file.")
    try:
        if path.stat().st_size > 16_384:
            raise SetupError("The client setup marker is invalid.")
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SetupError("The client setup marker is invalid.") from exc
    if not isinstance(marker, dict):
        raise SetupError("The client setup marker is invalid.")
    if marker.get("format") != FORMAT_VERSION:
        raise SetupError("The client setup marker uses an unsupported format.")
    run_id = marker.get("run_id")
    status = marker.get("status")
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise SetupError("The client setup marker has an invalid run identifier.")
    if status not in VALID_MARKER_STATUSES:
        raise SetupError("The client setup marker has an invalid status.")
    fingerprint = marker.get("key_fingerprint")
    if status == "complete":
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise SetupError("The completed client setup marker has no valid key fingerprint.")
    elif fingerprint is not None:
        raise SetupError("An incomplete client setup marker contains invalid completion data.")
    return marker


def _create_marker(path: Path, run_id: str) -> dict[str, object]:
    marker: dict[str, object] = {
        "format": FORMAT_VERSION,
        "run_id": run_id,
        "status": "preparing",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_new_file(path, json.dumps(marker, sort_keys=True, indent=2) + "\n")
    return marker


def _update_marker(
    path: Path,
    marker: dict[str, object],
    status: str,
    *,
    key_fingerprint: str | None = None,
) -> None:
    if status not in VALID_MARKER_STATUSES:
        raise ValueError("invalid marker status")
    updated = dict(marker)
    updated["status"] = status
    updated["updated_at"] = datetime.now(timezone.utc).isoformat()
    if status == "complete":
        if not isinstance(key_fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", key_fingerprint
        ):
            raise ValueError("a completed marker requires a key fingerprint")
        updated["key_fingerprint"] = key_fingerprint
    else:
        updated.pop("key_fingerprint", None)
    temporary = path.with_name(f"{path.name}.tmp")
    if _path_present(temporary) and (temporary.is_symlink() or not temporary.is_file()):
        raise SetupError("The temporary client setup marker is unsafe.")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(updated, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise SetupError("The client setup marker could not be updated safely.") from exc
    marker.clear()
    marker.update(updated)


def _stage_artifacts(paths: InstallPaths) -> set[Path]:
    artifacts = set(paths.root.glob(".env.client-*.stage"))
    artifacts.update((paths.root / "database").glob(".attendance.client-*.db*"))
    temporary_marker = paths.marker.with_name(f"{paths.marker.name}.tmp")
    if _path_present(temporary_marker):
        artifacts.add(temporary_marker)
    return artifacts


def _validate_stage_ownership(paths: InstallPaths, marker: dict[str, object] | None) -> None:
    artifacts = _stage_artifacts(paths)
    if not artifacts:
        return
    if any(artifact.is_symlink() or not artifact.is_file() for artifact in artifacts):
        raise SetupError("A setup staging path is not a regular local file.")
    if marker is None:
        raise SetupError(
            "Unowned setup staging files were found. Contact the technician; no files were changed."
        )
    run_id = str(marker["run_id"])
    allowed = {
        paths.staging_environment(run_id),
        paths.staging_database(run_id),
        Path(f"{paths.staging_database(run_id)}-wal"),
        Path(f"{paths.staging_database(run_id)}-shm"),
        paths.marker.with_name(f"{paths.marker.name}.tmp"),
    }
    unexpected = artifacts - allowed
    if unexpected:
        raise SetupError(
            "Staging files from another setup run were found; no files were changed."
        )


def _sqlite_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise SetupError("The local attendance database is missing or is not a regular file.")
    wal_path = Path(f"{path}-wal")
    shm_path = Path(f"{path}-shm")
    for companion in (wal_path, shm_path):
        if _path_present(companion) and (
            companion.is_symlink() or not companion.is_file()
        ):
            raise SetupError("An attendance database WAL path is not a regular local file.")
    has_wal = _path_present(wal_path)

    # A clean database can use SQLite's immutable read hint, which prevents a
    # read from creating empty WAL companions.  Whenever a WAL exists it is
    # deliberately omitted so committed WAL state remains visible; SQLite may
    # recreate only the transient SHM file after an interrupted writer.
    immutable_hint = "" if has_wal else "&immutable=1"
    uri = path.resolve().as_uri() + f"?mode=ro{immutable_hint}"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=15.0)
    except sqlite3.Error as exc:
        raise SetupError("The local attendance database could not be opened read-only.") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _normalized_schema_sql(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _validate_logical_schema(connection: sqlite3.Connection) -> None:
    objects = {
        (row["type"], row["name"]): row
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger')"
        ).fetchall()
    }

    for table_name, expected_columns in REQUIRED_TABLE_COLUMNS.items():
        table = objects.get(("table", table_name))
        if (
            table is None
            or table["tbl_name"] != table_name
            or not isinstance(table["sql"], str)
            or not table["sql"].strip()
        ):
            raise SetupError(
                f"The local attendance database is missing required table {table_name}."
            )
        columns = tuple(
            row["name"]
            for row in connection.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()
        )
        if columns != expected_columns:
            raise SetupError(
                f"The required database table {table_name} has an invalid column layout."
            )

    index_lists: dict[str, dict[str, sqlite3.Row]] = {}
    for index_name, (
        table_name,
        expected_unique,
        expected_partial,
        expected_columns,
    ) in REQUIRED_INDEXES.items():
        index = objects.get(("index", index_name))
        if (
            index is None
            or index["tbl_name"] != table_name
            or not isinstance(index["sql"], str)
            or not index["sql"].strip()
        ):
            raise SetupError(
                f"The local attendance database is missing required index {index_name}."
            )
        if table_name not in index_lists:
            index_lists[table_name] = {
                row["name"]: row
                for row in connection.execute(
                    f'PRAGMA index_list("{table_name}")'
                ).fetchall()
            }
        index_info = index_lists[table_name].get(index_name)
        if (
            index_info is None
            or bool(index_info["unique"]) is not expected_unique
            or bool(index_info["partial"]) is not expected_partial
        ):
            raise SetupError(f"Required database index {index_name} is invalid.")
        actual_columns = tuple(
            row["name"]
            for row in connection.execute(
                f'PRAGMA index_info("{index_name}")'
            ).fetchall()
        )
        if actual_columns != expected_columns:
            raise SetupError(f"Required database index {index_name} is invalid.")
        token = REQUIRED_INDEX_SQL_TOKENS.get(index_name)
        if token and token not in _normalized_schema_sql(index["sql"]):
            raise SetupError(f"Required database index {index_name} is invalid.")

    for trigger_name, (table_name, required_tokens) in REQUIRED_TRIGGERS.items():
        trigger = objects.get(("trigger", trigger_name))
        if (
            trigger is None
            or trigger["tbl_name"] != table_name
            or not isinstance(trigger["sql"], str)
            or not trigger["sql"].strip()
        ):
            raise SetupError(
                f"The local attendance database is missing required trigger {trigger_name}."
            )
        normalized_sql = _normalized_schema_sql(trigger["sql"])
        if any(token not in normalized_sql for token in required_tokens):
            raise SetupError(f"Required database trigger {trigger_name} is invalid.")


def _validate_database(
    path: Path,
    *,
    fresh: bool,
    env_values: Mapping[str, str],
    run_id: str,
    progress_handler: Callable[[], int] | None = None,
) -> None:
    try:
        with closing(_sqlite_read_only(path)) as connection:
            if progress_handler is not None:
                connection.set_progress_handler(progress_handler, 1000)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise SetupError("The local attendance database failed its integrity check.")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise SetupError("The local attendance database has foreign-key violations.")
            _validate_logical_schema(connection)
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            )
            if versions != EXPECTED_MIGRATIONS:
                raise SetupError(
                    "The local attendance database is not migrated through version 033."
                )
            _validate_database_binding(connection, env_values, run_id)
            active_deans = connection.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'Dean' AND is_active = 1"
            ).fetchone()[0]
            if active_deans < 1:
                raise SetupError("The local attendance database has no active Dean account.")
            if fresh:
                user_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                if active_deans != 1 or user_count != 1:
                    raise SetupError(
                        "The staged database does not contain exactly one initial active Dean."
                    )
                audit_count = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM system_audit_logs sal
                    JOIN users u ON u.id = sal.actor_user_id
                    WHERE sal.action = 'user_created'
                      AND u.role = 'Dean'
                      AND u.is_active = 1
                    """
                ).fetchone()[0]
                if audit_count != 1:
                    raise SetupError("The staged Dean creation audit is missing or duplicated.")
    except SetupError:
        raise
    except sqlite3.Error as exc:
        raise SetupError("The local attendance database schema is incomplete or invalid.") from exc


def _stage_user_state(path: Path) -> str:
    if not _path_present(path):
        return "absent"
    try:
        with closing(_sqlite_read_only(path)) as connection:
            users_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()
            if users_table is None:
                return "empty"
            users = connection.execute(
                "SELECT role, is_active FROM users ORDER BY id"
            ).fetchall()
    except SetupError:
        raise
    except sqlite3.Error as exc:
        raise SetupError("The staged attendance database is invalid.") from exc
    if not users:
        return "empty"
    if len(users) == 1 and users[0]["role"] == "Dean" and users[0]["is_active"] == 1:
        return "dean_present"
    raise SetupError("The staged database contains unexpected account data; no files were changed.")


_PROJECT_MODULE_PREFIXES = (
    "config",
    "core",
    "database",
    "migrate",
    "security",
    "services",
)


def _is_project_module(name: str) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in _PROJECT_MODULE_PREFIXES)


@contextmanager
def _project_runtime(
    paths: InstallPaths,
    staging_database: Path,
    env_values: Mapping[str, str],
) -> Iterator[tuple[object, object, object]]:
    """Load database/migration/audit modules from the selected client root."""

    _assert_private_storage(paths.root)
    saved_modules = {
        name: module for name, module in sys.modules.items() if _is_project_module(name)
    }
    for name in tuple(saved_modules):
        sys.modules.pop(name, None)
    saved_sys_path = list(sys.path)
    saved_environment = {name: os.environ.get(name) for name in env_values}
    project_logger = logging.getLogger("edge_attendance")
    saved_handlers = list(project_logger.handlers)
    saved_level = project_logger.level
    saved_propagate = project_logger.propagate
    project_logger.handlers = [logging.NullHandler()]
    project_logger.propagate = False
    sys.path.insert(0, str(paths.root))
    for name, value in env_values.items():
        os.environ[name] = value
    try:
        importlib.invalidate_caches()
        config = importlib.import_module("config")
        config.DATABASE_PATH = str(staging_database)
        config.SCHEMA_PATH = str(paths.root / "database" / "schema.sql")
        config.ENV_PATH = str(paths.environment)
        config.LOG_FILE = str(paths.root / ".client-setup.log")
        config.AUDIT_FALLBACK_LOG = str(paths.root / ".client-audit-fallback.log")
        db_manager = importlib.import_module("database.db_manager")
        migrations = importlib.import_module("migrate")
        audit = importlib.import_module("services.audit")
        runtime_versions = tuple(entry[0] for entry in migrations.MIGRATIONS)
        if runtime_versions != EXPECTED_MIGRATIONS:
            raise SetupError("The runtime migration manifest changed during setup.")
        yield db_manager, migrations, audit
    finally:
        for name in tuple(sys.modules):
            if _is_project_module(name):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.path[:] = saved_sys_path
        for name, previous in saved_environment.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        for handler in project_logger.handlers:
            if handler not in saved_handlers:
                handler.close()
        project_logger.handlers = saved_handlers
        project_logger.setLevel(saved_level)
        project_logger.propagate = saved_propagate


def _prepare_database(
    paths: InstallPaths,
    staging_database: Path,
    env_values: Mapping[str, str],
    credentials: DeanCredentials | None,
    run_id: str,
) -> None:
    from werkzeug.security import generate_password_hash

    with _project_runtime(paths, staging_database, env_values) as (
        db_manager,
        migrations,
        audit,
    ):
        db_manager.init_db()
        migrations.run_migrations()
        connection = db_manager.get_db_connection()
        try:
            rows = connection.execute(
                "SELECT id, username, role, is_active FROM users ORDER BY id"
            ).fetchall()
            if not rows:
                if credentials is None:
                    raise SetupError("Dean credentials are required to finish this setup run.")
                password_hash = generate_password_hash(credentials.password)
                cursor = connection.execute(
                    """
                    INSERT INTO users
                        (staff_id, username, password_hash, role,
                         requires_password_change, is_active)
                    VALUES (?, ?, ?, 'Dean', 0, 1)
                    """,
                    ("ADMIN-001", credentials.username, password_hash),
                )
                audit.log_system_event(
                    connection,
                    "user_created",
                    actor_user_id=cursor.lastrowid,
                    details={
                        "created_user_id": cursor.lastrowid,
                        "staff_id": "ADMIN-001",
                        "username": credentials.username,
                        "role": "Dean",
                        "bootstrap": True,
                    },
                )
                connection.commit()
            elif not (
                len(rows) == 1
                and rows[0]["role"] == "Dean"
                and rows[0]["is_active"] == 1
            ):
                raise SetupError("The staged database contains unexpected account data.")
            else:
                audit_count = connection.execute(
                    """
                    SELECT COUNT(*) FROM system_audit_logs
                    WHERE actor_user_id = ? AND action = 'user_created'
                    """,
                    (rows[0]["id"],),
                ).fetchone()[0]
                if audit_count == 0:
                    audit.log_system_event(
                        connection,
                        "user_created",
                        actor_user_id=rows[0]["id"],
                        details={
                            "created_user_id": rows[0]["id"],
                            "staff_id": "ADMIN-001",
                            "username": rows[0]["username"],
                            "role": "Dean",
                            "bootstrap": True,
                            "resumed": True,
                        },
                    )
                    connection.commit()
                elif audit_count != 1:
                    raise SetupError("The staged Dean creation audit is duplicated.")
            _ensure_database_binding(connection, env_values, run_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    _validate_database(
        staging_database,
        fresh=True,
        env_values=env_values,
        run_id=run_id,
    )
    try:
        checkpoint = sqlite3.connect(str(staging_database), timeout=15.0)
        try:
            result = checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result is None or result[0] != 0:
                raise SetupError("The staged database WAL checkpoint did not complete.")
        finally:
            checkpoint.close()
    except sqlite3.Error as exc:
        raise SetupError("The staged database could not be checkpointed safely.") from exc
    wal_path = Path(f"{staging_database}-wal")
    shm_path = Path(f"{staging_database}-shm")
    try:
        if _path_present(wal_path) and (wal_path.is_symlink() or not wal_path.is_file()):
            raise SetupError("A staged database checkpoint path is unsafe.")
        if _path_present(shm_path) and (shm_path.is_symlink() or not shm_path.is_file()):
            raise SetupError("A staged database checkpoint path is unsafe.")
        if _path_present(wal_path) and wal_path.stat().st_size != 0:
            raise SetupError("The staged database still has uncheckpointed WAL data.")
        for companion in (wal_path, shm_path):
            if _path_present(companion):
                companion.unlink()
    except OSError as exc:
        raise SetupError("The staged database checkpoint files could not be finalized.") from exc
    _validate_database(
        staging_database,
        fresh=True,
        env_values=env_values,
        run_id=run_id,
    )


def _atomic_publish_no_replace(staging: Path, destination: Path, description: str) -> None:
    """Atomically create ``destination`` without an overwrite race.

    Both files are always on the same client volume.  Creating a hard link is
    atomic and fails if the destination already exists; unlinking the staging
    name then completes the move without ever exposing a partial file.
    """

    if not staging.is_file() or staging.is_symlink():
        raise SetupError(f"The verified staged {description} is missing or unsafe.")
    try:
        os.link(staging, destination)
    except FileExistsError as exc:
        raise SetupError(f"The final {description} appeared during setup; nothing was replaced.") from exc
    except OSError as exc:
        raise SetupError(f"The verified {description} could not be published atomically.") from exc
    try:
        staging.unlink()
    except OSError as exc:
        # The final name is already a complete, verified hard link.  The marker
        # permits a later run to identify and remove only this staging alias.
        raise SetupError(f"The {description} was published but setup finalization was interrupted.") from exc


def _remove_published_staging_alias(staging: Path, destination: Path) -> None:
    if not _path_present(staging):
        return
    if staging.is_symlink() or not staging.is_file():
        raise SetupError("A published setup staging alias is unsafe.")
    try:
        if not os.path.samefile(staging, destination):
            raise SetupError("A setup staging file does not match its published file.")
        staging.unlink()
    except SetupError:
        raise
    except OSError as exc:
        raise SetupError("A published setup staging alias could not be finalized.") from exc


def _publish_database(staging: Path, destination: Path) -> None:
    _atomic_publish_no_replace(staging, destination, "attendance database")


def _publish_environment(staging: Path, destination: Path) -> None:
    _atomic_publish_no_replace(staging, destination, ".env")


def _complete_publication(
    paths: InstallPaths,
    marker: dict[str, object],
    *,
    status: str,
) -> SetupResult:
    _assert_private_storage(paths.root)
    run_id = str(marker["run_id"])
    staging_env = paths.staging_environment(run_id)
    staging_db = paths.staging_database(run_id)

    if status == "prepared":
        if _path_present(paths.environment) or _path_present(paths.database):
            raise SetupError("Final installation files appeared before publication completed.")
        env_values = _parse_env(staging_env)
        _validate_env_values(env_values)
        _validate_database(
            staging_db,
            fresh=True,
            env_values=env_values,
            run_id=run_id,
        )
        _assert_private_storage(paths.root)
        _publish_database(staging_db, paths.database)
        _update_marker(paths.marker, marker, "database_published")
        status = "database_published"

    if status == "database_published":
        if not paths.database.is_file() or _path_present(paths.environment):
            raise SetupError("The interrupted setup state is inconsistent; no files were changed.")
        _remove_published_staging_alias(staging_db, paths.database)
        env_values = _parse_env(staging_env)
        _validate_env_values(env_values)
        _validate_database(
            paths.database,
            fresh=True,
            env_values=env_values,
            run_id=run_id,
        )
        _assert_private_storage(paths.root)
        _publish_environment(staging_env, paths.environment)

    env_values = _parse_env(paths.environment)
    _validate_env_values(env_values)
    _validate_database(
        paths.database,
        fresh=True,
        env_values=env_values,
        run_id=run_id,
    )
    _update_marker(
        paths.marker,
        marker,
        "complete",
        key_fingerprint=_key_fingerprint(env_values, run_id),
    )
    return SetupResult("initialized", "Client database and environment initialized safely.")


def check_install(root: os.PathLike[str] | str) -> SetupResult:
    """Perform a strictly read-only client readiness check."""

    _require_python_311()
    _assert_private_storage(root)
    paths = _resolve_root(root)
    marker = _read_marker(paths.marker)
    _validate_stage_ownership(paths, marker)
    has_env = _path_present(paths.environment)
    has_database = _path_present(paths.database)
    if has_env != has_database:
        raise SetupError(
            "The .env and attendance database must either both exist or both be absent."
        )
    if not has_env:
        if marker is not None:
            raise SetupError("Client setup is incomplete; run the initialize command to resume it.")
        raise SetupError("This client has not been initialized yet.")
    env_values = _parse_env(paths.environment)
    _validate_env_values(env_values)
    if marker is None:
        raise SetupError(
            "The initialized client files have no setup marker; contact the technician."
        )
    if marker["status"] != "complete":
        raise SetupError("Client setup is incomplete; run the initialize command to resume it.")
    if _stage_artifacts(paths):
        raise SetupError("Completed client setup still has staging files; contact the technician.")
    run_id = str(marker["run_id"])
    _validate_database(
        paths.database,
        fresh=False,
        env_values=env_values,
        run_id=run_id,
    )
    expected_fingerprint = _key_fingerprint(env_values, run_id)
    if not secrets.compare_digest(
        str(marker["key_fingerprint"]), expected_fingerprint
    ):
        raise SetupError("The local security keys do not match this client installation.")
    return SetupResult("ready", "Client installation is ready.")


def initialize(
    root: os.PathLike[str] | str,
    credential_prompt: Callable[[], DeanCredentials] | None = None,
) -> SetupResult:
    """Initialize a fresh client or resume a marker-owned interrupted run."""

    _require_python_311()
    _assert_private_storage(root)
    paths = _resolve_root(root)
    marker = _read_marker(paths.marker)
    _validate_stage_ownership(paths, marker)
    has_env = _path_present(paths.environment)
    has_database = _path_present(paths.database)

    if marker is None:
        if has_env != has_database:
            raise SetupError(
                "The .env and attendance database must either both exist or both be absent; no files were changed."
            )
        if has_env:
            return check_install(paths.root)

        prompt = credential_prompt or prompt_dean_credentials
        credentials = _validate_dean_credentials(prompt())

        _assert_private_storage(paths.root)
        run_id = uuid4().hex
        marker = _create_marker(paths.marker, run_id)
        staging_env = paths.staging_environment(run_id)
        staging_db = paths.staging_database(run_id)
        _assert_private_storage(paths.root)
        env_values = _new_env_values()
        _write_new_file(staging_env, _render_env(env_values))
        _prepare_database(paths, staging_db, env_values, credentials, run_id)
        _update_marker(paths.marker, marker, "prepared")
        return _complete_publication(paths, marker, status="prepared")

    run_id = str(marker["run_id"])
    status = str(marker["status"])
    staging_env = paths.staging_environment(run_id)
    staging_db = paths.staging_database(run_id)

    if status == "complete":
        if has_env != has_database or not has_env:
            raise SetupError("The completed setup marker does not match the installation files.")
        return check_install(paths.root)

    # Infer the two atomic-publish crash windows before applying strict pairing.
    if has_env and has_database:
        if status not in {"prepared", "database_published"}:
            raise SetupError("The interrupted setup marker does not match the installation files.")
        env_values = _parse_env(paths.environment)
        _validate_env_values(env_values)
        _validate_database(
            paths.database,
            fresh=True,
            env_values=env_values,
            run_id=run_id,
        )
        _remove_published_staging_alias(staging_db, paths.database)
        _remove_published_staging_alias(staging_env, paths.environment)
        _update_marker(
            paths.marker,
            marker,
            "complete",
            key_fingerprint=_key_fingerprint(env_values, run_id),
        )
        return SetupResult("resumed", "Interrupted client setup was verified and finalized.")
    if has_database and not has_env:
        if status not in {"prepared", "database_published"} or not staging_env.is_file():
            raise SetupError("The interrupted setup state is inconsistent; no files were changed.")
        return _complete_publication(
            paths, marker, status="database_published"
        )
    if has_env and not has_database:
        raise SetupError("The interrupted setup state is inconsistent; no files were changed.")

    if status == "prepared":
        if not staging_env.is_file() or not staging_db.is_file():
            raise SetupError("Prepared setup staging files are incomplete; no files were changed.")
        return _complete_publication(paths, marker, status="prepared")
    if status != "preparing":
        raise SetupError("The interrupted setup state is inconsistent; no files were changed.")

    stage_user_state = _stage_user_state(staging_db)
    credentials: DeanCredentials | None = None
    if _path_present(staging_env):
        env_values = _parse_env(staging_env)
        _validate_env_values(env_values)
    else:
        if stage_user_state == "dean_present":
            raise SetupError("The interrupted setup lost its staged environment; no files were changed.")
        prompt = credential_prompt or prompt_dean_credentials
        credentials = _validate_dean_credentials(prompt())
        _assert_private_storage(paths.root)
        env_values = _new_env_values()
        _write_new_file(staging_env, _render_env(env_values))

    if stage_user_state != "dean_present" and credentials is None:
        prompt = credential_prompt or prompt_dean_credentials
        credentials = _validate_dean_credentials(prompt())
    _prepare_database(paths, staging_db, env_values, credentials, run_id)
    _update_marker(paths.marker, marker, "prepared")
    return _complete_publication(paths, marker, status="prepared")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Initialize or check a local CSD Attendance client installation."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("password-policy", help="Return public password guidance metadata; no installation or credentials are accessed")
    for command in ("initialize", "check"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--root", required=True, help="Absolute client bundle root")
        subparser.add_argument("--json", action="store_true", help="Return a structured result without credentials")
        if command == "initialize":
            inputs = subparser.add_mutually_exclusive_group()
            inputs.add_argument(
                "--credentials-stdin", action="store_true",
                help="Read setup-window credentials from a bounded UTF-8 stdin document",
            )
            inputs.add_argument('--no-prompt', action='store_true',
                                help='Verify or resume setup; return status 2 if account input is needed')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "password-policy":
        # Only public Unicode ranges; never accepts or evaluates a password.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from security.passwords import browser_password_policy
        print(json.dumps(browser_password_policy()))
        return 0

    def report(message, *, status, success, retryable_input=False):
        if args.json:
            result = {"ok": success, "status": status, "message": message}
            if retryable_input:
                result['retryable_input'] = True
            print(json.dumps(result))
        elif success:
            print(message)
        else:
            print(f"ERROR: {message}", file=sys.stderr)

    try:
        if args.command == "initialize":
            def input_required():
                raise SetupInputRequired('Enter administrator details in the setup window to continue.')
            prompt = (
                lambda: read_dean_credentials(sys.stdin.buffer)
            ) if args.credentials_stdin else (input_required if args.no_prompt else None)
            result = initialize(args.root, credential_prompt=prompt)
        else:
            result = check_install(args.root)
    except SetupStorageError as exc:
        report(str(exc), status="storage_access", success=False)
        return 3
    except SetupInputRequired as exc:
        report(str(exc), status='input_required', success=False)
        return 2
    except SetupInputError as exc:
        report(str(exc), status='error', success=False, retryable_input=True)
        return 1
    except SetupError as exc:
        report(str(exc), status="error", success=False)
        return 1
    except Exception:
        # Unexpected exceptions are intentionally not interpolated: exception
        # messages can contain credentials supplied by a dependency.
        report("Client setup failed unexpectedly. No existing installation was replaced.",
               status="error", success=False)
        return 1
    report(result.message, status=result.status, success=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
