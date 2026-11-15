"""Occurrence-bound Attendance Display grants and safe display payloads."""

import datetime
import hashlib
import json
import re
import secrets
import sqlite3


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
CHECKED_IN_STATUSES = {"Present", "Late"}


class DisplayGrantNotFound(ValueError):
    """The requested attendance occurrence or active grant does not exist."""


class DisplayGrantForbidden(PermissionError):
    """The actor does not own the attendance occurrence."""


def hash_display_token(token):
    token = str(token or "")
    if not _TOKEN_RE.fullmatch(token):
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _owned_occurrence(conn, attendance_session_id, actor_user_id):
    row = conn.execute(
        """
        SELECT s.id, s.status, c.id AS class_id, c.teacher_id, c.is_archived
        FROM attendance_sessions s
        JOIN classes c ON c.id = s.class_id
        WHERE s.id = ?
        """,
        (attendance_session_id,),
    ).fetchone()
    if not row:
        raise DisplayGrantNotFound("Attendance occurrence not found.")
    if str(row["teacher_id"]) != str(actor_user_id):
        raise DisplayGrantForbidden("Only the assigned Teacher can control this Attendance Display.")
    if row["is_archived"]:
        raise DisplayGrantNotFound("Archived classes cannot enable a display.")
    if row["status"] not in ("Open", "Closed"):
        raise DisplayGrantNotFound("This occurrence has no attendance results to display.")
    return row


def enable_display_grant(conn, attendance_session_id, actor_user_id, now=None):
    """Create a new high-entropy grant and revoke any prior active link."""
    owned = _owned_occurrence(conn, attendance_session_id, actor_user_id)
    from services.session_lifecycle import lifecycle
    state = lifecycle(conn, attendance_session_id)
    if state and state['collection'] == 'starting':
        raise DisplayGrantNotFound("Wait for startup confirmation before enabling the display.")
    now = now or datetime.datetime.now()
    now_sql = now.strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        UPDATE attendance_display_grants
        SET revoked_at = ?, revoked_by_user_id = ?, revoke_reason = 'replaced_by_teacher'
        WHERE attendance_session_id IN (SELECT id FROM attendance_sessions WHERE class_id = ?) AND revoked_at IS NULL
        """,
        (now_sql, actor_user_id, owned["class_id"]),
    )
    token = secrets.token_urlsafe(32)
    conn.execute(
        """
        INSERT INTO attendance_display_grants
            (attendance_session_id, grant_hash, enabled_by_user_id, enabled_at)
        VALUES (?, ?, ?, ?)
        """,
        (attendance_session_id, hash_display_token(token), actor_user_id, now_sql),
    )
    return token


def revoke_display_grants(
    conn,
    attendance_session_id,
    actor_user_id=None,
    reason="teacher_deactivated",
    now=None,
):
    """Revoke every active link for one occurrence, retaining audit history."""
    now = now or datetime.datetime.now()
    now_sql = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor = conn.execute(
            """
            UPDATE attendance_display_grants
            SET revoked_at = ?, revoked_by_user_id = ?, revoke_reason = ?
            WHERE attendance_session_id = ? AND revoked_at IS NULL
            """,
            (now_sql, actor_user_id, reason, attendance_session_id),
        )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return 0
        raise
    return cursor.rowcount


def revoke_owned_display_grants(conn, attendance_session_id, actor_user_id, now=None):
    _owned_occurrence(conn, attendance_session_id, actor_user_id)
    return revoke_display_grants(
        conn,
        attendance_session_id,
        actor_user_id=actor_user_id,
        reason="teacher_deactivated",
        now=now,
    )


def active_display_grant(conn, attendance_session_id):
    try:
        return conn.execute(
            """
            SELECT * FROM attendance_display_grants
            WHERE attendance_session_id = ? AND revoked_at IS NULL
            ORDER BY id DESC LIMIT 1
            """,
            (attendance_session_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise


def resolve_display_grant(conn, token):
    """Resolve results authorization only; this never grants video or collection."""
    grant_hash = hash_display_token(token)
    if not grant_hash:
        return None
    # The general roster helper has a compatibility fallback for old unit
    # schemas. A retained public board must never use today's enrollments as
    # an invented historical roster when the history schema is unavailable.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='class_enrollment_periods'").fetchone():
        return None
    occurrence = conn.execute(
        """
        SELECT g.id AS grant_id, g.attendance_session_id,
               s.class_id, s.status, s.scheduled_date, s.actual_started_at, s.actual_ended_at,
               c.teacher_id, c.course_name, c.class_code,
               s.scheduled_start_time AS start_time,
               COALESCE(s.scheduled_end_time, c.end_time) AS end_time,
               r.room_code, r.room_name,
               cd.id AS camera_id, cd.name AS camera_name
        FROM attendance_display_grants g
        JOIN attendance_sessions s ON s.id = g.attendance_session_id
        JOIN classes c ON c.id = s.class_id AND c.is_archived = 0
        JOIN users u ON u.id = g.enabled_by_user_id AND u.id = c.teacher_id
          AND u.is_active = 1 AND u.role = 'Teacher' AND u.requires_password_change = 0
        JOIN rooms r ON r.id = c.room_id AND r.is_active = 1
        LEFT JOIN camera_devices cd ON cd.id = s.camera_id AND cd.is_active = 1
        WHERE g.grant_hash = ?
          AND g.revoked_at IS NULL
          AND s.status IN ('Open', 'Closed')
        LIMIT 1
        """,
        (grant_hash,),
    ).fetchone()
    return occurrence


def display_capture_allowed(conn, occurrence):
    """Results survive End; video must independently satisfy the live write gate."""
    if not occurrence or occurrence['status'] != 'Open':
        return False
    from services.session_lifecycle import lifecycle, collection_allowed, clock_unreliable
    state = lifecycle(conn, occurrence['attendance_session_id'])
    if state:
        return collection_allowed(state) and not clock_unreliable(conn)
    from services.attendance_sessions import occurrence_deadline
    return datetime.datetime.now() < occurrence_deadline(occurrence, occurrence['scheduled_date'])


def _attendance_time(row):
    # A correction changes status, not the original attendance recording time.
    value = row["timestamp"]
    if not value:
        return ""
    text = str(value)
    return text.split(" ", 1)[1][:5] if " " in text else text[:5]


def build_display_payload(conn, token):
    """Return the full, safe roster for one valid display grant."""
    occurrence = resolve_display_grant(conn, token)
    if not occurrence:
        return {
            "is_active": False,
            "authorized": False,
            "message": "Attendance Display is inactive.",
        }

    # Never reconstruct an old board from today's class_enrollments.
    from services.attendance_sessions import occurrence_roster_rows
    from services.session_lifecycle import lifecycle, snapshot
    state = lifecycle(conn, occurrence['attendance_session_id'])
    summary = snapshot(conn, occurrence['attendance_session_id']) if state else None
    capture_allowed = display_capture_allowed(conn, occurrence)
    saved = {row['student_id']: row for row in conn.execute(
        'SELECT * FROM attendance_records WHERE attendance_session_id=?',
        (occurrence['attendance_session_id'],))}
    historical = occurrence_roster_rows(conn, occurrence['attendance_session_id'])
    visible_ids = {r['id'] for r in conn.execute('SELECT id FROM students WHERE id IN (SELECT value FROM json_each(?)) AND is_active=1 AND permanently_removed_at IS NULL', (json.dumps([s['id'] for s in historical]),))}
    roster_rows = []
    for student in historical:
        # Historical membership never overrides privacy removal/inactivation.
        if student['id'] not in visible_ids:
            continue
        mark = saved.get(student['id'])
        roster_rows.append({'student_code': student['student_id'], 'name': student['name'],
            'record_id': mark['id'] if mark else None, 'status': mark['status'] if mark else 'Pending',
            'timestamp': mark['timestamp'] if mark else None})
    roster = []
    checked_in_count = 0
    for row in roster_rows:
        status = row["status"] or "Pending"
        if status in CHECKED_IN_STATUSES:
            checked_in_count += 1
        roster.append(
            {
                "record_id": row["record_id"],
                "student_code": row["student_code"],
                "name": row["name"],
                "status": status,
                "time": _attendance_time(row),
                "recorded_at": str(row["timestamp"] or ""),
            }
        )

    return {
        "is_active": capture_allowed,
        "collection_active": capture_allowed,
        "reopened": bool(summary and summary.get("reopening")),
        "collection_state": state['collection'] if state else ('collecting' if capture_allowed else 'stopped'),
        "closure_confirmed": bool(state['closed_at']) if state else occurrence['status'] == 'Closed',
        "review_required": summary['review_required'] if summary else any(s['status'] == 'Pending' for s in roster),
        "cleanup_unfinished": bool(state and state['cleanup'] == 'pending'),
        "scheduled_date": str(occurrence['scheduled_date']),
        "authorized": True,
        "attendance_session_id": occurrence["attendance_session_id"],
        "class_id": occurrence["class_id"],
        "course_name": occurrence["course_name"],
        "course_code": occurrence["class_code"],
        "room_name": occurrence["room_name"],
        "room_code": occurrence["room_code"],
        "scheduled_start_time": str(occurrence["start_time"]),
        "scheduled_end_time": str(occurrence["end_time"]),
        "started_at": str(occurrence["actual_started_at"]),
        "total_enrolled": len(roster),
        "checked_in_count": checked_in_count,
        "has_camera": bool(capture_allowed and occurrence["camera_id"] and occurrence["camera_name"]),
        "roster": roster,
    }


def revoke_class_displays(conn, class_id, actor, reason):
    """Security changes retire links permanently; later restoration cannot revive them."""
    for row in conn.execute('SELECT id FROM attendance_sessions WHERE class_id=?', (class_id,)).fetchall():
        revoke_display_grants(conn, row['id'], actor_user_id=actor, reason=reason)
