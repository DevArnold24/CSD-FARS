"""Canonical class schedule validation and conflict detection."""

import datetime
import re

from services.attendance_sessions import DAY_ALIASES, normalized_day_tokens


DAY_ORDER = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$")


class ScheduleValidationError(ValueError):
    """Raised when a proposed recurring weekly schedule is invalid."""


def _row_value(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return row.get(key, default) if isinstance(row, dict) else default


def parse_schedule_time(value):
    """Return seconds since midnight and canonical HH:MM:SS text."""
    text = str(value or "").strip()
    if not TIME_PATTERN.fullmatch(text):
        raise ScheduleValidationError("Enter valid start and end times.")
    parts = [int(part) for part in text.split(":")]
    if len(parts) == 2:
        parts.append(0)
    hour, minute, second = parts
    return hour * 3600 + minute * 60 + second, f"{hour:02d}:{minute:02d}:{second:02d}"


def canonical_schedule(days, start_time, end_time):
    """Validate and normalize a non-overnight recurring weekly schedule."""
    raw_days = days if isinstance(days, (list, tuple, set)) else str(days or "").replace(";", ",").split(",")
    tokens = set()
    for raw_day in raw_days:
        key = str(raw_day or "").strip().lower()
        if not key or key not in DAY_ALIASES:
            raise ScheduleValidationError("Select at least one valid scheduled day.")
        tokens.add(DAY_ALIASES[key])
    if not tokens:
        raise ScheduleValidationError("Select at least one valid scheduled day.")

    start_seconds, canonical_start = parse_schedule_time(start_time)
    end_seconds, canonical_end = parse_schedule_time(end_time)
    if end_seconds <= start_seconds:
        raise ScheduleValidationError("End time must be after start time; overnight classes are not supported.")

    canonical_days = [day for day in DAY_ORDER if day in tokens]
    return {
        "days": canonical_days,
        "days_text": ",".join(canonical_days),
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "start_time": canonical_start,
        "end_time": canonical_end,
    }


def ensure_initial_schedule_period(conn, class_info):
    """Ensure an existing class has an effective-dated schedule baseline."""
    effective_from = _row_value(class_info, "effective_start_date")
    if not effective_from:
        effective_from = datetime.date.today().isoformat()
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO class_schedule_periods
            (class_id, effective_from, effective_until, days_of_week,
             start_time, end_time)
        VALUES (?, ?, NULL, ?, ?, ?)
        """,
        (
            _row_value(class_info, "id"),
            str(effective_from),
            _row_value(class_info, "days_of_week"),
            _row_value(class_info, "start_time"),
            _row_value(class_info, "end_time"),
        ),
    )
    return cursor.lastrowid


def transition_class_schedule(conn, class_info, schedule, effective_from):
    """Close the prior schedule period and persist a future-effective revision.

    Multiple edits with the same effective date update that still-unapplied
    revision instead of creating overlapping periods.
    """
    class_id = int(_row_value(class_info, "id"))
    effective_from = str(effective_from)
    ensure_initial_schedule_period(conn, class_info)

    period = conn.execute(
        """
        SELECT *
        FROM class_schedule_periods
        WHERE class_id = ?
          AND effective_from <= ?
          AND (effective_until IS NULL OR effective_until > ?)
        ORDER BY effective_from DESC, id DESC
        LIMIT 1
        """,
        (class_id, effective_from, effective_from),
    ).fetchone()

    if period and str(period["effective_from"]) == effective_from:
        conn.execute(
            """
            UPDATE class_schedule_periods
            SET days_of_week = ?, start_time = ?, end_time = ?
            WHERE id = ?
            """,
            (
                schedule["days_text"],
                schedule["start_time"],
                schedule["end_time"],
                period["id"],
            ),
        )
        return period["id"]

    if period:
        conn.execute(
            "UPDATE class_schedule_periods SET effective_until = ? WHERE id = ?",
            (effective_from, period["id"]),
        )

    cursor = conn.execute(
        """
        INSERT INTO class_schedule_periods
            (class_id, effective_from, effective_until, days_of_week,
             start_time, end_time)
        VALUES (?, ?, NULL, ?, ?, ?)
        """,
        (
            class_id,
            effective_from,
            schedule["days_text"],
            schedule["start_time"],
            schedule["end_time"],
        ),
    )
    return cursor.lastrowid


def schedule_period_for_date(conn, class_id, scheduled_date):
    """Return the immutable schedule revision applicable to one class date."""
    return conn.execute(
        """
        SELECT *
        FROM class_schedule_periods
        WHERE class_id = ?
          AND effective_from <= ?
          AND (effective_until IS NULL OR effective_until > ?)
        ORDER BY effective_from DESC, id DESC
        LIMIT 1
        """,
        (class_id, str(scheduled_date), str(scheduled_date)),
    ).fetchone()


def class_on_date(conn, class_info, scheduled_date):
    """Resolve timing without mutating a class or an accepted attendance record.

    Legacy callers with no schedule periods keep their original fields. Once a
    period exists, a gap is non-operational (archive/restore must not fill it).
    """
    info = dict(class_info)
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='class_schedule_periods'").fetchone()
    if not exists or not info.get('id'):
        return info
    period = schedule_period_for_date(conn, info['id'], scheduled_date)
    if period:
        info.update({key: period[key] for key in ('days_of_week', 'start_time', 'end_time')})
    elif conn.execute('SELECT 1 FROM class_schedule_periods WHERE class_id=? LIMIT 1', (info['id'],)).fetchone():
        info['days_of_week'] = ''
    return info


def class_schedule_view(conn, class_info, scheduled_date):
    """Show today's timing alongside a saved change that has not taken effect."""
    info = class_on_date(conn, class_info, scheduled_date)
    info['pending_schedule'] = None
    if info.get('is_archived') or not info.get('id'):
        return info
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='class_schedule_periods'").fetchone():
        return info
    future = conn.execute("""
        SELECT effective_from, days_of_week, start_time, end_time
        FROM class_schedule_periods
        WHERE class_id=? AND effective_from>?
        ORDER BY effective_from, id LIMIT 1
    """, (info['id'], str(scheduled_date))).fetchone()
    fields = ('days_of_week', 'start_time', 'end_time')
    if future and any(future[key] != info.get(key) for key in fields):
        info['pending_schedule'] = dict(future)
    return info


def schedule_edit_effective_date(conn, class_id, today):
    """Keep a materialized meeting's date under its original schedule revision."""
    saved_today = conn.execute(
        'SELECT 1 FROM attendance_sessions WHERE class_id=? AND scheduled_date=? LIMIT 1',
        (class_id, today.isoformat()),
    ).fetchone()
    return today + datetime.timedelta(days=1) if saved_today else today


def close_class_schedule(conn, class_id, effective_until):
    """End recurring schedule generation at an exclusive date boundary.

    Future or same-day revisions never became operational and are removed.
    The period spanning the boundary is retained as readable history and
    bounded at ``effective_until``. Materialized attendance is independent of
    these periods and is not changed.
    """
    boundary = str(effective_until)
    conn.execute(
        """
        DELETE FROM class_schedule_periods
        WHERE class_id = ? AND effective_from >= ?
        """,
        (class_id, boundary),
    )
    conn.execute(
        """
        UPDATE class_schedule_periods
        SET effective_until = ?
        WHERE class_id = ?
          AND effective_from < ?
          AND (effective_until IS NULL OR effective_until > ?)
        """,
        (boundary, class_id, boundary, boundary),
    )


def resume_class_schedule(conn, class_info, schedule, effective_from):
    """Start a new schedule period when an archived class is restored."""
    close_class_schedule(conn, _row_value(class_info, "id"), effective_from)
    cursor = conn.execute(
        """
        INSERT INTO class_schedule_periods
            (class_id, effective_from, effective_until, days_of_week,
             start_time, end_time)
        VALUES (?, ?, NULL, ?, ?, ?)
        """,
        (
            int(_row_value(class_info, "id")),
            str(effective_from),
            schedule["days_text"],
            schedule["start_time"],
            schedule["end_time"],
        ),
    )
    return cursor.lastrowid


def find_schedule_conflict(conn, room_id, teacher_id, schedule, exclude_class_id=None):
    """Return a user-facing Room or Teacher conflict, or ``None``.

    Boundaries may touch. Only actual overlap (start < other end and end >
    other start) on at least one shared recurring day conflicts.
    """
    query = """
        SELECT c.id, c.course_name, c.class_code, c.days_of_week,
               c.start_time, c.end_time, c.room_id, c.teacher_id,
               r.room_code, u.username AS teacher_name
        FROM classes c
        JOIN users u ON u.id = c.teacher_id AND u.role = 'Teacher'
        LEFT JOIN rooms r ON r.id = c.room_id
        WHERE c.is_archived = 0
          AND (c.room_id = ? OR c.teacher_id = ?)
    """
    params = [room_id, teacher_id]
    if exclude_class_id is not None:
        query += " AND c.id != ?"
        params.append(exclude_class_id)
    query += " ORDER BY c.id"

    proposed_days = set(schedule["days"])
    for existing in conn.execute(query, params).fetchall():
        shared_days = proposed_days.intersection(normalized_day_tokens(existing["days_of_week"]))
        if not shared_days:
            continue
        try:
            existing_start, _ = parse_schedule_time(existing["start_time"])
            existing_end, _ = parse_schedule_time(existing["end_time"])
        except ScheduleValidationError:
            # Preserve legacy rows without allowing malformed historical data to
            # crash creation of a valid new schedule.
            continue
        overlaps = schedule["start_seconds"] < existing_end and schedule["end_seconds"] > existing_start
        if not overlaps:
            continue

        days_text = ", ".join(day for day in DAY_ORDER if day in shared_days)
        if existing["room_id"] == room_id:
            room_label = existing["room_code"] or "the selected Room"
            return (
                f"Room Conflict: Room {room_label} already has an overlapping class on {days_text} "
                f"from {existing['start_time']} to {existing['end_time']}."
            )
        if existing["teacher_id"] == teacher_id:
            return (
                f"Teacher Schedule Conflict: Teacher {existing['teacher_name']} already has an "
                f"overlapping class on {days_text} "
                f"from {existing['start_time']} to {existing['end_time']}."
            )
    return None


def normalized_class_code_exists(conn, teacher_id, class_code, exclude_class_id=None):
    query = """
        SELECT 1 FROM classes
        WHERE teacher_id = ? AND LOWER(TRIM(class_code)) = LOWER(TRIM(?))
    """
    params = [teacher_id, class_code]
    if exclude_class_id is not None:
        query += " AND id != ?"
        params.append(exclude_class_id)
    return conn.execute(query, params).fetchone() is not None
