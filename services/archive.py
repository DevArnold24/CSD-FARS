"""Archive creation for the two-step end-of-term purge workflow."""

import csv
import json
import os
import zipfile
from uuid import uuid4

from cryptography.fernet import Fernet

from database.db_manager import get_db_connection
import config
from services.backup import (
    BACKUP_DIR, _publish_new_file, _staged_bytes, create_sqlite_snapshot,
    ensure_backup_dir, timestamp_slug,
)
from services.private_storage import ensure_private_directory, private_temporary_directory, use_memory_sqlite_temp
from services.export_security import spreadsheet_safe_text
from core.utils import _institution_local_date_expr, _institution_local_time_expr


ARCHIVE_DIR = os.path.join(BACKUP_DIR, "archives")


ATTENDANCE_EXPORT_HEADERS = [
    "Session ID",
    "Session Date",
    "Scheduled Start",
    "Student ID",
    "Student Name",
    "Program",
    "Year Level",
    "Course Name",
    "Class Code",
    "Room Number",
    "Date",
    "Time Logged",
    "Status",
    "Reason",
    "Source",
    "Edited",
    "Latest Editor",
]


def ensure_archive_dir():
    ensure_backup_dir()
    ensure_private_directory(ARCHIVE_DIR)
    return ARCHIVE_DIR


def _attendance_rows(conn):
    local_record_date = _institution_local_date_expr("a.timestamp")
    local_record_time = _institution_local_time_expr("a.timestamp")
    return conn.execute(
        f"""
        SELECT
            ats.id AS session_id,
            COALESCE(ats.scheduled_date, {local_record_date}) AS session_date,
            COALESCE(ats.scheduled_start_time, c.start_time) AS scheduled_start,
            s.student_id AS student_id,
            s.name AS student_name,
            ap.name AS program,
            s.year_level AS year_level,
            c.course_name AS course_name,
            c.class_code AS class_code,
            COALESCE(r.room_code, '') AS room_number,
            CASE
                WHEN ats.id IS NULL THEN {local_record_date}
                ELSE DATE(a.timestamp)
            END AS attendance_date,
            CASE
                WHEN ats.id IS NULL THEN {local_record_time}
                ELSE TIME(a.timestamp)
            END AS time_logged,
            CASE
                WHEN a.status = 'Manual_Override' THEN COALESCE((
                    SELECT al.new_status
                    FROM audit_logs al
                    WHERE al.attendance_record_id = a.id
                    ORDER BY al.timestamp DESC
                    LIMIT 1
                ), 'Present')
                ELSE a.status
            END AS status,
            COALESCE(a.reason, '') AS reason,
            a.source AS source,
            CASE
                WHEN EXISTS(SELECT 1 FROM attendance_change_audit aca WHERE aca.attendance_record_id = a.id)
                  OR EXISTS(SELECT 1 FROM audit_logs al WHERE al.attendance_record_id = a.id)
                THEN 'Yes'
                ELSE 'No'
            END AS edited,
            COALESCE((
                SELECT u.username
                FROM attendance_change_audit aca
                JOIN users u ON aca.actor_user_id = u.id
                WHERE aca.attendance_record_id = a.id
                ORDER BY aca.changed_at DESC
                LIMIT 1
            ), '') AS latest_editor
        FROM attendance_records a
        JOIN students s ON a.student_id = s.id
        JOIN academic_programs ap ON s.program_id = ap.id
        JOIN classes c ON a.class_id = c.id
        LEFT JOIN rooms r ON c.room_id = r.id
        LEFT JOIN attendance_sessions ats ON a.attendance_session_id = ats.id
        ORDER BY COALESCE(ats.scheduled_date, {local_record_date}) DESC, c.course_name, s.name
        """
    ).fetchall()


def _write_attendance_csv(conn, csv_path):
    rows = _attendance_rows(conn)
    with open(csv_path, "w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(ATTENDANCE_EXPORT_HEADERS)
        for row in rows:
            writer.writerow([spreadsheet_safe_text(value) for value in [
                row["session_id"],
                row["session_date"],
                row["scheduled_start"],
                row["student_id"],
                row["student_name"],
                row["program"],
                row["year_level"],
                row["course_name"],
                row["class_code"],
                row["room_number"],
                row["attendance_date"],
                row["time_logged"],
                row["status"],
                row["reason"],
                row["source"],
                row["edited"],
                row["latest_editor"],
            ]])
    return len(rows)


def _table_count(conn, table_name):
    return conn.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()["count"]


def _system_state(conn):
    return {
        "users": [dict(row) for row in conn.execute("SELECT id, staff_id, username, role, is_active FROM users ORDER BY id")],
        "academic_programs": [dict(row) for row in conn.execute("SELECT * FROM academic_programs ORDER BY id")],
        "students": [dict(row) for row in conn.execute("SELECT id, student_id, name, program_id, year_level, consent_granted FROM students ORDER BY id")],
        "classes": [dict(row) for row in conn.execute("SELECT * FROM classes ORDER BY id")],
        "attendance_sessions": [dict(row) for row in conn.execute("SELECT * FROM attendance_sessions ORDER BY id")],
        "attendance_change_audit": [dict(row) for row in conn.execute("SELECT * FROM attendance_change_audit ORDER BY id")],
        "camera_devices": [dict(row) for row in conn.execute("SELECT id, name, camera_type, placement_notes, resolution_width, resolution_height, target_fps, match_threshold, is_default, is_active, last_test_status, last_test_at, last_successful_frame, failure_count, last_failure_reason, last_health_check_at FROM camera_devices ORDER BY id")],
    }


def create_purge_archive(actor_user_id):
    """Publish a new encrypted purge archive without replacing prior evidence."""
    key = getattr(config, "PURGE_ARCHIVE_KEY", None)
    if not key:
        raise RuntimeError("PURGE_ARCHIVE_KEY is not configured.")
    if key in {getattr(config, "BIOMETRIC_KEY", None), os.environ.get("CAMERA_SOURCE_KEY")}:
        raise RuntimeError("PURGE_ARCHIVE_KEY must be independent from biometric and camera keys.")

    archive_dir = ensure_archive_dir()
    slug = timestamp_slug()
    base_name = f"purge_archive_{slug}"
    # All plaintext artifacts stay under a checked, operation-owned directory.
    with private_temporary_directory(archive_dir) as temp_dir:
        snapshot_path = os.path.join(temp_dir, "attendance.db")
        csv_path = os.path.join(temp_dir, "attendance.csv")
        state_path = os.path.join(temp_dir, "system_state.json")
        manifest_path = os.path.join(temp_dir, "manifest.json")
        plain_zip_path = os.path.join(temp_dir, f"{base_name}.zip")
        manifest = None
        create_sqlite_snapshot(snapshot_path)
        conn = get_db_connection()
        try:
            use_memory_sqlite_temp(conn)
            attendance_count = _write_attendance_csv(conn, csv_path)
            with open(state_path, "w", encoding="utf-8") as output:
                json.dump(_system_state(conn), output, indent=2, default=str)
            row_counts = {
                table: _table_count(conn, table)
                for table in (
                    "users", "academic_programs", "students", "embeddings",
                    "camera_devices", "classes", "class_enrollments",
                    "attendance_sessions", "attendance_records",
                    "attendance_change_audit", "audit_logs",
                )
            }
        finally:
            conn.close()
        manifest = {
            "created_at_utc": slug,
            "actor_user_id": actor_user_id,
            "attendance_export_rows": attendance_count,
            "row_counts": row_counts,
            "sqlite_backup_file": "attendance.db",
        }
        with open(manifest_path, "w", encoding="utf-8") as output:
            json.dump(manifest, output, indent=2)
        with zipfile.ZipFile(plain_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(csv_path, "attendance.csv")
            archive.write(state_path, "system_state.json")
            archive.write(manifest_path, "manifest.json")
            archive.write(snapshot_path, "attendance.db")
        with open(plain_zip_path, "rb") as source:
            encrypted = Fernet(key.encode() if isinstance(key, str) else key).encrypt(source.read())
        with _staged_bytes(encrypted, archive_dir) as temporary:
            for _ in range(10):
                final_path = os.path.join(archive_dir, f"{base_name}_{uuid4().hex}.enc")
                try:
                    _publish_new_file(temporary, final_path)
                except FileExistsError:
                    continue
                return {
                    "archive_file": os.path.basename(final_path),
                    "manifest": manifest,
                }
            raise FileExistsError("Could not allocate a unique purge archive filename.")
