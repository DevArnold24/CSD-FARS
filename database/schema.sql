-- ============================================================================
-- CSD FARS -- Database Schema
-- Target: SQLite 3
-- ============================================================================

-- Enable Write-Ahead Logging for concurrent read/write access.
PRAGMA journal_mode=WAL;

-- ============================================================================
-- USERS
-- Handles authentication and Role-Based Access Control (RBAC).
-- ============================================================================
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id                 TEXT    NOT NULL UNIQUE,
    username                 TEXT    NOT NULL UNIQUE,
    password_hash            TEXT    NOT NULL,
    role                     TEXT    NOT NULL CHECK(role IN ('Dean', 'Teacher', 'IT_Support', 'Office_Admin')),
    requires_password_change BOOLEAN NOT NULL DEFAULT 1,
    is_active                BOOLEAN NOT NULL DEFAULT 1,
    failed_login_attempts    INTEGER DEFAULT 0,
    locked_until             TEXT,
    tuner_preferences        TEXT
);


-- ============================================================================
-- Staff notice acknowledgments contain no passwords or student consent.
CREATE TABLE IF NOT EXISTS login_notice_acknowledgments (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    notice_version TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    remember INTEGER NOT NULL CHECK (remember IN (0, 1)),
    PRIMARY KEY (user_id, notice_version)
);

-- ACADEMIC PROGRAMS
-- Database table to store dynamic academic programs.
-- ============================================================================
CREATE TABLE IF NOT EXISTS academic_programs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT    UNIQUE NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT 1
);

-- ============================================================================
-- STUDENTS
-- Core student profile data with foreign key link to academic program.
-- ============================================================================
CREATE TABLE IF NOT EXISTS students (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id        TEXT    NOT NULL UNIQUE,
    name              TEXT    NOT NULL,
    program_id        INTEGER NOT NULL,
    year_level        TEXT    NOT NULL,
    consent_granted   BOOLEAN NOT NULL DEFAULT 0,
    is_active         BOOLEAN NOT NULL DEFAULT 1,
    permanently_removed_at DATETIME,
    FOREIGN KEY (program_id) REFERENCES academic_programs(id)
);

-- ============================================================================
-- EMBEDDINGS
-- 128-dimensional facial embedding vectors linked to each student.
-- A student may have multiple embeddings (e.g., re-enrollment).
-- Raw face images are never saved to disk.
-- ============================================================================
CREATE TABLE IF NOT EXISTS embeddings (
    id              INTEGER   PRIMARY KEY AUTOINCREMENT,
    student_id      INTEGER   NOT NULL,
    face_vector     BLOB      NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
);

-- ============================================================================
-- INSTITUTIONAL LOCATIONS
-- Buildings and floors are canonical. Rooms retain legacy building/floor text
-- snapshots for compatibility, but floor_id is the authoritative relationship.
-- ============================================================================
CREATE TABLE IF NOT EXISTS buildings (
    id              INTEGER  PRIMARY KEY AUTOINCREMENT,
    name            TEXT     NOT NULL,
    normalized_name TEXT     NOT NULL UNIQUE,
    is_active       BOOLEAN  NOT NULL DEFAULT 1,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

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
);

CREATE INDEX IF NOT EXISTS idx_floors_building_order
    ON floors(building_id, sort_order, normalized_name);

INSERT OR IGNORE INTO buildings
    (id, name, normalized_name, is_active)
VALUES (1, 'Unspecified Building', 'unspecified building', 1);

INSERT OR IGNORE INTO floors
    (id, building_id, name, normalized_name, sort_order, is_active)
VALUES (1, 1, 'Unspecified Floor', 'unspecified floor', 0, 1);

CREATE TABLE IF NOT EXISTS rooms (
    id          INTEGER  PRIMARY KEY AUTOINCREMENT,
    floor_id    INTEGER  NOT NULL DEFAULT 1,
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
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (floor_id) REFERENCES floors(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_rooms_active_code
    ON rooms(is_active, room_code);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rooms_code_normalized
    ON rooms(LOWER(TRIM(room_code)));

CREATE INDEX IF NOT EXISTS idx_rooms_floor_active
    ON rooms(floor_id, is_active, room_code);

-- ============================================================================
-- CAMERA_DEVICES
-- Room-based camera inventory used by live attendance sessions. Classes select
-- Rooms, while Dean/IT Support manage each Room's active attendance CCTV.
-- ============================================================================
CREATE TABLE IF NOT EXISTS camera_devices (
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
    match_threshold   REAL     NOT NULL DEFAULT 0.45,
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
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_camera_devices_single_default
    ON camera_devices(is_default)
    WHERE is_default = 1;

CREATE INDEX IF NOT EXISTS idx_camera_devices_room_active
    ON camera_devices(room_id, is_active);

CREATE UNIQUE INDEX IF NOT EXISTS idx_camera_devices_one_active_per_room
    ON camera_devices(room_id)
    WHERE room_id IS NOT NULL AND is_active = 1;

-- ============================================================================
-- SCHEMA_MIGRATIONS
-- Tracks data-preserving migrations applied to existing databases.
-- ============================================================================
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT     PRIMARY KEY,
    name       TEXT     NOT NULL,
    applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recognition_settings (
    id                          INTEGER PRIMARY KEY CHECK(id = 1),
    match_threshold             REAL    NOT NULL DEFAULT 0.50,
    match_margin                REAL    NOT NULL DEFAULT 0.20,
    match_confirmation_passes   INTEGER NOT NULL DEFAULT 2,
    frame_skip_interval         INTEGER NOT NULL DEFAULT 3,
    downscale_height            INTEGER NOT NULL DEFAULT 720,
    liveness_ttl_seconds        REAL    NOT NULL DEFAULT 10.0,
    require_liveness            INTEGER NOT NULL DEFAULT 0 CHECK(require_liveness IN (0, 1)),
    updated_by_user_id          INTEGER,
    updated_at                  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

INSERT OR IGNORE INTO recognition_settings (id) VALUES (1);

-- ============================================================================
-- CLASSES
-- Class section definitions, scheduling, teacher assignment, and
-- attendance window configuration.
-- ============================================================================
CREATE TABLE IF NOT EXISTS classes (
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
    FOREIGN KEY (room_id)   REFERENCES rooms(id) ON DELETE SET NULL,
    FOREIGN KEY (camera_id)  REFERENCES camera_devices(id),
    FOREIGN KEY (teacher_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE (teacher_id, class_code)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_classes_teacher_code_normalized
    ON classes(teacher_id, LOWER(TRIM(class_code)));

CREATE UNIQUE INDEX IF NOT EXISTS idx_classes_teacher_code
    ON classes(teacher_id, class_code);

CREATE INDEX IF NOT EXISTS idx_classes_schedule_resources
    ON classes(is_archived, room_id, teacher_id, start_time, end_time);

-- Effective-dated recurring schedules ensure later edits do not reinterpret
-- prior unmaterialized or materialized occurrence dates.
CREATE TABLE IF NOT EXISTS class_schedule_periods (
    id              INTEGER  PRIMARY KEY AUTOINCREMENT,
    class_id        INTEGER  NOT NULL,
    effective_from  DATE     NOT NULL,
    effective_until DATE,
    days_of_week    TEXT     NOT NULL,
    start_time      TIME     NOT NULL,
    end_time        TIME     NOT NULL,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE,
    UNIQUE(class_id, effective_from),
    CHECK(effective_until IS NULL OR effective_until > effective_from)
);

CREATE INDEX IF NOT EXISTS idx_class_schedule_period_resolution
    ON class_schedule_periods(class_id, effective_from, effective_until);

CREATE TRIGGER IF NOT EXISTS trg_class_schedule_period_insert
AFTER INSERT ON classes
BEGIN
    INSERT INTO class_schedule_periods
        (class_id, effective_from, effective_until, days_of_week,
         start_time, end_time)
    VALUES (
        NEW.id, NEW.effective_start_date, NULL, NEW.days_of_week,
        NEW.start_time, NEW.end_time
    );
END;

-- ============================================================================
-- CLASS_ENROLLMENTS
-- Junction table: many-to-many between students and classes (the Roster).
-- ============================================================================
CREATE TABLE IF NOT EXISTS class_enrollments (
    id          INTEGER   PRIMARY KEY AUTOINCREMENT,
    class_id    INTEGER   NOT NULL,
    student_id  INTEGER   NOT NULL,
    enrolled_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (class_id)   REFERENCES classes(id)  ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
    UNIQUE (class_id, student_id)
);

-- Durable membership periods preserve which roster applied to each scheduled
-- occurrence even after the current roster is changed.
CREATE TABLE IF NOT EXISTS class_enrollment_periods (
    id            INTEGER   PRIMARY KEY AUTOINCREMENT,
    class_id      INTEGER   NOT NULL,
    student_id    INTEGER   NOT NULL,
    enrolled_at   TIMESTAMP NOT NULL,
    unenrolled_at TIMESTAMP,
    enrolled_order   INTEGER,
    unenrolled_order INTEGER,
    FOREIGN KEY (class_id)   REFERENCES classes(id)  ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE RESTRICT,
    CHECK(unenrolled_at IS NULL OR unenrolled_at >= enrolled_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_class_enrollment_period_active
    ON class_enrollment_periods(class_id, student_id)
    WHERE unenrolled_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_class_enrollment_period_occurrence
    ON class_enrollment_periods(class_id, enrolled_at, unenrolled_at, student_id);

CREATE TABLE IF NOT EXISTS lifecycle_event_sequence (
    singleton  INTEGER PRIMARY KEY CHECK(singleton = 1),
    last_value INTEGER NOT NULL
);

INSERT OR IGNORE INTO lifecycle_event_sequence(singleton, last_value)
VALUES(1, 0);

CREATE TRIGGER IF NOT EXISTS trg_class_enrollment_period_insert
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
END;

CREATE TRIGGER IF NOT EXISTS trg_class_enrollment_period_delete
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
END;

-- ============================================================================
-- ATTENDANCE_SESSIONS
-- Durable class meeting instances. Live camera sessions and historical reports
-- are anchored here instead of relying only on class/date lookups.
-- ============================================================================
CREATE TABLE IF NOT EXISTS attendance_sessions (
    id                   INTEGER  PRIMARY KEY AUTOINCREMENT,
    class_id             INTEGER  NOT NULL,
    teacher_id           INTEGER,
    camera_id            INTEGER,
    scheduled_date       DATE     NOT NULL,
    scheduled_start_time TIME     NOT NULL,
    scheduled_end_time   TIME,
    occurrence_key       TEXT,
    roster_anchor_at     DATETIME,
    roster_anchor_order  INTEGER,
    actual_started_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    actual_ended_at      DATETIME,
    status               TEXT     NOT NULL DEFAULT 'Open'
                                      CHECK(status IN ('Open', 'Closed', 'Cancelled', 'Missed')),
    close_reason         TEXT     NOT NULL DEFAULT '',
    created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (class_id)   REFERENCES classes(id) ON DELETE CASCADE,
    FOREIGN KEY (teacher_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (camera_id)  REFERENCES camera_devices(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_attendance_sessions_class_date
    ON attendance_sessions(class_id, scheduled_date, status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_sessions_canonical_occurrence
    ON attendance_sessions(class_id, occurrence_key)
    WHERE occurrence_key IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_attendance_session_assign_order
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
END;

CREATE TRIGGER IF NOT EXISTS trg_attendance_occurrence_date_insert
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
END;

CREATE TRIGGER IF NOT EXISTS trg_attendance_occurrence_date_update
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
END;

-- ============================================================================
-- ATTENDANCE_DISPLAY_GRANTS
-- High-entropy, occurrence-bound links for unauthenticated LAN displays. Only
-- a hash is stored; revocation history remains auditable.
-- ============================================================================
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
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_display_one_active_grant
    ON attendance_display_grants(attendance_session_id)
    WHERE revoked_at IS NULL;

-- ============================================================================
-- ATTENDANCE_RECORDS
-- Logs one current attendance status per student per session. Absences are
-- materialized when a session closes so historical reports are stable.
-- ============================================================================
CREATE TABLE IF NOT EXISTS attendance_records (
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
);

-- ============================================================================
-- INDEXES
-- Optimise the most frequent query patterns.
-- ============================================================================

-- Fast lookup of attendance by student and class on a given day (dedup check).
CREATE INDEX IF NOT EXISTS idx_attendance_student_class
    ON attendance_records(student_id, class_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_attendance_session_student
    ON attendance_records(attendance_session_id, student_id);

-- Fast embedding retrieval for matching.
CREATE INDEX IF NOT EXISTS idx_embeddings_student
    ON embeddings(student_id);

CREATE TRIGGER IF NOT EXISTS trg_embeddings_require_eligible_student_insert
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
END;

CREATE TRIGGER IF NOT EXISTS trg_embeddings_require_eligible_student_update
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
END;

-- Supported student lifecycle actions purge biometric material. Enforce the
-- same invariant for direct database writes so no ineligible parent row can
-- retain embeddings.
CREATE TRIGGER IF NOT EXISTS trg_students_purge_ineligible_embeddings
AFTER UPDATE OF is_active, consent_granted, permanently_removed_at ON students
WHEN NEW.is_active != 1
  OR NEW.consent_granted != 1
  OR NEW.permanently_removed_at IS NOT NULL
BEGIN
    DELETE FROM embeddings WHERE student_id = NEW.id;
END;

-- Inactive and permanently removed students leave every current roster. The
-- class_enrollments delete trigger closes their historical enrollment period.
CREATE TRIGGER IF NOT EXISTS trg_students_close_ineligible_enrollments
AFTER UPDATE OF is_active, permanently_removed_at ON students
WHEN NEW.is_active != 1 OR NEW.permanently_removed_at IS NOT NULL
BEGIN
    DELETE FROM class_enrollments WHERE student_id = NEW.id;
END;

-- ============================================================================
-- PENDING_STUDENT_REGISTRATIONS
-- Teacher-submitted biometric enrollment requests awaiting Dean or Office Admin approval.
-- Pending requests are not active students and are excluded from rosters,
-- directory listings, and attendance recognition until approved.
-- ============================================================================
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
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_student_unique_pending
    ON pending_student_registrations(student_id)
    WHERE status = 'Pending';

CREATE INDEX IF NOT EXISTS idx_pending_student_status_submitted
    ON pending_student_registrations(status, submitted_at);

-- Fast lookup of classes by assigned camera.
CREATE INDEX IF NOT EXISTS idx_classes_camera
    ON classes(camera_id);

CREATE INDEX IF NOT EXISTS idx_classes_room
    ON classes(room_id);

-- ============================================================================
-- AUDIT_LOGS
-- A security and accountability table that tracks all manual exceptions and overrides.
-- ============================================================================
CREATE TABLE IF NOT EXISTS audit_logs (
    id                   INTEGER  PRIMARY KEY AUTOINCREMENT,
    attendance_record_id INTEGER  NOT NULL,
    teacher_id           INTEGER  NOT NULL,
    previous_status      TEXT     NOT NULL CHECK(previous_status IN ('Absent', 'Present', 'Late', 'Manual_Override')),
    new_status           TEXT     NOT NULL CHECK(new_status IN ('Absent', 'Present', 'Late', 'Manual_Override')),
    reason               TEXT     NOT NULL,
    timestamp            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (attendance_record_id) REFERENCES attendance_records(id) ON DELETE CASCADE,
    FOREIGN KEY (teacher_id) REFERENCES users(id) ON DELETE CASCADE
);

-- ============================================================================
-- ATTENDANCE_CHANGE_AUDIT
-- Session-aware audit log for every accountable attendance status edit.
-- ============================================================================
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
    operation_kind        TEXT CHECK(operation_kind IN ('initial_manual', 'correction')),
    FOREIGN KEY (attendance_session_id) REFERENCES attendance_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY (attendance_record_id)  REFERENCES attendance_records(id)  ON DELETE SET NULL,
    FOREIGN KEY (class_id)              REFERENCES classes(id)             ON DELETE CASCADE,
    FOREIGN KEY (student_id)            REFERENCES students(id)            ON DELETE CASCADE,
    FOREIGN KEY (actor_user_id)         REFERENCES users(id)               ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_attendance_change_audit_session
    ON attendance_change_audit(attendance_session_id, changed_at);

-- ============================================================================
-- SYSTEM_AUDIT_LOGS
-- Durable audit events for backups, migrations, purges, credentials, and
-- operational security actions.
-- ============================================================================
CREATE TABLE IF NOT EXISTS system_audit_logs (
    id            INTEGER  PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER,
    action        TEXT     NOT NULL,
    details       TEXT     NOT NULL DEFAULT '',
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TRIGGER IF NOT EXISTS trg_users_preserve_active_dean
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
END;

CREATE TABLE IF NOT EXISTS attendance_lifecycle (
 session_id INTEGER PRIMARY KEY REFERENCES attendance_sessions(id) ON DELETE CASCADE,
 policy_version INTEGER NOT NULL DEFAULT 2 CHECK(policy_version=2),
 cutoff REAL NOT NULL, late_at REAL NOT NULL, roster_signature TEXT NOT NULL,
 collection TEXT NOT NULL DEFAULT 'collecting', version INTEGER NOT NULL DEFAULT 1,
 end_operation TEXT UNIQUE, end_actor INTEGER, end_reason TEXT, ended_at REAL,
 closed_at REAL, cleanup TEXT NOT NULL,
 episode TEXT, episode_started REAL, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL, exhausted INTEGER NOT NULL DEFAULT 0, in_flight INTEGER NOT NULL DEFAULT 0,
 last_error TEXT NOT NULL DEFAULT '', interruption_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS attendance_lifecycle_events (
 id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
 event TEXT NOT NULL, at REAL NOT NULL, actor INTEGER, detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS attendance_process_owners (
 token TEXT PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, pid INTEGER, created REAL, confirmed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attendance_server_owner (
 id INTEGER PRIMARY KEY CHECK(id=1), pid INTEGER NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_unfinished ON attendance_lifecycle(cutoff)
 WHERE end_operation IS NULL OR closed_at IS NULL OR cleanup='pending';


CREATE TABLE IF NOT EXISTS attendance_start_attempts (
 id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE,
 session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
 mode TEXT NOT NULL CHECK(mode IN ('camera','manual')),
 phase TEXT NOT NULL, camera_id INTEGER, accepted_at REAL NOT NULL,
 deadline_mono REAL NOT NULL, server_boot TEXT NOT NULL,
 ready_at REAL, failure TEXT NOT NULL DEFAULT '', dispatch TEXT NOT NULL,
 config_digest TEXT NOT NULL, coverage_digest TEXT NOT NULL, camera_digest TEXT NOT NULL,
 overrides TEXT NOT NULL DEFAULT '{}', dispatch_owner TEXT
);
CREATE INDEX IF NOT EXISTS idx_start_session ON attendance_start_attempts(session_id,id);
CREATE TABLE IF NOT EXISTS camera_capacity_limits (
 id INTEGER PRIMARY KEY CHECK(id=1), recognition INTEGER NOT NULL,
 viewers INTEGER NOT NULL, secondary INTEGER NOT NULL,
 updated_by_user_id INTEGER, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO camera_capacity_limits(id,recognition,viewers,secondary) VALUES(1,2,4,2);

CREATE TABLE IF NOT EXISTS attendance_start_operations (
 operation_id TEXT PRIMARY KEY, class_id INTEGER NOT NULL, actor INTEGER NOT NULL,
 session_id INTEGER NOT NULL REFERENCES attendance_sessions(id) ON DELETE CASCADE,
 attempt_id INTEGER NOT NULL REFERENCES attendance_start_attempts(id) ON DELETE CASCADE,
 intent TEXT NOT NULL, accepted_at REAL NOT NULL
);
