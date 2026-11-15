"""Session-backed attendance helpers."""

import datetime
import sqlite3

import config
from services import session_lifecycle as lifecycle


VISIBLE_ATTENDANCE_STATUSES = {"Present", "Late", "Absent", "Excused"}
EDITABLE_ATTENDANCE_STATUSES = VISIBLE_ATTENDANCE_STATUSES
LEGACY_AUDIT_STATUSES = {"Present", "Late", "Absent"}

DAY_ALIASES = {
    "mon": "Mon",
    "monday": "Mon",
    "tue": "Tue",
    "tuesday": "Tue",
    "wed": "Wed",
    "wednesday": "Wed",
    "thu": "Thu",
    "thursday": "Thu",
    "fri": "Fri",
    "friday": "Fri",
    "sat": "Sat",
    "saturday": "Sat",
    "sun": "Sun",
    "sunday": "Sun",
}


def row_value(row, key, default=None):
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        if isinstance(row, dict):
            return row.get(key, default)
        return default


def sql_datetime(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def sql_date(value):
    return value.strftime("%Y-%m-%d")


def late_marking_time(scheduled_start, minutes):
    """Validate supported calendar arithmetic without imposing a lateness policy."""
    if type(minutes) is not int or minutes < 0:
        raise ValueError("Enter a valid whole number of minutes, zero or greater.")
    try:
        return scheduled_start + datetime.timedelta(minutes=minutes)
    except OverflowError as exc:
        raise ValueError("The late-marking time is too large. Enter a smaller number of minutes.") from exc


def attendance_occurrence_key(scheduled_date, scheduled_start_time):
    date_text = str(scheduled_date)
    hour, minute, second = _parse_start_time(scheduled_start_time)
    return f"{date_text}T{hour:02d}:{minute:02d}:{second:02d}"


def attendance_occurrence_datetime(scheduled_date, scheduled_start_time):
    """Return the occurrence's institutional wall time as a naive UTC SQL timestamp."""
    local_value = datetime.datetime.strptime(
        attendance_occurrence_key(scheduled_date, scheduled_start_time),
        "%Y-%m-%dT%H:%M:%S",
    )
    utc_value = local_value - datetime.timedelta(
        minutes=config.INSTITUTION_UTC_OFFSET_MINUTES
    )
    return sql_datetime(utc_value)


def _parse_start_time(start_time):
    parts = str(start_time or "00:00:00").split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    second = int(parts[2]) if len(parts) > 2 else 0
    return hour, minute, second


def occurrence_deadline(
    class_info,
    scheduled_date,
    grace_minutes=None,
    scheduled_end_time=None,
):
    """Return the end of the resumable live window for one occurrence."""
    if isinstance(scheduled_date, datetime.datetime):
        occurrence_date = scheduled_date.date()
    elif isinstance(scheduled_date, datetime.date):
        occurrence_date = scheduled_date
    else:
        occurrence_date = datetime.datetime.strptime(str(scheduled_date), "%Y-%m-%d").date()
    end_time = scheduled_end_time or row_value(class_info, "end_time", "00:00:00")
    end_hour, end_minute, end_second = _parse_start_time(end_time)
    scheduled_end = datetime.datetime.combine(
        occurrence_date,
        datetime.time(end_hour, end_minute, end_second),
    )
    grace = config.SESSION_RESUME_GRACE_MINUTES if grace_minutes is None else grace_minutes
    return scheduled_end + datetime.timedelta(minutes=grace)


def normalized_day_tokens(days_of_week):
    tokens = set()
    for raw in str(days_of_week or "").replace(";", ",").split(","):
        key = raw.strip().lower()
        if key in DAY_ALIASES:
            tokens.add(DAY_ALIASES[key])
    return tokens


def session_window_for_class(class_info, now=None, *, conn=None):
    """Resolve the scheduled occurrence, including opening on its previous date."""
    from services.attendance_settings import load, SettingsUnavailable
    from services.class_scheduling import class_on_date
    if conn is None:
        from database.db_manager import managed_transaction
        with managed_transaction(commit=False) as read_conn:
            return session_window_for_class(class_info, now, conn=read_conn)
    now = now or lifecycle.institutional_now()
    error, policy = None, None
    try:
        policy = load(conn)
    except (SettingsUnavailable, OSError, sqlite3.Error) as exc:
        error = str(exc) if isinstance(exc, SettingsUnavailable) else 'Attendance opening cannot be verified. Ask the Dean to check Attendance Settings.'
    minutes = policy['minutes'] if policy else 0
    def candidate(offset):
        day = now.date() + datetime.timedelta(days=offset)
        info = class_on_date(conn, class_info, day.isoformat())
        start_parts = _parse_start_time(row_value(info, "start_time", "00:00:00"))
        end_text = row_value(info, "end_time", None)
        start = datetime.datetime.combine(day, datetime.time(*start_parts))
        if end_text:
            end = datetime.datetime.combine(start.date(), datetime.time(*_parse_start_time(end_text)))
            if end <= start:
                end += datetime.timedelta(days=1)
        else:
            end = start + datetime.timedelta(minutes=config.SESSION_DURATION_MINUTES)
        return start, start - datetime.timedelta(minutes=minutes), end, info

    candidates = [candidate(offset) for offset in (-1, 0, 1)]
    eligible = [item for item in candidates if item[0].strftime('%a') in normalized_day_tokens(item[3]['days_of_week']) and item[1] <= now < item[2]]
    # Prefer today's in-progress class over tomorrow's overlapping early window.
    in_progress = [item for item in eligible if item[0] <= now]
    upcoming_midnight = [item for item in candidates if item[0].date() > now.date()
                         and item[1].date() == now.date() and item[0].strftime('%a') in normalized_day_tokens(item[3]['days_of_week'])]
    scheduled_start, window_start, window_end, effective_info = (in_progress or eligible or upcoming_midnight or [candidates[1]])[0]
    today = scheduled_start.strftime('%a')
    meets_today = today in normalized_day_tokens(effective_info['days_of_week'])
    allowed = not error and meets_today and window_start <= now < window_end
    if error:
        message = error
    elif not meets_today:
        message = "Live attendance can only be started on a scheduled class day."
    elif now < window_start:
        message = f"Live attendance opens at {window_start.strftime('%Y-%m-%d %H:%M')}."
    elif now >= window_end:
        message = "The live attendance window for this class has already closed."
    else:
        message = "Live attendance is available for this class."
    return {
        "allowed": bool(allowed), "message": message,
        "scheduled_date": sql_date(scheduled_start), "scheduled_start": scheduled_start,
        "window_start": window_start, "window_end": window_end,
        "resume_deadline": window_end + datetime.timedelta(minutes=config.SESSION_RESUME_GRACE_MINUTES),
        "today_token": today, "opening_minutes": policy['minutes'] if policy else None,
        "opening_policy_version": policy['version'] if policy else None,
        "opening_error": error,
        "effective_class": effective_info,
    }


def get_open_attendance_session(conn, class_id):
    return lifecycle.enrich(conn, conn.execute(
        """
        SELECT *
        FROM attendance_sessions
        WHERE class_id = ? AND status = 'Open'
        ORDER BY actual_started_at DESC, id DESC
        LIMIT 1
        """,
        (class_id,),
    ).fetchone())


def get_latest_attendance_session(conn, class_id, scheduled_date=None):
    params = [class_id]
    date_filter = ""
    if scheduled_date:
        date_filter = "AND scheduled_date = ?"
        params.append(scheduled_date)
    return lifecycle.enrich(conn, conn.execute(
        f"""
        SELECT *
        FROM attendance_sessions
        WHERE class_id = ?
        {date_filter}
        ORDER BY actual_started_at DESC, id DESC
        LIMIT 1
        """,
        params,
    ).fetchone())


def get_attendance_occurrence(conn, class_id, scheduled_date, scheduled_start_time):
    """Return the durable session row for one scheduled class occurrence."""
    # A recurring class has at most one logical occurrence per institutional
    # date. Its stored start/end times are historical attributes, not identity
    # inputs that may be re-derived after a schedule edit.
    by_date = conn.execute(
        """
        SELECT * FROM attendance_sessions
        WHERE class_id = ? AND scheduled_date = ?
        ORDER BY (occurrence_key IS NOT NULL) DESC, id DESC
        LIMIT 1
        """,
        (class_id, str(scheduled_date)),
    ).fetchone()
    if by_date:
        return lifecycle.enrich(conn, by_date)
    occurrence_key = attendance_occurrence_key(scheduled_date, scheduled_start_time)
    canonical = conn.execute(
        """
        SELECT * FROM attendance_sessions
        WHERE class_id = ? AND occurrence_key = ?
        LIMIT 1
        """,
        (class_id, occurrence_key),
    ).fetchone()
    if canonical:
        return lifecycle.enrich(conn, canonical)
    return lifecycle.enrich(conn, conn.execute(
        """
        SELECT *
        FROM attendance_sessions
        WHERE class_id = ? AND scheduled_date = ? AND scheduled_start_time = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (class_id, str(scheduled_date), str(scheduled_start_time)),
    ).fetchone())


def occurrence_can_resume(class_info, occurrence, now=None):
    if not occurrence or occurrence["status"] in {"Cancelled", "Missed"}:
        return False
    if row_value(occurrence, 'policy_version') == 2:
        if row_value(occurrence, 'interruption_reason') == 'clock_anomaly':
            return False
        instant = lifecycle.epoch(now) if now else lifecycle.time.time()
        return bool(occurrence['collection'] == 'interrupted' and not occurrence['end_operation']
                    and instant < occurrence['cutoff'] and occurrence['cleanup'] != 'pending')
    now = now or datetime.datetime.now()
    return now < occurrence_deadline(
        class_info,
        occurrence["scheduled_date"],
        scheduled_end_time=row_value(occurrence, "scheduled_end_time"),
    )


def reopen_attendance_session(conn, session_id, version=None, actor=None):
    """Reopen a durable occurrence without replacing its attendance history."""
    if lifecycle.lifecycle(conn, session_id):
        lifecycle.resume(conn, session_id, version, actor)
        return session_id
    conn.execute(
        """
        UPDATE attendance_sessions
        SET status = 'Open', actual_ended_at = NULL, close_reason = ''
        WHERE id = ?
        """,
        (session_id,),
    )
    return session_id


def create_attendance_session(conn, class_info, actor_user_id, now=None):
    now = now or lifecycle.institutional_now()
    window = session_window_for_class(class_info, now, conn=conn)
    scheduled_date = window['scheduled_date']
    class_info = window['effective_class']
    lifecycle.activation_check(conn)
    existing = get_attendance_occurrence(
        conn,
        row_value(class_info, "id"),
        scheduled_date,
        row_value(class_info, "start_time"),
    )
    if existing:
        raise ValueError("An attendance occurrence already exists for this class date.")
    cursor = conn.execute(
        """
        INSERT INTO attendance_sessions
            (class_id, teacher_id, camera_id, scheduled_date, scheduled_start_time,
             scheduled_end_time, occurrence_key, roster_anchor_at,
             actual_started_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?,
                STRFTIME('%Y-%m-%d %H:%M:%f', 'now'), ?, 'Open')
        """,
        (
            row_value(class_info, "id"),
            actor_user_id,
            row_value(class_info, "camera_id"),
            scheduled_date,
            row_value(class_info, "start_time"),
            row_value(class_info, "end_time"),
            attendance_occurrence_key(scheduled_date, row_value(class_info, "start_time")),
            sql_datetime(now),
        ),
    )
    lifecycle.create(conn, cursor.lastrowid, class_info, scheduled_date)
    return cursor.lastrowid


def enrollment_period_contains_sql(anchor_at, anchor_order):
    """Shared historical membership predicate; arguments are internal SQL expressions."""
    return f"""(
        STRFTIME('%Y-%m-%d %H:%M:%S', cep.enrolled_at) < STRFTIME('%Y-%m-%d %H:%M:%S', {anchor_at})
        OR (STRFTIME('%Y-%m-%d %H:%M:%S', cep.enrolled_at) = STRFTIME('%Y-%m-%d %H:%M:%S', {anchor_at})
            AND (cep.enrolled_order IS NULL OR {anchor_order} IS NULL OR cep.enrolled_order <= {anchor_order}))
    ) AND (
        cep.unenrolled_at IS NULL
        OR STRFTIME('%Y-%m-%d %H:%M:%S', cep.unenrolled_at) > STRFTIME('%Y-%m-%d %H:%M:%S', {anchor_at})
        OR (STRFTIME('%Y-%m-%d %H:%M:%S', cep.unenrolled_at) = STRFTIME('%Y-%m-%d %H:%M:%S', {anchor_at})
            AND cep.unenrolled_order IS NOT NULL AND {anchor_order} IS NOT NULL
            AND cep.unenrolled_order > {anchor_order})
    )"""


def occurrence_roster_rows(conn, session_id, class_id=None):
    """Return the students whose enrollment period covered an occurrence."""
    occurrence = conn.execute(
        """
        SELECT class_id, scheduled_date, scheduled_start_time, roster_anchor_at,
               roster_anchor_order
        FROM attendance_sessions
        WHERE id = ?
        """,
        (session_id,),
    ).fetchone()
    if not occurrence:
        return []
    if class_id is not None and int(occurrence["class_id"]) != int(class_id):
        return []
    # Enrollment timestamps and live roster anchors use SQLite UTC. Migrated or
    # synthetic rows without an anchor fall back to the scheduled institutional
    # occurrence converted to UTC.
    occurrence_at = occurrence["roster_anchor_at"] or attendance_occurrence_datetime(
        occurrence["scheduled_date"], occurrence["scheduled_start_time"]
    )
    try:
        return conn.execute(
            f"""
            WITH roster_ids(student_id) AS (
                SELECT cep.student_id
                FROM class_enrollment_periods cep
                WHERE cep.class_id = :class_id
                  AND {enrollment_period_contains_sql(':anchor_at', ':anchor_order')}
                UNION
                SELECT ar.student_id
                FROM attendance_records ar
                WHERE ar.attendance_session_id = :session_id
                  AND ar.class_id = :class_id
            )
            SELECT s.id, s.student_id, s.name, s.program_id, s.year_level
            FROM roster_ids roster
            JOIN students s ON s.id = roster.student_id
            ORDER BY s.name COLLATE NOCASE, s.id
            """,
            {
                "class_id": occurrence["class_id"],
                "anchor_at": occurrence_at,
                "anchor_order": occurrence["roster_anchor_order"],
                "session_id": session_id,
            },
        ).fetchall()
    except Exception as exc:
        # Compatibility for narrow legacy/unit schemas that predate migration
        # 023. Production databases always use enrollment periods.
        if "class_enrollment_periods" not in str(exc):
            raise
        return conn.execute(
            """
            SELECT s.id, s.student_id, s.name, s.program_id, s.year_level
            FROM class_enrollments ce
            JOIN students s ON s.id = ce.student_id
            WHERE ce.class_id = ?
            ORDER BY s.name COLLATE NOCASE, s.id
            """,
            (occurrence["class_id"],),
        ).fetchall()


def materialize_absences(conn, session_id, class_id, timestamp=None):
    timestamp = timestamp or sql_datetime(datetime.datetime.now())
    roster = occurrence_roster_rows(conn, session_id, class_id)
    conn.executemany(
        """
        INSERT OR IGNORE INTO attendance_records
            (attendance_session_id, student_id, class_id, timestamp, status, source, reason)
        VALUES (?, ?, ?, ?, 'Absent', 'system', '')
        """,
        [(session_id, student["id"], class_id, timestamp) for student in roster],
    )


def close_attendance_session(conn, session_id, class_id=None, reason="session_stopped", ended_at=None):
    if not session_id:
        return None
    session = conn.execute(
        "SELECT * FROM attendance_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if not session:
        return None
    if lifecycle.lifecycle(conn, session_id):
        lifecycle.accept_end(conn, session_id, reason=reason,
                             now=lifecycle.epoch(ended_at) if ended_at else None)
        return session_id
    class_id = class_id or session["class_id"]
    ended_at = ended_at or datetime.datetime.now()
    ended_at_sql = sql_datetime(ended_at)
    materialize_absences(conn, session_id, class_id, ended_at_sql)
    # PB-004: retain read-only results; video has an independent live gate.
    conn.execute(
        """
        UPDATE attendance_sessions
        SET status = 'Closed',
            actual_ended_at = COALESCE(actual_ended_at, ?),
            close_reason = CASE
                WHEN close_reason = '' THEN ?
                ELSE close_reason
            END
        WHERE id = ?
        """,
        (ended_at_sql, reason, session_id),
    )
    return session_id


def interrupt_attendance_session(conn, session_id, reason='collection_interrupted'):
    """Stop collection while preserving an unfinished occurrence and results board.

    Camera failure ends execution, not attendance. The owner may resume the
    same Open occurrence once worker cleanup is confirmed, or explicitly End it.
    """
    # Results remain authorized, but interruption immediately stops capture.
    lifecycle.interrupt(conn, session_id, reason)
    return session_id


def _is_student_enrolled_for_occurrence(conn, session_id, class_id, student_pk):
    return any(
        int(student["id"]) == int(student_pk)
        for student in occurrence_roster_rows(conn, session_id, class_id)
    )


def attendance_audit_kind_sql(record_id):
    """Classify trusted audit operations, not editable reasons (internal SQL only).

    A historical first Pending -> Present operation is ambiguous: it may have
    been live manual attendance or a later correction. Keep that uncertainty.
    Other historical transitions are corrections under both policies.
    """
    return f"""COALESCE((SELECT COALESCE(aca.operation_kind,
        CASE WHEN aca.previous_status='Pending' AND aca.new_status='Present'
             AND NOT EXISTS (SELECT 1 FROM attendance_change_audit prior
                             WHERE prior.attendance_record_id=aca.attendance_record_id AND prior.id<aca.id)
             THEN 'legacy_manual' ELSE 'correction' END)
        FROM attendance_change_audit aca WHERE aca.attendance_record_id={record_id}
        ORDER BY aca.id DESC LIMIT 1), 'correction')"""


def upsert_attendance_record(
    conn,
    session_id,
    class_id,
    student_pk,
    status,
    source="manual_edit",
    reason="",
    actor_user_id=None,
    changed_at=None,
    live=False,
    version=None,
):
    if status not in EDITABLE_ATTENDANCE_STATUSES:
        raise ValueError("Invalid attendance status.")
    reason = (reason or "").strip()
    if status == "Excused" and not reason:
        raise ValueError("A reason is required when marking a student excused.")
    if not _is_student_enrolled_for_occurrence(conn, session_id, class_id, student_pk):
        raise ValueError("Student is not enrolled for this attendance occurrence.")

    decision = lifecycle.guard_live_write(conn, session_id, version) if live or source == 'face_recognition' else None
    if source == 'face_recognition':
        from services.session_start import current as current_start
        if current_start(conn, session_id):
            consent = conn.execute('SELECT consent_granted,is_active,permanently_removed_at FROM students WHERE id=?', (student_pk,)).fetchone()
            if not consent or not consent['consent_granted'] or not consent['is_active'] or consent['permanently_removed_at'] is not None:
                raise ValueError('The student is not currently eligible for camera attendance.')
    changed_at = changed_at or (lifecycle.institutional_now() if lifecycle.lifecycle(conn, session_id) else datetime.datetime.now())
    if decision and source == 'face_recognition':
        state, accepted_at = decision
        status = 'Late' if accepted_at > state['late_at'] else 'Present'
        changed_at = datetime.datetime.fromisoformat(lifecycle.local_sql(accepted_at))
    changed_at_sql = sql_datetime(changed_at)
    if source == "face_recognition":
        from services.session_reopening import collection_window as reopening
        if reopening(conn, session_id) and conn.execute('SELECT 1 FROM attendance_records WHERE attendance_session_id=? AND student_id=?', (session_id, student_pk)).fetchone():
            return False
        # A resumed occurrence can contain absences from an earlier End or a
        # legacy camera failure. Eligibility belongs in the write itself: a
        # concurrent correction or End must win over delayed recognition.
        cursor = conn.execute(
            """
            INSERT INTO attendance_records
                (attendance_session_id, student_id, class_id, timestamp, status, source, reason)
            SELECT ?, ?, ?, ?, ?, 'face_recognition', ''
            WHERE EXISTS (
                SELECT 1 FROM attendance_sessions
                WHERE id = ? AND class_id = ? AND status = 'Open'
            )
            ON CONFLICT(attendance_session_id, student_id) DO UPDATE SET
                status = excluded.status,
                source = excluded.source,
                timestamp = excluded.timestamp,
                updated_at = excluded.timestamp
            WHERE attendance_records.status = 'Absent'
              AND attendance_records.source = 'system'
              AND COALESCE(attendance_records.reason, '') = ''
              AND attendance_records.updated_by_user_id IS NULL
            """,
            (session_id, student_pk, class_id, changed_at_sql, status, session_id, class_id),
        )
        return cursor.rowcount > 0

    existing = conn.execute(
        """
        SELECT *
        FROM attendance_records
        WHERE attendance_session_id = ? AND student_id = ?
        """,
        (session_id, student_pk),
    ).fetchone()

    # Only an initial Present mark in a confirmed active manual attempt is reason-free.
    # guard_live_write above reserves the writer before we inspect the existing mark.
    from services.session_start import current as current_start
    attempt = current_start(conn, session_id)
    initial_manual = bool(source == 'manual_edit' and live and decision and not existing
                          and status == 'Present' and attempt and attempt['mode'] == 'manual'
                          and actor_user_id is not None)
    if source == 'manual_edit' and not reason and not initial_manual:
        raise ValueError("A reason is required for attendance changes.")
    # A missing row is unresolved, even for older closed sessions. Keep the audit
    # consistent with reports without rewriting any existing historical marks.
    previous_status = existing["status"] if existing else 'Pending'
    previous_reason = existing["reason"] if existing else ""

    if existing:
        record_id = existing["id"]
        conn.execute(
            """
            UPDATE attendance_records
            SET status = ?,
                source = ?,
                reason = ?,
                updated_by_user_id = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (status, source, reason, actor_user_id, changed_at_sql, record_id),
        )
    else:
        cursor = conn.execute(
            """
            INSERT INTO attendance_records
                (attendance_session_id, student_id, class_id, timestamp, status,
                 source, reason, updated_by_user_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                student_pk,
                class_id,
                changed_at_sql,
                status,
                source,
                reason,
                actor_user_id,
                changed_at_sql if actor_user_id else None,
            ),
        )
        record_id = cursor.lastrowid

    if source == "manual_edit":
        conn.execute(
            """
            INSERT INTO attendance_change_audit
                (attendance_session_id, attendance_record_id, class_id, student_id,
                 actor_user_id, previous_status, new_status, previous_reason,
                 new_reason, change_reason, changed_at, operation_kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                record_id,
                class_id,
                student_pk,
                actor_user_id,
                previous_status,
                status,
                previous_reason or "",
                reason,
                'Initial manual attendance.' if initial_manual and not reason else reason,
                changed_at_sql,
                'initial_manual' if initial_manual else 'correction',
            ),
        )
        if previous_status in LEGACY_AUDIT_STATUSES and status in LEGACY_AUDIT_STATUSES:
            conn.execute(
                """
                INSERT INTO audit_logs
                    (attendance_record_id, teacher_id, previous_status, new_status, reason, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (record_id, actor_user_id, previous_status, status, reason, changed_at_sql),
            )

    return True
