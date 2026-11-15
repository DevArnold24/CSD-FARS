"""Data-preserving database migration runner."""

import config

from database.db_manager import get_db_connection
from core import get_logger


logger = get_logger("migrate")


def _table_exists(conn, table_name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _column_exists(conn, table_name, column_name):
    return any(row["name"] == column_name for row in conn.execute(f"PRAGMA table_info({table_name})"))


def _ensure_schema_migrations(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    TEXT     PRIMARY KEY,
            name       TEXT     NOT NULL,
            applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _mark_applied(conn, version, name):
    conn.execute(
        "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
        (version, name),
    )


def _is_applied(conn, version):
    row = conn.execute(
        "SELECT version FROM schema_migrations WHERE version = ?",
        (version,),
    ).fetchone()
    return row is not None


def migration_006_academic_programs(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS academic_programs (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               name TEXT UNIQUE NOT NULL,
               is_active BOOLEAN NOT NULL DEFAULT 1
           )"""
    )
    baseline = (
        "BS Computer Science", "BS Information Technology",
        "BS Computer Engineering", "BS Business Administration",
        "General / Undecided",
    )
    for name in baseline:
        conn.execute("INSERT OR IGNORE INTO academic_programs (name, is_active) VALUES (?, 1)", (name,))
    if not _table_exists(conn, "students"):
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(students)")}
    if "program_id" not in columns:
        conn.execute("ALTER TABLE students ADD COLUMN program_id INTEGER")
    if "year_level" not in columns:
        conn.execute("ALTER TABLE students ADD COLUMN year_level TEXT NOT NULL DEFAULT '1st Year'")
    if "program" in columns:
        for row in conn.execute("SELECT DISTINCT program FROM students WHERE program IS NOT NULL"):
            conn.execute("INSERT OR IGNORE INTO academic_programs (name, is_active) VALUES (?, 1)", (row[0],))
        conn.execute(
            """UPDATE students SET program_id = COALESCE(
                   (SELECT id FROM academic_programs WHERE name = students.program),
                   (SELECT id FROM academic_programs WHERE name = 'General / Undecided'))
               WHERE program_id IS NULL"""
        )
    else:
        conn.execute(
            """UPDATE students SET program_id =
                   (SELECT id FROM academic_programs WHERE name = 'General / Undecided')
               WHERE program_id IS NULL"""
        )


def migration_007_section_column(conn):
    if _table_exists(conn, "classes") and not _column_exists(conn, "classes", "section"):
        conn.execute("ALTER TABLE classes ADD COLUMN section TEXT NOT NULL DEFAULT 'Section A'")


def migration_008_class_crud_archive(conn):
    if not _table_exists(conn, "classes"):
        return
    if not _column_exists(conn, "classes", "is_archived"):
        conn.execute("ALTER TABLE classes ADD COLUMN is_archived BOOLEAN NOT NULL DEFAULT 0")
    duplicates = conn.execute(
        """SELECT teacher_id, class_code FROM classes
           GROUP BY teacher_id, class_code HAVING COUNT(*) > 1"""
    ).fetchall()
    for teacher_id, class_code in duplicates:
        rows = conn.execute(
            "SELECT id FROM classes WHERE teacher_id = ? AND class_code = ? ORDER BY id",
            (teacher_id, class_code),
        ).fetchall()
        for row in rows[1:]:
            conn.execute("UPDATE classes SET class_code = class_code || '-' || id WHERE id = ?", (row[0],))
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_classes_teacher_code ON classes(teacher_id, class_code)"
    )


def migration_009_biometric_encryption(conn):
    from services.biometric_security import encrypt_face_vector, is_encrypted
    for table in ("embeddings", "pending_student_registrations"):
        if not _table_exists(conn, table) or not _column_exists(conn, table, "face_vector"):
            continue
        for row in conn.execute(f"SELECT id, face_vector FROM {table} WHERE face_vector IS NOT NULL").fetchall():
            if not is_encrypted(row["face_vector"]):
                conn.execute(
                    f"UPDATE {table} SET face_vector = ? WHERE id = ?",
                    (encrypt_face_vector(row["face_vector"]), row["id"]),
                )


def migration_001_production_hardening(conn):
    """Add production hardening metadata without wiping existing data."""
    if _table_exists(conn, "camera_devices"):
        camera_columns = {
            "last_successful_frame": "DATETIME",
            "failure_count": "INTEGER NOT NULL DEFAULT 0",
            "last_failure_reason": "TEXT",
            "last_health_check_at": "DATETIME",
        }
        for column_name, definition in camera_columns.items():
            if not _column_exists(conn, "camera_devices", column_name):
                conn.execute(f"ALTER TABLE camera_devices ADD COLUMN {column_name} {definition}")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_audit_logs (
            id            INTEGER  PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER,
            action        TEXT     NOT NULL,
            details       TEXT     NOT NULL DEFAULT '',
            created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )


def migration_002_teacher_registration_approvals(conn):
    """Add Teacher-submitted student registration approval queue."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_student_registrations (
            id                   INTEGER   PRIMARY KEY AUTOINCREMENT,
            student_id           TEXT      NOT NULL,
            name                 TEXT      NOT NULL,
            program_id           INTEGER   NOT NULL,
            year_level           TEXT      NOT NULL,
            consent_granted      BOOLEAN   NOT NULL DEFAULT 1,
            face_vector          BLOB,
            submitted_by_user_id INTEGER   NOT NULL,
            submitted_at         DATETIME  NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status               TEXT      NOT NULL DEFAULT 'Pending'
                                          CHECK(status IN ('Pending', 'Approved', 'Rejected')),
            reviewed_by_user_id  INTEGER,
            reviewed_at          DATETIME,
            review_note          TEXT      NOT NULL DEFAULT '',
            FOREIGN KEY (program_id) REFERENCES academic_programs(id),
            FOREIGN KEY (submitted_by_user_id) REFERENCES users(id) ON DELETE RESTRICT,
            FOREIGN KEY (reviewed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_student_unique_pending
        ON pending_student_registrations(student_id)
        WHERE status = 'Pending'
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pending_student_status_submitted
        ON pending_student_registrations(status, submitted_at)
        """
    )


def _normalize_attendance_status(status):
    if status in {"Present", "Late", "Absent", "Excused"}:
        return status
    return "Present"


def migration_003_attendance_sessions(conn):
    """Add session-backed attendance records and session-aware audit history."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance_sessions (
            id                   INTEGER  PRIMARY KEY AUTOINCREMENT,
            class_id             INTEGER  NOT NULL,
            teacher_id           INTEGER,
            camera_id            INTEGER,
            scheduled_date       DATE     NOT NULL,
            scheduled_start_time TIME     NOT NULL,
            actual_started_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            actual_ended_at      DATETIME,
            status               TEXT     NOT NULL DEFAULT 'Open'
                                          CHECK(status IN ('Open', 'Closed', 'Cancelled')),
            close_reason         TEXT     NOT NULL DEFAULT '',
            created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (class_id)   REFERENCES classes(id) ON DELETE CASCADE,
            FOREIGN KEY (teacher_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (camera_id)  REFERENCES camera_devices(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_attendance_sessions_class_date
        ON attendance_sessions(class_id, scheduled_date, status)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance_change_audit (
            id                    INTEGER  PRIMARY KEY AUTOINCREMENT,
            attendance_session_id INTEGER,
            attendance_record_id  INTEGER,
            class_id              INTEGER  NOT NULL,
            student_id            INTEGER  NOT NULL,
            actor_user_id         INTEGER  NOT NULL,
            previous_status       TEXT     NOT NULL DEFAULT 'Absent',
            new_status            TEXT     NOT NULL CHECK(new_status IN ('Present', 'Late', 'Absent', 'Excused')),
            previous_reason       TEXT     NOT NULL DEFAULT '',
            new_reason            TEXT     NOT NULL DEFAULT '',
            change_reason         TEXT     NOT NULL,
            changed_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (attendance_session_id) REFERENCES attendance_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (attendance_record_id)  REFERENCES attendance_records(id)  ON DELETE SET NULL,
            FOREIGN KEY (class_id)              REFERENCES classes(id)             ON DELETE CASCADE,
            FOREIGN KEY (student_id)            REFERENCES students(id)            ON DELETE CASCADE,
            FOREIGN KEY (actor_user_id)         REFERENCES users(id)               ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_attendance_change_audit_session
        ON attendance_change_audit(attendance_session_id, changed_at)
        """
    )

    if not _table_exists(conn, "attendance_records"):
        return

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(attendance_records)")}
    create_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'attendance_records'"
    ).fetchone()
    create_sql = create_sql_row["sql"] if create_sql_row else ""
    needs_rebuild = (
        "attendance_session_id" not in columns
        or "source" not in columns
        or "Excused" not in create_sql
    )

    if not needs_rebuild:
        return

    legacy_records = conn.execute(
        """
        SELECT a.id, a.student_id, a.class_id, a.timestamp, a.status,
               DATE(a.timestamp) AS session_date,
               c.teacher_id, c.camera_id, c.start_time,
               COALESCE((
                   SELECT al.new_status
                   FROM audit_logs al
                   WHERE al.attendance_record_id = a.id
                   ORDER BY al.timestamp DESC
                   LIMIT 1
               ), a.status) AS effective_status,
               COALESCE((
                   SELECT al.reason
                   FROM audit_logs al
                   WHERE al.attendance_record_id = a.id
                   ORDER BY al.timestamp DESC
                   LIMIT 1
               ), '') AS latest_reason,
               (
                   SELECT al.teacher_id
                   FROM audit_logs al
                   WHERE al.attendance_record_id = a.id
                   ORDER BY al.timestamp DESC
                   LIMIT 1
               ) AS latest_actor_id
        FROM attendance_records a
        JOIN classes c ON a.class_id = c.id
        ORDER BY a.id
        """
    ).fetchall()
    legacy_audits = conn.execute(
        """
        SELECT al.*, a.class_id, a.student_id, DATE(a.timestamp) AS session_date
        FROM audit_logs al
        JOIN attendance_records a ON al.attendance_record_id = a.id
        ORDER BY al.id
        """
    ).fetchall()

    session_map = {}
    for record in legacy_records:
        key = (record["class_id"], record["session_date"])
        if key in session_map:
            continue
        matching = [row for row in legacy_records if (row["class_id"], row["session_date"]) == key]
        first_timestamp = min(row["timestamp"] for row in matching)
        last_timestamp = max(row["timestamp"] for row in matching)
        cursor = conn.execute(
            """
            INSERT INTO attendance_sessions
                (class_id, teacher_id, camera_id, scheduled_date, scheduled_start_time,
                 actual_started_at, actual_ended_at, status, close_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'Closed', 'Migrated from legacy attendance records.')
            """,
            (
                record["class_id"],
                record["teacher_id"],
                record["camera_id"],
                record["session_date"],
                record["start_time"],
                first_timestamp,
                last_timestamp,
            ),
        )
        session_map[key] = cursor.lastrowid

    conn.execute("ALTER TABLE attendance_records RENAME TO attendance_records_legacy_003")
    conn.execute(
        """
        CREATE TABLE attendance_records (
            id                    INTEGER  PRIMARY KEY AUTOINCREMENT,
            attendance_session_id INTEGER,
            student_id            INTEGER  NOT NULL,
            class_id              INTEGER  NOT NULL,
            timestamp             DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status                TEXT     NOT NULL CHECK(status IN ('Present', 'Late', 'Absent', 'Excused')),
            source                TEXT     NOT NULL DEFAULT 'face_recognition'
                                              CHECK(source IN ('face_recognition', 'manual_edit', 'system', 'migration')),
            reason                TEXT     NOT NULL DEFAULT '',
            updated_by_user_id    INTEGER,
            updated_at            DATETIME,
            FOREIGN KEY (attendance_session_id) REFERENCES attendance_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (class_id)   REFERENCES classes(id)  ON DELETE CASCADE,
            FOREIGN KEY (updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
            UNIQUE (attendance_session_id, student_id)
        )
        """
    )

    for record in legacy_records:
        status = _normalize_attendance_status(record["effective_status"])
        source = "manual_edit" if record["latest_actor_id"] else "migration"
        reason = record["latest_reason"] if record["latest_actor_id"] else ""
        session_id = session_map.get((record["class_id"], record["session_date"]))
        conn.execute(
            """
            INSERT OR REPLACE INTO attendance_records
                (id, attendance_session_id, student_id, class_id, timestamp,
                 status, source, reason, updated_by_user_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["id"],
                session_id,
                record["student_id"],
                record["class_id"],
                record["timestamp"],
                status,
                source,
                reason,
                record["latest_actor_id"],
                record["timestamp"] if record["latest_actor_id"] else None,
            ),
        )

    conn.execute("DROP TABLE IF EXISTS attendance_change_audit")
    conn.execute(
        """
        CREATE TABLE attendance_change_audit (
            id                    INTEGER  PRIMARY KEY AUTOINCREMENT,
            attendance_session_id INTEGER,
            attendance_record_id  INTEGER,
            class_id              INTEGER  NOT NULL,
            student_id            INTEGER  NOT NULL,
            actor_user_id         INTEGER  NOT NULL,
            previous_status       TEXT     NOT NULL DEFAULT 'Absent',
            new_status            TEXT     NOT NULL CHECK(new_status IN ('Present', 'Late', 'Absent', 'Excused')),
            previous_reason       TEXT     NOT NULL DEFAULT '',
            new_reason            TEXT     NOT NULL DEFAULT '',
            change_reason         TEXT     NOT NULL,
            changed_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (attendance_session_id) REFERENCES attendance_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (attendance_record_id)  REFERENCES attendance_records(id)  ON DELETE SET NULL,
            FOREIGN KEY (class_id)              REFERENCES classes(id)             ON DELETE CASCADE,
            FOREIGN KEY (student_id)            REFERENCES students(id)            ON DELETE CASCADE,
            FOREIGN KEY (actor_user_id)         REFERENCES users(id)               ON DELETE CASCADE
        )
        """
    )

    for audit in legacy_audits:
        session_id = session_map.get((audit["class_id"], audit["session_date"]))
        conn.execute(
            """
            INSERT INTO attendance_change_audit
                (attendance_session_id, attendance_record_id, class_id, student_id,
                 actor_user_id, previous_status, new_status, previous_reason,
                 new_reason, change_reason, changed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
            """,
            (
                session_id,
                audit["attendance_record_id"],
                audit["class_id"],
                audit["student_id"],
                audit["teacher_id"],
                _normalize_attendance_status(audit["previous_status"]),
                _normalize_attendance_status(audit["new_status"]),
                audit["reason"],
                audit["reason"],
                audit["timestamp"],
            ),
        )

    conn.execute("DROP TABLE IF EXISTS audit_logs")
    conn.execute("DROP TABLE attendance_records_legacy_003")
    conn.execute(
        """
        CREATE TABLE audit_logs (
            id                   INTEGER  PRIMARY KEY AUTOINCREMENT,
            attendance_record_id INTEGER  NOT NULL,
            teacher_id           INTEGER  NOT NULL,
            previous_status      TEXT     NOT NULL CHECK(previous_status IN ('Absent', 'Present', 'Late', 'Manual_Override')),
            new_status           TEXT     NOT NULL CHECK(new_status IN ('Absent', 'Present', 'Late', 'Manual_Override')),
            reason               TEXT     NOT NULL,
            timestamp            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (attendance_record_id) REFERENCES attendance_records(id) ON DELETE CASCADE,
            FOREIGN KEY (teacher_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    for audit in legacy_audits:
        conn.execute(
            """
            INSERT INTO audit_logs
                (id, attendance_record_id, teacher_id, previous_status, new_status, reason, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit["id"],
                audit["attendance_record_id"],
                audit["teacher_id"],
                audit["previous_status"],
                audit["new_status"],
                audit["reason"],
                audit["timestamp"],
            ),
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_attendance_student_class
        ON attendance_records(student_id, class_id, timestamp)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_attendance_session_student
        ON attendance_records(attendance_session_id, student_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_attendance_change_audit_session
        ON attendance_change_audit(attendance_session_id, changed_at)
        """
    )


def _is_fallback_room_value(value):
    return str(value or "").strip().lower() in ("", "unassigned", "fallback", "demo")


def _ensure_room_for_code(conn, room_code):
    room_code = str(room_code or "").strip()
    if _is_fallback_room_value(room_code):
        return None
    conn.execute(
        """
        INSERT OR IGNORE INTO rooms
            (room_code, room_name, department, room_type, is_active)
        VALUES (?, ?, 'Computer Studies Department', 'Classroom', 1)
        """,
        (room_code, f"Room {room_code}"),
    )
    row = conn.execute("SELECT id FROM rooms WHERE room_code = ?", (room_code,)).fetchone()
    return row["id"] if row else None


def migration_004_room_camera_coverage(conn):
    """Add official rooms and link existing cameras/classes to them."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rooms (
            id          INTEGER  PRIMARY KEY AUTOINCREMENT,
            room_code   TEXT     NOT NULL UNIQUE,
            room_name   TEXT     NOT NULL DEFAULT '',
            building    TEXT     NOT NULL DEFAULT '',
            floor       TEXT     NOT NULL DEFAULT '',
            department  TEXT     NOT NULL DEFAULT 'Computer Studies Department',
            room_type   TEXT     NOT NULL DEFAULT 'Classroom',
            capacity    INTEGER,
            is_active   BOOLEAN  NOT NULL DEFAULT 1,
            notes       TEXT     NOT NULL DEFAULT '',
            created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_rooms_active_code
        ON rooms(is_active, room_code)
        """
    )

    if _table_exists(conn, "camera_devices"):
        if not _column_exists(conn, "camera_devices", "room_id"):
            conn.execute("ALTER TABLE camera_devices ADD COLUMN room_id INTEGER")
        if not _column_exists(conn, "camera_devices", "lifecycle_status"):
            conn.execute(
                "ALTER TABLE camera_devices ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'Ready'"
            )

    if _table_exists(conn, "classes") and not _column_exists(conn, "classes", "room_id"):
        conn.execute("ALTER TABLE classes ADD COLUMN room_id INTEGER")

    room_values = []
    if _table_exists(conn, "camera_devices") and _column_exists(conn, "camera_devices", "room_number"):
        room_values.extend(
            row["room_number"]
            for row in conn.execute("SELECT DISTINCT room_number FROM camera_devices")
        )
    if _table_exists(conn, "classes") and _column_exists(conn, "classes", "room_number"):
        room_values.extend(
            row["room_number"]
            for row in conn.execute("SELECT DISTINCT room_number FROM classes")
        )

    for room_code in sorted({str(value or "").strip() for value in room_values if not _is_fallback_room_value(value)}):
        _ensure_room_for_code(conn, room_code)

    if _table_exists(conn, "camera_devices") and _column_exists(conn, "camera_devices", "room_number"):
        for camera in conn.execute("SELECT id, room_number, is_default FROM camera_devices").fetchall():
            if camera["is_default"] or _is_fallback_room_value(camera["room_number"]):
                conn.execute(
                    "UPDATE camera_devices SET room_id = NULL, room_number = 'Fallback' WHERE id = ?",
                    (camera["id"],),
                )
                continue
            room_id = _ensure_room_for_code(conn, camera["room_number"])
            conn.execute(
                "UPDATE camera_devices SET room_id = ? WHERE id = ?",
                (room_id, camera["id"]),
            )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_camera_devices_room_active
            ON camera_devices(room_id, is_active)
            """
        )

    if _table_exists(conn, "classes") and _column_exists(conn, "classes", "room_number"):
        for class_row in conn.execute("SELECT id, room_number FROM classes").fetchall():
            room_id = _ensure_room_for_code(conn, class_row["room_number"])
            conn.execute(
                "UPDATE classes SET room_id = ? WHERE id = ?",
                (room_id, class_row["id"]),
            )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_classes_room
            ON classes(room_id)
            """
        )


def migration_005_user_lockout_fields(conn):
    """Add failed_login_attempts and locked_until columns to users table."""
    if _table_exists(conn, "users"):
        if not _column_exists(conn, "users", "failed_login_attempts"):
            conn.execute("ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0")
        if not _column_exists(conn, "users", "locked_until"):
            conn.execute("ALTER TABLE users ADD COLUMN locked_until TIMESTAMP")


def migration_010_student_is_active(conn):
    """Add is_active column to students table for soft deletion."""
    if _table_exists(conn, "students"):
        if not _column_exists(conn, "students", "is_active"):
            conn.execute("ALTER TABLE students ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1")

def migration_011_drop_room_number(conn):
    """Drop redundant room_number columns from classes and camera_devices."""
    if _table_exists(conn, "classes"):
        if _column_exists(conn, "classes", "room_number"):
            conn.execute("ALTER TABLE classes DROP COLUMN room_number")
    if _table_exists(conn, "camera_devices"):
        if _column_exists(conn, "camera_devices", "room_number"):
            conn.execute("DROP INDEX IF EXISTS idx_camera_devices_room_active")
            conn.execute("ALTER TABLE camera_devices DROP COLUMN room_number")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_camera_devices_room_active ON camera_devices(room_id, is_active)")


def migration_012_nullable_camera_id(conn):
    """Make camera_id nullable in classes table."""
    if _table_exists(conn, "classes"):
        camera_column = next(
            (row for row in conn.execute("PRAGMA table_info(classes)") if row["name"] == "camera_id"),
            None,
        )
        if camera_column is not None and not camera_column["notnull"]:
            return
        conn.execute(
            """
            CREATE TABLE classes_new (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                teacher_id               INTEGER NOT NULL,
                camera_id                INTEGER,
                room_id                  INTEGER,
                course_name              TEXT    NOT NULL,
                class_code               TEXT    NOT NULL,
                section                  TEXT    NOT NULL DEFAULT 'Section A',
                days_of_week             TEXT    NOT NULL,
                start_time               TIME    NOT NULL,
                end_time                 TIME    NOT NULL,
                late_threshold_minutes   INTEGER NOT NULL DEFAULT 15,
                is_active                BOOLEAN NOT NULL DEFAULT 0,
                is_archived              BOOLEAN NOT NULL DEFAULT 0,
                FOREIGN KEY (room_id)   REFERENCES rooms(id) ON DELETE SET NULL,
                FOREIGN KEY (camera_id)  REFERENCES camera_devices(id),
                FOREIGN KEY (teacher_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE (teacher_id, class_code)
            )
            """
        )
        conn.execute("INSERT INTO classes_new SELECT * FROM classes")
        conn.execute("DROP TABLE classes")
        conn.execute("ALTER TABLE classes_new RENAME TO classes")


def migration_013_exclusive_cctv(conn):
    """Normalize camera storage without deleting supported local cameras."""
    if _table_exists(conn, "camera_devices"):
        conn.execute(
            """
            CREATE TABLE camera_devices_new (
                id                INTEGER  PRIMARY KEY AUTOINCREMENT,
                room_id           INTEGER,
                name              TEXT     NOT NULL UNIQUE,
                camera_type       TEXT     NOT NULL DEFAULT 'local' CHECK(camera_type IN ('local', 'rtsp')),
                source            TEXT     NOT NULL DEFAULT '0',
                placement_notes   TEXT     NOT NULL DEFAULT '',
                lifecycle_status  TEXT     NOT NULL DEFAULT 'Ready'
                                                  CHECK(lifecycle_status IN ('Planned', 'Installed', 'Testing', 'Ready', 'Maintenance', 'Retired')),
                resolution_width  INTEGER  NOT NULL DEFAULT 640,
                resolution_height INTEGER  NOT NULL DEFAULT 480,
                target_fps        INTEGER  NOT NULL DEFAULT 30,
                is_default        BOOLEAN  NOT NULL DEFAULT 0,
                is_active         BOOLEAN  NOT NULL DEFAULT 1,
                last_test_status  TEXT,
                last_test_at      DATETIME,
                last_successful_frame DATETIME,
                failure_count     INTEGER  NOT NULL DEFAULT 0,
                last_failure_reason TEXT,
                last_health_check_at DATETIME,
                created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO camera_devices_new (
                id, room_id, name, camera_type, source, placement_notes, lifecycle_status,
                resolution_width, resolution_height, target_fps, is_default, is_active,
                last_test_status, last_test_at, last_successful_frame, failure_count,
                last_failure_reason, last_health_check_at, created_at, updated_at
            )
            SELECT id, room_id, name, camera_type, source, placement_notes, lifecycle_status,
                   resolution_width, resolution_height, target_fps, is_default, is_active,
                   last_test_status, last_test_at, last_successful_frame, failure_count,
                   last_failure_reason, last_health_check_at, created_at, updated_at
            FROM camera_devices
            """
        )
        conn.execute("DROP TABLE camera_devices")
        conn.execute("ALTER TABLE camera_devices_new RENAME TO camera_devices")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_camera_devices_single_default
            ON camera_devices(is_default)
            WHERE is_default = 1
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_camera_devices_room_active
            ON camera_devices(room_id, is_active)
            """
        )


def migration_014_camera_performance(conn):
    if _table_exists(conn, "camera_devices") and not _column_exists(conn, "camera_devices", "match_threshold"):
        conn.execute("ALTER TABLE camera_devices ADD COLUMN match_threshold FLOAT NOT NULL DEFAULT 0.45")


def migration_015_recognition_settings(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS recognition_settings (
               id INTEGER PRIMARY KEY CHECK(id = 1),
               match_threshold REAL NOT NULL DEFAULT 0.50,
               match_margin REAL NOT NULL DEFAULT 0.20,
               match_confirmation_passes INTEGER NOT NULL DEFAULT 2,
               frame_skip_interval INTEGER NOT NULL DEFAULT 3,
               downscale_height INTEGER NOT NULL DEFAULT 720,
               liveness_ttl_seconds REAL NOT NULL DEFAULT 10.0,
               updated_by_user_id INTEGER,
               updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
               FOREIGN KEY (updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL
           )"""
    )
    conn.execute("INSERT OR IGNORE INTO recognition_settings (id) VALUES (1)")


def migration_016_restore_supported_camera_types(conn):
    """Repair databases created by historical migration 013's RTSP-only constraint."""
    if not _table_exists(conn, "camera_devices"):
        return
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'camera_devices'"
    ).fetchone()
    if sql_row and "'local'" in (sql_row["sql"] or ""):
        return
    conn.execute(
        """CREATE TABLE camera_devices_repaired (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               room_id INTEGER,
               name TEXT NOT NULL UNIQUE,
               camera_type TEXT NOT NULL DEFAULT 'local' CHECK(camera_type IN ('local', 'rtsp')),
               source TEXT NOT NULL DEFAULT '0', placement_notes TEXT NOT NULL DEFAULT '',
               lifecycle_status TEXT NOT NULL DEFAULT 'Ready'
                   CHECK(lifecycle_status IN ('Planned','Installed','Testing','Ready','Maintenance','Retired')),
               resolution_width INTEGER NOT NULL DEFAULT 640,
               resolution_height INTEGER NOT NULL DEFAULT 480,
               target_fps INTEGER NOT NULL DEFAULT 30,
               match_threshold REAL NOT NULL DEFAULT 0.45,
               is_default BOOLEAN NOT NULL DEFAULT 0, is_active BOOLEAN NOT NULL DEFAULT 1,
               last_test_status TEXT, last_test_at DATETIME, last_successful_frame DATETIME,
               failure_count INTEGER NOT NULL DEFAULT 0, last_failure_reason TEXT,
               last_health_check_at DATETIME, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
               updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
               FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE SET NULL
           )"""
    )
    conn.execute(
        """INSERT INTO camera_devices_repaired
           SELECT id, room_id, name, camera_type, source, placement_notes, lifecycle_status,
                  resolution_width, resolution_height, target_fps, match_threshold,
                  is_default, is_active, last_test_status, last_test_at, last_successful_frame,
                  failure_count, last_failure_reason, last_health_check_at, created_at, updated_at
           FROM camera_devices"""
    )
    conn.execute("DROP TABLE camera_devices")
    conn.execute("ALTER TABLE camera_devices_repaired RENAME TO camera_devices")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_camera_devices_single_default ON camera_devices(is_default) WHERE is_default = 1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_camera_devices_room_active ON camera_devices(room_id, is_active)")


def migration_017_student_permanent_removal_tombstone(conn):
    """Retain historical student references while removing profiles from active use."""
    if _table_exists(conn, "students") and not _column_exists(conn, "students", "permanently_removed_at"):
        conn.execute("ALTER TABLE students ADD COLUMN permanently_removed_at DATETIME")


def _canonical_location_label(value, fallback):
    """Collapse legacy whitespace while preserving a stable display spelling."""
    return " ".join(str(value or "").split()) or fallback


def _normalized_location_name(value, fallback):
    return _canonical_location_label(value, fallback).casefold()


def _ensure_canonical_building(conn, name):
    display_name = _canonical_location_label(name, "Unspecified Building")
    normalized_name = display_name.casefold()
    row = conn.execute(
        "SELECT id, name FROM buildings WHERE normalized_name = ?",
        (normalized_name,),
    ).fetchone()
    if row:
        return row["id"], row["name"]
    cursor = conn.execute(
        "INSERT INTO buildings (name, normalized_name, is_active) VALUES (?, ?, 1)",
        (display_name, normalized_name),
    )
    return cursor.lastrowid, display_name


def _ensure_canonical_floor(conn, building_id, name, sort_order=0):
    display_name = _canonical_location_label(name, "Unspecified Floor")
    normalized_name = display_name.casefold()
    row = conn.execute(
        "SELECT id, name FROM floors WHERE building_id = ? AND normalized_name = ?",
        (building_id, normalized_name),
    ).fetchone()
    if row:
        return row["id"], row["name"]
    cursor = conn.execute(
        """
        INSERT INTO floors
            (building_id, name, normalized_name, sort_order, is_active)
        VALUES (?, ?, ?, ?, 1)
        """,
        (building_id, display_name, normalized_name, sort_order),
    )
    return cursor.lastrowid, display_name


def migration_018_canonical_location_hierarchy(conn):
    """Normalize Building -> Floor -> Room and enforce one active camera per Room."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS buildings (
            id              INTEGER  PRIMARY KEY AUTOINCREMENT,
            name            TEXT     NOT NULL,
            normalized_name TEXT     NOT NULL UNIQUE,
            is_active       BOOLEAN  NOT NULL DEFAULT 1,
            created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS floors (
            id              INTEGER  PRIMARY KEY AUTOINCREMENT,
            building_id     INTEGER  NOT NULL,
            name            TEXT     NOT NULL,
            normalized_name TEXT     NOT NULL,
            sort_order      INTEGER  NOT NULL DEFAULT 0,
            is_active       BOOLEAN  NOT NULL DEFAULT 1,
            created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (building_id) REFERENCES buildings(id) ON DELETE RESTRICT,
            UNIQUE (building_id, normalized_name)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_floors_building_order
        ON floors(building_id, sort_order, normalized_name)
        """
    )

    if _table_exists(conn, "rooms"):
        if not _column_exists(conn, "rooms", "floor_id"):
            conn.execute("ALTER TABLE rooms ADD COLUMN floor_id INTEGER REFERENCES floors(id)")

        rooms = conn.execute(
            "SELECT id, building, floor FROM rooms ORDER BY id"
        ).fetchall()
        ordered_rooms = sorted(
            rooms,
            key=lambda room: (
                _normalized_location_name(room["building"], "Unspecified Building"),
                _normalized_location_name(room["floor"], "Unspecified Floor"),
                room["id"],
            ),
        )
        floor_positions = {}
        for room in ordered_rooms:
            building_id, building_name = _ensure_canonical_building(conn, room["building"])
            floor_key = (
                building_id,
                _normalized_location_name(room["floor"], "Unspecified Floor"),
            )
            if floor_key not in floor_positions:
                floor_positions[floor_key] = 10 * (
                    1 + sum(1 for key in floor_positions if key[0] == building_id)
                )
            floor_id, floor_name = _ensure_canonical_floor(
                conn,
                building_id,
                room["floor"],
                floor_positions[floor_key],
            )
            conn.execute(
                """
                UPDATE rooms
                SET floor_id = ?, building = ?, floor = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (floor_id, building_name, floor_name, room["id"]),
            )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_rooms_floor_active
            ON rooms(floor_id, is_active, room_code)
            """
        )
        duplicate_codes = conn.execute(
            """
            SELECT LOWER(TRIM(room_code)) AS normalized_code
            FROM rooms
            GROUP BY LOWER(TRIM(room_code))
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for duplicate in duplicate_codes:
            matches = conn.execute(
                """
                SELECT id, room_code
                FROM rooms
                WHERE LOWER(TRIM(room_code)) = ?
                ORDER BY id
                """,
                (duplicate["normalized_code"],),
            ).fetchall()
            for room in matches[1:]:
                base_code = _canonical_location_label(room["room_code"], "ROOM")
                candidate = f"{base_code}-{room['id']}"
                suffix = 1
                while conn.execute(
                    "SELECT 1 FROM rooms WHERE LOWER(TRIM(room_code)) = LOWER(TRIM(?)) AND id != ?",
                    (candidate, room["id"]),
                ).fetchone():
                    candidate = f"{base_code}-{room['id']}-{suffix}"
                    suffix += 1
                conn.execute(
                    "UPDATE rooms SET room_code = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (candidate, room["id"]),
                )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rooms_code_normalized
            ON rooms(LOWER(TRIM(room_code)))
            """
        )
        conn.execute("DROP TRIGGER IF EXISTS trg_rooms_floor_required_insert")
        conn.execute("DROP TRIGGER IF EXISTS trg_rooms_floor_required_update")
        conn.execute(
            """
            CREATE TRIGGER trg_rooms_floor_required_insert
            BEFORE INSERT ON rooms
            WHEN NEW.floor_id IS NULL
            BEGIN
                SELECT RAISE(ABORT, 'rooms.floor_id is required');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER trg_rooms_floor_required_update
            BEFORE UPDATE OF floor_id ON rooms
            WHEN NEW.floor_id IS NULL
            BEGIN
                SELECT RAISE(ABORT, 'rooms.floor_id is required');
            END
            """
        )

    if _table_exists(conn, "camera_devices"):
        conn.execute("DROP INDEX IF EXISTS idx_camera_devices_one_active_per_room")
        conn.execute(
            "UPDATE camera_devices SET is_default = 0 WHERE room_id IS NOT NULL AND is_default = 1"
        )
        duplicate_rooms = conn.execute(
            """
            SELECT room_id
            FROM camera_devices
            WHERE room_id IS NOT NULL AND is_active = 1
            GROUP BY room_id
            HAVING COUNT(*) > 1
            ORDER BY room_id
            """
        ).fetchall()
        for duplicate in duplicate_rooms:
            room_id = duplicate["room_id"]
            cameras = conn.execute(
                """
                SELECT cd.id,
                       (SELECT COUNT(*) FROM classes c
                        WHERE c.camera_id = cd.id AND c.room_id = cd.room_id) AS class_refs,
                       (SELECT COUNT(*) FROM attendance_sessions ats
                        WHERE ats.camera_id = cd.id) AS session_refs
                FROM camera_devices cd
                WHERE cd.room_id = ? AND cd.is_active = 1
                ORDER BY class_refs DESC, session_refs DESC, cd.id ASC
                """,
                (room_id,),
            ).fetchall()
            for camera in cameras[1:]:
                conn.execute(
                    """
                    UPDATE camera_devices
                    SET is_active = 0, is_default = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (camera["id"],),
                )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_camera_devices_one_active_per_room
            ON camera_devices(room_id)
            WHERE room_id IS NOT NULL AND is_active = 1
            """
        )

    if _table_exists(conn, "classes") and _table_exists(conn, "camera_devices"):
        conn.execute(
            """
            UPDATE classes
            SET room_id = (
                SELECT cd.room_id FROM camera_devices cd WHERE cd.id = classes.camera_id
            )
            WHERE room_id IS NULL
              AND camera_id IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM camera_devices cd
                  WHERE cd.id = classes.camera_id AND cd.room_id IS NOT NULL
              )
            """
        )
        conn.execute(
            """
            UPDATE classes
            SET camera_id = (
                SELECT cd.id
                FROM camera_devices cd
                WHERE cd.room_id = classes.room_id AND cd.is_active = 1
                ORDER BY cd.id ASC
                LIMIT 1
            )
            WHERE room_id IS NOT NULL
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_classes_room ON classes(room_id)"
        )


def migration_019_canonical_class_identity_indexes(conn):
    """Make per-Teacher class identity canonical and index conflict scans."""
    if not _table_exists(conn, "classes"):
        return

    duplicates = conn.execute(
        """
        SELECT teacher_id, LOWER(TRIM(class_code)) AS normalized_code
        FROM classes
        GROUP BY teacher_id, LOWER(TRIM(class_code))
        HAVING COUNT(*) > 1
        ORDER BY teacher_id, normalized_code
        """
    ).fetchall()
    for duplicate in duplicates:
        rows = conn.execute(
            """
            SELECT id, class_code
            FROM classes
            WHERE teacher_id = ? AND LOWER(TRIM(class_code)) = ?
            ORDER BY id
            """,
            (duplicate["teacher_id"], duplicate["normalized_code"]),
        ).fetchall()
        for row in rows[1:]:
            base_code = str(row["class_code"] or "CLASS").strip() or "CLASS"
            candidate = f"{base_code}-{row['id']}"
            suffix = 1
            while conn.execute(
                """
                SELECT 1 FROM classes
                WHERE teacher_id = ?
                  AND LOWER(TRIM(class_code)) = LOWER(TRIM(?))
                  AND id != ?
                """,
                (duplicate["teacher_id"], candidate, row["id"]),
            ).fetchone():
                candidate = f"{base_code}-{row['id']}-{suffix}"
                suffix += 1
            conn.execute(
                "UPDATE classes SET class_code = ? WHERE id = ?",
                (candidate, row["id"]),
            )

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_classes_teacher_code_normalized
        ON classes(teacher_id, LOWER(TRIM(class_code)))
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_classes_schedule_resources
        ON classes(is_archived, room_id, teacher_id, start_time, end_time)
        """
    )


def migration_020_canonical_attendance_occurrence(conn):
    """Assign one non-destructive canonical row to each class occurrence."""
    if not _table_exists(conn, "attendance_sessions"):
        return
    if not _column_exists(conn, "attendance_sessions", "occurrence_key"):
        conn.execute("ALTER TABLE attendance_sessions ADD COLUMN occurrence_key TEXT")

    rows = conn.execute(
        """
        SELECT id, class_id, scheduled_date, scheduled_start_time
        FROM attendance_sessions
        ORDER BY id DESC
        """
    ).fetchall()
    claimed = set()
    for row in rows:
        parts = str(row["scheduled_start_time"] or "00:00:00").split(":")
        hour = int(parts[0] or 0)
        minute = int(parts[1] or 0) if len(parts) > 1 else 0
        second = int(parts[2] or 0) if len(parts) > 2 else 0
        occurrence_key = f"{row['scheduled_date']}T{hour:02d}:{minute:02d}:{second:02d}"
        identity = (row["class_id"], occurrence_key)
        if identity in claimed:
            # Preserve the older legacy row and all of its references. Only
            # the newest row becomes the canonical resumable occurrence.
            conn.execute(
                "UPDATE attendance_sessions SET occurrence_key = NULL WHERE id = ?",
                (row["id"],),
            )
            continue
        claimed.add(identity)
        conn.execute(
            "UPDATE attendance_sessions SET occurrence_key = ? WHERE id = ?",
            (occurrence_key, row["id"]),
        )

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_sessions_canonical_occurrence
        ON attendance_sessions(class_id, occurrence_key)
        WHERE occurrence_key IS NOT NULL
        """
    )


def migration_021_optional_liveness_policy(conn):
    """Persist whether blink verification is required; default to advisory/off."""
    if not _table_exists(conn, "recognition_settings"):
        migration_015_recognition_settings(conn)
    if not _column_exists(conn, "recognition_settings", "require_liveness"):
        conn.execute(
            """ALTER TABLE recognition_settings
               ADD COLUMN require_liveness INTEGER NOT NULL DEFAULT 0
               CHECK(require_liveness IN (0, 1))"""
        )


def migration_022_occurrence_bound_attendance_display(conn):
    """Create revocable per-occurrence Attendance Display grants."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance_display_grants (
            id                    INTEGER  PRIMARY KEY AUTOINCREMENT,
            attendance_session_id INTEGER  NOT NULL,
            grant_hash            TEXT     NOT NULL UNIQUE,
            enabled_by_user_id    INTEGER  NOT NULL,
            enabled_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            revoked_at            DATETIME,
            revoked_by_user_id    INTEGER,
            revoke_reason         TEXT     NOT NULL DEFAULT '',
            FOREIGN KEY (attendance_session_id) REFERENCES attendance_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (enabled_by_user_id) REFERENCES users(id) ON DELETE RESTRICT,
            FOREIGN KEY (revoked_by_user_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_display_one_active_grant
        ON attendance_display_grants(attendance_session_id)
        WHERE revoked_at IS NULL
        """
    )


def migration_023_historical_roster_integrity(conn):
    """Anchor class history and preserve occurrence-time roster membership."""
    institution_offset = int(config.INSTITUTION_UTC_OFFSET_MINUTES)
    sqlite_offset = f"{institution_offset:+d} minutes"
    if not _column_exists(conn, "classes", "effective_start_date"):
        conn.execute("ALTER TABLE classes ADD COLUMN effective_start_date DATE")
    conn.execute(
        """
        UPDATE classes
        SET effective_start_date = COALESCE(
            (SELECT MIN(ats.scheduled_date)
             FROM attendance_sessions ats
             WHERE ats.class_id = classes.id),
            (SELECT DATE(MIN(ce.enrolled_at), ?)
             FROM class_enrollments ce
             WHERE ce.class_id = classes.id),
            DATE('now', ?)
        )
        WHERE effective_start_date IS NULL OR TRIM(effective_start_date) = ''
        """,
        (sqlite_offset, sqlite_offset),
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_classes_effective_start_insert")
    conn.execute("DROP TRIGGER IF EXISTS trg_classes_effective_start_update")
    conn.execute(
        f"""
        CREATE TRIGGER trg_classes_effective_start_insert
        AFTER INSERT ON classes
        WHEN NEW.effective_start_date IS NULL OR TRIM(NEW.effective_start_date) = ''
        BEGIN
            UPDATE classes
            SET effective_start_date = DATE('now', '{sqlite_offset}')
            WHERE id = NEW.id;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER trg_classes_effective_start_update
        AFTER UPDATE OF effective_start_date ON classes
        WHEN NEW.effective_start_date IS NULL OR TRIM(NEW.effective_start_date) = ''
        BEGIN
            UPDATE classes
            SET effective_start_date = COALESCE(OLD.effective_start_date, DATE('now', '{sqlite_offset}'))
            WHERE id = NEW.id;
        END
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS class_enrollment_periods (
            id            INTEGER   PRIMARY KEY AUTOINCREMENT,
            class_id      INTEGER   NOT NULL,
            student_id    INTEGER   NOT NULL,
            enrolled_at   TIMESTAMP NOT NULL,
            unenrolled_at TIMESTAMP,
            FOREIGN KEY (class_id)   REFERENCES classes(id)  ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE RESTRICT,
            CHECK(unenrolled_at IS NULL OR unenrolled_at >= enrolled_at)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO class_enrollment_periods
            (class_id, student_id, enrolled_at, unenrolled_at)
        SELECT ce.class_id, ce.student_id, ce.enrolled_at, NULL
        FROM class_enrollments ce
        WHERE NOT EXISTS (
            SELECT 1 FROM class_enrollment_periods cep
            WHERE cep.class_id = ce.class_id
              AND cep.student_id = ce.student_id
              AND cep.unenrolled_at IS NULL
        )
        """
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_class_enrollment_period_active
           ON class_enrollment_periods(class_id, student_id)
           WHERE unenrolled_at IS NULL"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_class_enrollment_period_occurrence
           ON class_enrollment_periods(class_id, enrolled_at, unenrolled_at, student_id)"""
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_class_enrollment_period_insert")
    conn.execute("DROP TRIGGER IF EXISTS trg_class_enrollment_period_delete")
    conn.execute(
        """
        CREATE TRIGGER trg_class_enrollment_period_insert
        AFTER INSERT ON class_enrollments
        BEGIN
            INSERT INTO class_enrollment_periods
                (class_id, student_id, enrolled_at, unenrolled_at)
            VALUES (NEW.class_id, NEW.student_id, NEW.enrolled_at, NULL);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER trg_class_enrollment_period_delete
        BEFORE DELETE ON class_enrollments
        BEGIN
            UPDATE class_enrollment_periods
            SET unenrolled_at = CURRENT_TIMESTAMP
            WHERE class_id = OLD.class_id
              AND student_id = OLD.student_id
              AND unenrolled_at IS NULL;
        END
        """
    )

    if not _column_exists(conn, "attendance_sessions", "roster_anchor_at"):
        conn.execute("ALTER TABLE attendance_sessions ADD COLUMN roster_anchor_at DATETIME")
    occurrence_utc_offset = f"{-institution_offset:+d} minutes"
    conn.execute(
        """
        UPDATE attendance_sessions
        SET roster_anchor_at = DATETIME(
            scheduled_date || ' ' || scheduled_start_time,
            ?
        )
        WHERE roster_anchor_at IS NULL
        """,
        (occurrence_utc_offset,),
    )

    # Missed/Cancelled occurrences are real durable rows without an actual
    # start. Rebuild only when the legacy NOT NULL constraint is present.
    session_columns = {
        row["name"]: row for row in conn.execute("PRAGMA table_info(attendance_sessions)")
    }
    actual_started = session_columns.get("actual_started_at")
    if actual_started and actual_started["notnull"]:
        conn.execute(
            """
            CREATE TABLE attendance_sessions_remediation (
                id                   INTEGER  PRIMARY KEY AUTOINCREMENT,
                class_id             INTEGER  NOT NULL,
                teacher_id           INTEGER,
                camera_id            INTEGER,
                scheduled_date       DATE     NOT NULL,
                scheduled_start_time TIME     NOT NULL,
                occurrence_key       TEXT,
                roster_anchor_at     DATETIME,
                actual_started_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                actual_ended_at      DATETIME,
                status               TEXT     NOT NULL DEFAULT 'Open'
                                             CHECK(status IN ('Open', 'Closed', 'Cancelled', 'Missed')),
                close_reason         TEXT     NOT NULL DEFAULT '',
                created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (class_id)   REFERENCES classes(id) ON DELETE CASCADE,
                FOREIGN KEY (teacher_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY (camera_id)  REFERENCES camera_devices(id) ON DELETE SET NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO attendance_sessions_remediation
                (id, class_id, teacher_id, camera_id, scheduled_date,
                 scheduled_start_time, occurrence_key, roster_anchor_at, actual_started_at,
                 actual_ended_at, status, close_reason, created_at)
            SELECT id, class_id, teacher_id, camera_id, scheduled_date,
                   scheduled_start_time, occurrence_key, roster_anchor_at, actual_started_at,
                   actual_ended_at, status, close_reason, created_at
            FROM attendance_sessions
            """
        )
        conn.execute("DROP TABLE attendance_sessions")
        conn.execute("ALTER TABLE attendance_sessions_remediation RENAME TO attendance_sessions")
        conn.execute(
            """CREATE INDEX idx_attendance_sessions_class_date
               ON attendance_sessions(class_id, scheduled_date, status)"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX idx_attendance_sessions_canonical_occurrence
               ON attendance_sessions(class_id, occurrence_key)
               WHERE occurrence_key IS NOT NULL"""
        )


def migration_024_lifecycle_database_guards(conn):
    """Enforce biometric eligibility and administrative continuity."""
    conn.execute("DROP TRIGGER IF EXISTS trg_embeddings_require_eligible_student_insert")
    conn.execute("DROP TRIGGER IF EXISTS trg_embeddings_require_eligible_student_update")
    conn.execute(
        """
        CREATE TRIGGER trg_embeddings_require_eligible_student_insert
        BEFORE INSERT ON embeddings
        WHEN NOT EXISTS (
            SELECT 1 FROM students s
            WHERE s.id = NEW.student_id
              AND s.is_active = 1
              AND s.consent_granted = 1
              AND s.permanently_removed_at IS NULL
        )
        BEGIN
            SELECT RAISE(ABORT, 'biometric enrollment requires an active consenting student');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER trg_embeddings_require_eligible_student_update
        BEFORE UPDATE ON embeddings
        WHEN NOT EXISTS (
            SELECT 1 FROM students s
            WHERE s.id = NEW.student_id
              AND s.is_active = 1
              AND s.consent_granted = 1
              AND s.permanently_removed_at IS NULL
        )
        BEGIN
            SELECT RAISE(ABORT, 'biometric enrollment requires an active consenting student');
        END
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_users_preserve_active_dean")
    # Cleanup for pre-release remediation builds; deleting users is not an
    # application operation, and disposable reset workflows must remain usable.
    conn.execute("DROP TRIGGER IF EXISTS trg_users_preserve_active_dean_delete")
    conn.execute(
        """
        CREATE TRIGGER trg_users_preserve_active_dean
        BEFORE UPDATE OF role, is_active ON users
        WHEN OLD.role = 'Dean'
         AND OLD.is_active = 1
         AND (NEW.role != 'Dean' OR NEW.is_active != 1)
         AND NOT EXISTS (
             SELECT 1 FROM users u
             WHERE u.id != OLD.id AND u.role = 'Dean' AND u.is_active = 1
         )
        BEGIN
            SELECT RAISE(ABORT, 'at least one active Dean is required');
        END
        """
    )


def _rebuild_classes_for_effective_start_parity(conn):
    """Give upgraded databases the same classes semantics as a fresh schema."""
    columns = {
        row["name"]: row for row in conn.execute("PRAGMA table_info(classes)")
    }
    effective = columns.get("effective_start_date")
    has_parity = bool(
        effective
        and effective["notnull"] == 1
        and effective["dflt_value"]
        and "DATE('now')" in str(effective["dflt_value"])
    )
    if has_parity:
        return

    conn.execute("DROP TRIGGER IF EXISTS trg_classes_effective_start_insert")
    conn.execute("DROP TRIGGER IF EXISTS trg_classes_effective_start_update")
    conn.execute("DROP TABLE IF EXISTS classes_remediation_025")
    conn.execute(
        """
        CREATE TABLE classes_remediation_025 (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id               INTEGER NOT NULL,
            camera_id                INTEGER,
            room_id                  INTEGER,
            course_name              TEXT    NOT NULL,
            class_code               TEXT    NOT NULL,
            section                  TEXT    NOT NULL DEFAULT 'Section A',
            days_of_week             TEXT    NOT NULL,
            start_time               TIME    NOT NULL,
            end_time                 TIME    NOT NULL,
            late_threshold_minutes   INTEGER NOT NULL DEFAULT 15,
            is_active                BOOLEAN NOT NULL DEFAULT 0,
            is_archived              BOOLEAN NOT NULL DEFAULT 0,
            effective_start_date     DATE    NOT NULL DEFAULT (DATE('now')),
            FOREIGN KEY (room_id)    REFERENCES rooms(id) ON DELETE SET NULL,
            FOREIGN KEY (camera_id)  REFERENCES camera_devices(id),
            FOREIGN KEY (teacher_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE (teacher_id, class_code)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO classes_remediation_025
            (id, teacher_id, camera_id, room_id, course_name, class_code,
             section, days_of_week, start_time, end_time,
             late_threshold_minutes, is_active, is_archived,
             effective_start_date)
        SELECT id, teacher_id, camera_id, room_id, course_name, class_code,
               section, days_of_week, start_time, end_time,
               late_threshold_minutes, is_active, is_archived,
               COALESCE(NULLIF(TRIM(effective_start_date), ''), DATE('now'))
        FROM classes
        """
    )
    conn.execute("DROP TABLE classes")
    conn.execute("ALTER TABLE classes_remediation_025 RENAME TO classes")


def migration_025_targeted_remediation_parity(conn):
    """Complete occurrence, roster, archive-adjacent, and schema invariants."""
    if not _table_exists(conn, "classes"):
        return

    _rebuild_classes_for_effective_start_parity(conn)
    # The rebuilt/fresh NOT NULL column and default are authoritative; the
    # compatibility triggers from 023 are no longer needed and would leave
    # fresh-vs-upgraded trigger sets inconsistent.
    conn.execute("DROP TRIGGER IF EXISTS trg_classes_effective_start_insert")
    conn.execute("DROP TRIGGER IF EXISTS trg_classes_effective_start_update")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_classes_teacher_code
           ON classes(teacher_id, class_code)"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_classes_teacher_code_normalized
           ON classes(teacher_id, LOWER(TRIM(class_code)))"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_classes_schedule_resources
           ON classes(is_archived, room_id, teacher_id, start_time, end_time)"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_classes_camera ON classes(camera_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_classes_room ON classes(room_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS class_schedule_periods (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id        INTEGER NOT NULL,
            effective_from  DATE    NOT NULL,
            effective_until DATE,
            days_of_week    TEXT    NOT NULL,
            start_time      TIME    NOT NULL,
            end_time        TIME    NOT NULL,
            created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE,
            UNIQUE(class_id, effective_from),
            CHECK(effective_until IS NULL OR effective_until > effective_from)
        )
        """
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_class_schedule_period_resolution
           ON class_schedule_periods(class_id, effective_from, effective_until)"""
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO class_schedule_periods
            (class_id, effective_from, effective_until, days_of_week,
             start_time, end_time)
        SELECT id, effective_start_date, NULL, days_of_week, start_time, end_time
        FROM classes
        """
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_class_schedule_period_insert")
    conn.execute(
        """
        CREATE TRIGGER trg_class_schedule_period_insert
        AFTER INSERT ON classes
        BEGIN
            INSERT INTO class_schedule_periods
                (class_id, effective_from, effective_until, days_of_week,
                 start_time, end_time)
            VALUES (
                NEW.id, NEW.effective_start_date, NULL, NEW.days_of_week,
                NEW.start_time, NEW.end_time
            );
        END
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lifecycle_event_sequence (
            singleton  INTEGER PRIMARY KEY CHECK(singleton = 1),
            last_value INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO lifecycle_event_sequence(singleton, last_value) VALUES(1, 0)"
    )
    if _table_exists(conn, "class_enrollment_periods"):
        if not _column_exists(conn, "class_enrollment_periods", "enrolled_order"):
            conn.execute("ALTER TABLE class_enrollment_periods ADD COLUMN enrolled_order INTEGER")
        if not _column_exists(conn, "class_enrollment_periods", "unenrolled_order"):
            conn.execute("ALTER TABLE class_enrollment_periods ADD COLUMN unenrolled_order INTEGER")

        conn.execute("DROP TRIGGER IF EXISTS trg_class_enrollment_period_insert")
        conn.execute("DROP TRIGGER IF EXISTS trg_class_enrollment_period_delete")
        conn.execute(
            """
            CREATE TRIGGER trg_class_enrollment_period_insert
            AFTER INSERT ON class_enrollments
            BEGIN
                UPDATE lifecycle_event_sequence
                SET last_value = last_value + 1
                WHERE singleton = 1;
                INSERT INTO class_enrollment_periods
                    (class_id, student_id, enrolled_at, unenrolled_at,
                     enrolled_order, unenrolled_order)
                VALUES (
                    NEW.class_id, NEW.student_id, NEW.enrolled_at, NULL,
                    (SELECT last_value FROM lifecycle_event_sequence WHERE singleton = 1),
                    NULL
                );
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER trg_class_enrollment_period_delete
            BEFORE DELETE ON class_enrollments
            BEGIN
                UPDATE lifecycle_event_sequence
                SET last_value = last_value + 1
                WHERE singleton = 1;
                UPDATE class_enrollment_periods
                SET unenrolled_at = STRFTIME('%Y-%m-%d %H:%M:%f', 'now'),
                    unenrolled_order = (
                        SELECT last_value FROM lifecycle_event_sequence WHERE singleton = 1
                    )
                WHERE class_id = OLD.class_id
                  AND student_id = OLD.student_id
                  AND unenrolled_at IS NULL;
            END
            """
        )

    if _table_exists(conn, "attendance_sessions"):
        if not _column_exists(conn, "attendance_sessions", "scheduled_end_time"):
            conn.execute("ALTER TABLE attendance_sessions ADD COLUMN scheduled_end_time TIME")
        if not _column_exists(conn, "attendance_sessions", "roster_anchor_order"):
            conn.execute("ALTER TABLE attendance_sessions ADD COLUMN roster_anchor_order INTEGER")
        conn.execute(
            """
            UPDATE attendance_sessions
            SET scheduled_end_time = COALESCE(
                scheduled_end_time,
                (SELECT c.end_time FROM classes c WHERE c.id = attendance_sessions.class_id)
            )
            WHERE scheduled_end_time IS NULL
            """
        )

        duplicate_dates = conn.execute(
            """
            SELECT class_id, scheduled_date
            FROM attendance_sessions
            WHERE occurrence_key IS NOT NULL
            GROUP BY class_id, scheduled_date
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for duplicate in duplicate_dates:
            rows = conn.execute(
                """
                SELECT id
                FROM attendance_sessions
                WHERE class_id = ? AND scheduled_date = ?
                  AND occurrence_key IS NOT NULL
                ORDER BY CASE status
                    WHEN 'Closed' THEN 0
                    WHEN 'Open' THEN 1
                    WHEN 'Cancelled' THEN 2
                    ELSE 3
                END,
                id
                """,
                (duplicate["class_id"], duplicate["scheduled_date"]),
            ).fetchall()
            for legacy_row in rows[1:]:
                conn.execute(
                    "UPDATE attendance_sessions SET occurrence_key = NULL WHERE id = ?",
                    (legacy_row["id"],),
                )

        conn.execute("DROP TRIGGER IF EXISTS trg_attendance_session_assign_order")
        conn.execute("DROP TRIGGER IF EXISTS trg_attendance_occurrence_date_insert")
        conn.execute("DROP TRIGGER IF EXISTS trg_attendance_occurrence_date_update")
        conn.execute(
            """
            CREATE TRIGGER trg_attendance_session_assign_order
            AFTER INSERT ON attendance_sessions
            WHEN NEW.roster_anchor_order IS NULL
            BEGIN
                UPDATE lifecycle_event_sequence
                SET last_value = last_value + 1
                WHERE singleton = 1;
                UPDATE attendance_sessions
                SET roster_anchor_order = (
                        SELECT last_value FROM lifecycle_event_sequence WHERE singleton = 1
                    )
                WHERE id = NEW.id;
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER trg_attendance_occurrence_date_insert
            BEFORE INSERT ON attendance_sessions
            WHEN NEW.occurrence_key IS NOT NULL
             AND EXISTS (
                 SELECT 1 FROM attendance_sessions ats
                 WHERE ats.class_id = NEW.class_id
                   AND ats.scheduled_date = NEW.scheduled_date
                   AND ats.occurrence_key IS NOT NULL
             )
            BEGIN
                SELECT RAISE(ABORT, 'attendance occurrence already exists for class date');
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER trg_attendance_occurrence_date_update
            BEFORE UPDATE OF class_id, scheduled_date, occurrence_key ON attendance_sessions
            WHEN NEW.occurrence_key IS NOT NULL
             AND EXISTS (
                 SELECT 1 FROM attendance_sessions ats
                 WHERE ats.id != OLD.id
                   AND ats.class_id = NEW.class_id
                   AND ats.scheduled_date = NEW.scheduled_date
                   AND ats.occurrence_key IS NOT NULL
             )
            BEGIN
                SELECT RAISE(ABORT, 'attendance occurrence already exists for class date');
            END
            """
        )

    if _table_exists(conn, "students") and _table_exists(conn, "embeddings"):
        conn.execute("DROP TRIGGER IF EXISTS trg_students_purge_ineligible_embeddings")
        conn.execute(
            """
            CREATE TRIGGER trg_students_purge_ineligible_embeddings
            AFTER UPDATE OF is_active, consent_granted, permanently_removed_at ON students
            WHEN NEW.is_active != 1
              OR NEW.consent_granted != 1
              OR NEW.permanently_removed_at IS NOT NULL
            BEGIN
                DELETE FROM embeddings WHERE student_id = NEW.id;
            END
            """
        )
        conn.execute(
            """
            DELETE FROM embeddings
            WHERE student_id IN (
                SELECT id FROM students
                WHERE is_active != 1
                   OR consent_granted != 1
                   OR permanently_removed_at IS NOT NULL
            )
            """
        )

    if _table_exists(conn, "students") and _table_exists(conn, "class_enrollments"):
        conn.execute("DROP TRIGGER IF EXISTS trg_students_close_ineligible_enrollments")
        conn.execute(
            """
            CREATE TRIGGER trg_students_close_ineligible_enrollments
            AFTER UPDATE OF is_active, permanently_removed_at ON students
            WHEN NEW.is_active != 1 OR NEW.permanently_removed_at IS NOT NULL
            BEGIN
                DELETE FROM class_enrollments WHERE student_id = NEW.id;
            END
            """
        )
        conn.execute(
            """
            DELETE FROM class_enrollments
            WHERE student_id IN (
                SELECT id FROM students
                WHERE is_active != 1 OR permanently_removed_at IS NOT NULL
            )
            """
        )
def migration_026_session_lifecycle(conn):
    from services.session_lifecycle import install_schema
    install_schema(conn)


def migration_027_session_start(conn):
    from services.session_start import install_schema
    install_schema(conn)


def migration_028_camera_capacity(conn):
    from services.camera_capacity import install_schema
    install_schema(conn)


def migration_029_attendance_opening(conn):
    from services.attendance_settings import install_schema
    install_schema(conn)


def migration_030_login_notice(conn):
    from security.login_notice import install_schema
    install_schema(conn)


def migration_031_camera_reopening(conn):
    from services.session_reopening import install_schema
    install_schema(conn)


def migration_032_early_camera_resume(conn):
    from services.session_reopening import install_resume_schema
    install_resume_schema(conn)


def migration_033_attendance_operation_kind(conn):
    # Old notes cannot prove whether a Pending -> Present mark was a live entry.
    # Leave historical metadata unknown; never reconstruct it from free text.
    if _table_exists(conn, 'attendance_change_audit') and not _column_exists(conn, 'attendance_change_audit', 'operation_kind'):
        conn.execute("ALTER TABLE attendance_change_audit ADD COLUMN operation_kind TEXT CHECK(operation_kind IN ('initial_manual', 'correction'))")


MIGRATIONS = [
    ("001", "production_hardening", migration_001_production_hardening),
    ("002", "teacher_registration_approvals", migration_002_teacher_registration_approvals),
    ("003", "attendance_sessions", migration_003_attendance_sessions),
    ("004", "room_camera_coverage", migration_004_room_camera_coverage),
    ("005", "user_lockout_fields", migration_005_user_lockout_fields),
    ("006", "academic_programs", migration_006_academic_programs),
    ("007", "section_column", migration_007_section_column),
    ("008", "class_crud_archive", migration_008_class_crud_archive),
    ("009", "biometric_encryption", migration_009_biometric_encryption),
    ("010", "student_is_active", migration_010_student_is_active),
    ("011", "drop_room_number", migration_011_drop_room_number),
    ("012", "nullable_camera_id", migration_012_nullable_camera_id),
    ("013", "exclusive_cctv", migration_013_exclusive_cctv),
    ("014", "camera_performance", migration_014_camera_performance),
    ("015", "recognition_settings", migration_015_recognition_settings),
    ("016", "restore_supported_camera_types", migration_016_restore_supported_camera_types),
    ("017", "student_permanent_removal_tombstone", migration_017_student_permanent_removal_tombstone),
    ("018", "canonical_location_hierarchy", migration_018_canonical_location_hierarchy),
    ("019", "canonical_class_identity_indexes", migration_019_canonical_class_identity_indexes),
    ("020", "canonical_attendance_occurrence", migration_020_canonical_attendance_occurrence),
    ("021", "optional_liveness_policy", migration_021_optional_liveness_policy),
    ("022", "occurrence_bound_attendance_display", migration_022_occurrence_bound_attendance_display),
    ("023", "historical_roster_integrity", migration_023_historical_roster_integrity),
    ("024", "lifecycle_database_guards", migration_024_lifecycle_database_guards),
    ("025", "targeted_remediation_parity", migration_025_targeted_remediation_parity),
    ("026", "terminal_session_lifecycle", migration_026_session_lifecycle),
    ("027", "reviewed_session_start", migration_027_session_start),
    ("028", "provisional_camera_capacity", migration_028_camera_capacity),
    ("029", "early_attendance_opening", migration_029_attendance_opening),
    ("030", "login_notice_acknowledgments", migration_030_login_notice),
    ("031", "audited_camera_reopening", migration_031_camera_reopening),
    ("032", "audited_early_camera_resume", migration_032_early_camera_resume),
    ("033", "attendance_operation_kind", migration_033_attendance_operation_kind),
]



def run_migrations():
    conn = get_db_connection()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        _ensure_schema_migrations(conn)
        for version, name, migration in MIGRATIONS:
            if _is_applied(conn, version):
                conn.execute("UPDATE schema_migrations SET name = ? WHERE version = ?", (name, version))
                logger.info("Migration %s already applied.", version)
                continue
            logger.info("Applying migration %s: %s", version, name)
            migration(conn)
            _mark_applied(conn, version, name)
            conn.commit()
            logger.info("Migration %s applied.", version)
    except Exception:
        conn.rollback()
        logger.exception("Migration failed.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run_migrations()
