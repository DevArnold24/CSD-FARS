import json
from functools import wraps
import base64
import os
import uuid
import csv
import io
import math
import openpyxl
import time
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from datetime import datetime, timezone, timedelta

import sqlite3
import re
from werkzeug.exceptions import HTTPException
from flask import jsonify, request, session, render_template, flash, redirect, url_for, abort, Response, send_file, current_app

from core import get_logger
from core.routing import parse_record_id
from core.utils import *
from security.rbac import has_permission, permission_required
from security.session_auth import authenticated_session_user, credential_matches
from database.db_manager import get_db_connection, managed_transaction
from services.audit import log_system_event
from services.recognition_settings import load_settings, normal_match_threshold
from routes.blueprints import classes_bp

from core.streaming import generate_mjpeg_stream
from workers.session_manager import session_manager
from services import session_lifecycle as lifecycle
from services.attendance_sessions import (
    EDITABLE_ATTENDANCE_STATUSES,
    attendance_occurrence_datetime,
    attendance_occurrence_key,
    close_attendance_session,
    create_attendance_session,
    get_attendance_occurrence,
    get_latest_attendance_session,
    get_open_attendance_session,
    occurrence_can_resume,
    occurrence_deadline,
    occurrence_roster_rows,
    reopen_attendance_session,
    session_window_for_class,
    upsert_attendance_record,
    normalized_day_tokens,
    row_value,
    interrupt_attendance_session,
    late_marking_time,
)
from services.attendance_display import (
    DisplayGrantForbidden,
    DisplayGrantNotFound,
    active_display_grant,
    enable_display_grant,
    hash_display_token,
    revoke_owned_display_grants,
)
from services.class_scheduling import (
    ScheduleValidationError,
    canonical_schedule,
    class_schedule_view,
    close_class_schedule,
    find_schedule_conflict,
    normalized_class_code_exists,
    resume_class_schedule,
    schedule_period_for_date,
    schedule_edit_effective_date,
    transition_class_schedule,
)
from services.export_security import spreadsheet_safe_text
import config
from services import session_start

active_sessions = session_manager.active_sessions

logger = get_logger("classes")


def _bounded_runtime_read(view):
    @wraps(view)
    def read(*args, **kwargs):
        guard = getattr(session_manager, 'bounded_runtime_guard', None)
        if guard is None:  # Lightweight injected managers used by headless tests.
            return view(*args, **kwargs)
        with guard() as acquired:
            if not acquired:
                message = 'Runtime status is temporarily unavailable while camera work settles. Attendance timing is unchanged. Reload to verify; do not send another Start.'
                return (jsonify(error=message), 503) if request.path.startswith('/api/') else (message, 503)
            return view(*args, **kwargs)
    return read


def _reconcile_session_runtime(conn, class_info, occurrence):
    """Reconcile camera execution, not the durable attendance occurrence.

    The caller holds lifecycle_guard before opening this transaction and has
    already checked ownership. The occurrence's camera snapshot distinguishes
    a valid manual session from a missing camera worker after an app restart.
    Room camera availability is not evidence of how this occurrence was run.
    """
    class_id = class_info["id"]
    durable = lifecycle.snapshot(conn, occurrence['id']) if occurrence else None
    if durable:
        if durable['collection_allowed'] and occurrence['camera_id'] is not None and session_manager.worker_state(class_info['id']) != 'running':
            reason = session_manager.recent_stops.get(class_info['id'], {}).get('reason', 'worker_unavailable')
            lifecycle.interrupt(conn, occurrence['id'], reason)
            conn.execute('UPDATE classes SET is_active=0 WHERE id=?', (class_info['id'],))
            durable = lifecycle.snapshot(conn, occurrence['id'])
        active = durable['collection_allowed']
        state = ('active' if occurrence['camera_id'] else 'manual') if active else ('starting' if durable['collection'] == 'starting' else ('interrupted' if durable['collection'] == 'interrupted' else 'inactive'))
        return {'is_active': active, 'session_state': state, 'session_message': durable['message'],
                'resume_available': durable['resume_available'], 'occurrence_open': durable['end_available'],
                'lifecycle': durable}
    active = bool(class_info["is_active"])
    occurrence_open = bool(occurrence and occurrence["status"] == "Open")
    expects_worker = bool(occurrence_open and occurrence["camera_id"] is not None)
    has_context = class_id in session_manager.active_sessions
    state = "active" if active else "inactive"
    message = None
    if expects_worker or has_context:
        worker_state = session_manager.worker_state(class_id)
        if worker_state in {"stopping", "unverified"}:
            state = worker_state
            message = (
                "Camera cleanup is still pending. Wait before resuming; the camera remains reserved."
                if state == "stopping" else
                "Camera worker status could not be verified. Resuming is blocked until cleanup is confirmed."
            )
        elif worker_state == "missing" and occurrence_open:
            if active:
                conn.execute("UPDATE classes SET is_active = 0 WHERE id = ?", (class_id,))
            active = False
            state = "interrupted"
            message = "Camera attendance was interrupted. Existing attendance is saved. Resume this occurrence or end it."
    elif active and occurrence_open:
        state = "manual"
    resumable = bool(
        occurrence and not active and state not in {"stopping", "unverified"}
        and occurrence_can_resume(class_info, occurrence)
    )
    return {
        "is_active": active and state not in {"stopping", "unverified"},
        "session_state": state,
        "session_message": message,
        "resume_available": resumable,
        "occurrence_open": occurrence_open,
    }


def _live_occurrence_for_class(conn, class_info, scheduled_date):
    """Keep a durable occurrence through schedule edits and midnight grace."""
    occurrence = get_open_attendance_session(conn, class_info["id"])
    if occurrence:
        return occurrence
    occurrence = get_attendance_occurrence(
        conn, class_info["id"], scheduled_date, class_info["start_time"],
    )
    if occurrence:
        return occurrence
    latest = get_latest_attendance_session(conn, class_info["id"])
    if occurrence_can_resume(class_info, latest):
        return latest
    if latest:
        from services.session_reopening import eligibility
        try:
            try:
                eligibility(conn, latest, lifecycle.lifecycle(conn, latest['id']), early=True)
            except ValueError:
                eligibility(conn, latest, lifecycle.lifecycle(conn, latest['id']))
            return latest
        except ValueError:
            pass
    return None


def _occurrence_availability(class_info, occurrence, availability):
    """Explain the durable occurrence instead of advertising a new schedule slot."""
    if not occurrence:
        return availability
    availability = dict(availability)
    allowed = occurrence_can_resume(class_info, occurrence)
    if row_value(occurrence, 'policy_version') == 2:
        return {**availability, 'allowed': allowed, 'report_session_id': occurrence['id'],
                'message': ('Interrupted attendance can Resume before its frozen cutoff.' if allowed else
                            'This occurrence uses a fixed attendance cutoff. End is terminal; check the current collection, closure and review status.')}
    availability["allowed"] = allowed
    availability["report_session_id"] = occurrence["id"]
    date = occurrence["scheduled_date"]
    if allowed:
        availability["message"] = (
            f"Attendance for {date} already exists. Continue or resume that saved occurrence; "
            "a second occurrence is not created for the same class date."
        )
    elif occurrence["status"] in {"Cancelled", "Missed"}:
        availability["message"] = f"Attendance for {date} is {occurrence['status'].lower()} and cannot be resumed. View the saved report."
    else:
        availability["message"] = (
            f"Attendance for {date} already exists and its resume window has ended. "
            "View the saved report. A second occurrence cannot be created for this class on that date."
        )
    return availability


def _session_publication_status(class_id, prefix=""):
    """Return non-sensitive stream progress for an active worker context."""
    context = session_manager.active_sessions.get(class_id)
    if context is None:
        return 0, None

    try:
        sequence = max(0, int(context.get(prefix + "publication_sequence", 0) or 0))
    except (TypeError, ValueError, OSError, EOFError, BrokenPipeError):
        sequence = 0

    try:
        published_at = context.get(prefix + "publication_timestamp_monotonic")
        published_at = float(published_at)
        if not math.isfinite(published_at):
            return sequence, None
        age_ms = max(0.0, (time.monotonic() - published_at) * 1000.0)
    except (TypeError, ValueError, OSError, EOFError, BrokenPipeError):
        return sequence, None
    return sequence, round(age_ms, 2)


def _class_stream_access_check(user_id, class_id):
    """Return a fail-closed authorization check for a long-lived class feed."""
    binding = session.get("credential_binding")
    signing_key = current_app.secret_key

    def access_is_current():
        try:
            with managed_transaction(commit=False) as conn:
                authorization = conn.execute(
                    """
                    SELECT u.role, u.password_hash, u.is_active AS user_is_active,
                           u.requires_password_change,
                           c.teacher_id, c.is_active AS class_is_active,
                           c.is_archived
                    FROM users u
                    JOIN classes c ON c.id = ?
                    WHERE u.id = ?
                    """,
                    (class_id, user_id),
                ).fetchone()
                occurrence = get_open_attendance_session(conn, class_id)
                if occurrence and row_value(occurrence, 'policy_version') == 2 and not lifecycle.collection_allowed(occurrence):
                    return False
            worker_is_running = session_manager.is_running(class_id)
        except Exception:
            logger.exception("Failed to revalidate class stream authorization.")
            return False
        return bool(
            authorization
            and credential_matches(user_id, authorization["password_hash"], binding, secret_key=signing_key)
            and authorization["user_is_active"]
            and not authorization["requires_password_change"]
            and has_permission(authorization["role"], "run_attendance_session")
            and authorization["teacher_id"] == user_id
            and authorization["class_is_active"]
            and not authorization["is_archived"]
            and worker_is_running
        )

    return access_is_current


def _display_token_cache():
    cached = session.get("attendance_display_tokens", {})
    return dict(cached) if isinstance(cached, dict) else {}


def _cache_display_token(attendance_session_id, token):
    cached = _display_token_cache()
    cached[str(attendance_session_id)] = token
    session["attendance_display_tokens"] = cached


def _forget_display_token(attendance_session_id):
    if not attendance_session_id:
        return
    cached = _display_token_cache()
    cached.pop(str(attendance_session_id), None)
    session["attendance_display_tokens"] = cached


def _cached_display_url(conn, occurrence):
    if not occurrence:
        return None, False
    grant = active_display_grant(conn, occurrence["id"])
    if not grant:
        _forget_display_token(occurrence["id"])
        return None, False
    token = _display_token_cache().get(str(occurrence["id"]))
    if not token or hash_display_token(token) != grant["grant_hash"]:
        return None, True
    return url_for("entrance.attendance_display", grant_token=token), True


def _check_schedule_conflicts(conn, room_id, teacher_id, days_of_week_list, start_time_str, end_time_str, exclude_class_id=None):
    """Compatibility wrapper around the canonical Wave 4 scheduler."""
    try:
        schedule = canonical_schedule(days_of_week_list, start_time_str, end_time_str)
    except ScheduleValidationError as exc:
        return str(exc)
    return find_schedule_conflict(
        conn,
        room_id,
        teacher_id,
        schedule,
        exclude_class_id=exclude_class_id,
    )


def _begin_class_schedule_write(conn):
    """Serialize only class schedule mutations across SQLite connections."""
    conn.execute("BEGIN IMMEDIATE")


def _eligible_teacher(conn, teacher_id):
    if not isinstance(teacher_id, int) or not 0 < teacher_id <= 9223372036854775807:
        return None
    return conn.execute(
        "SELECT id, username FROM users WHERE id = ? AND role = 'Teacher' AND is_active = 1",
        (teacher_id,),
    ).fetchone()


def _institution_today():
    tz = timezone(timedelta(minutes=config.INSTITUTION_UTC_OFFSET_MINUTES))
    return datetime.now(tz).date()


def _parse_late_marking_minutes(raw):
    try:
        minutes = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Enter a valid whole number of minutes, zero or greater.") from exc
    late_marking_time(datetime.combine(_institution_today(), datetime.min.time()), minutes)
    return minutes


def _class_has_open_occurrence(conn, class_id):
    return get_open_attendance_session(conn, class_id) is not None


@classes_bp.route("/api/rooms/<int:room_id>/schedule", methods=["GET"])
@login_required
@permission_required("manage_classes")
def api_room_schedule(room_id):
    """Return occupied time slots for a given room."""
    exclude_class_id = request.args.get("exclude_class_id")
    if exclude_class_id:
        if (len(exclude_class_id) > 19 or not exclude_class_id.isascii()
                or not exclude_class_id.isdecimal()
                or not 0 < int(exclude_class_id) <= 2**63 - 1):
            return jsonify({"error": "Choose a valid class to exclude from the room schedule."}), 400
        exclude_class_id = int(exclude_class_id)
    with managed_transaction(commit=False) as conn:
        query = """
            SELECT c.id, c.course_name, c.class_code, c.days_of_week, c.start_time, c.end_time,
                   u.username AS teacher_name
            FROM classes c
            JOIN users u ON c.teacher_id = u.id AND u.role = 'Teacher'
            WHERE c.room_id = ? AND c.is_archived = 0
        """
        params = [room_id]
        if exclude_class_id:
            query += " AND c.id != ?"
            params.append(exclude_class_id)

        rows = conn.execute(query, params).fetchall()
        slots = []
        for r in rows:
            slots.append({
                "id": r["id"],
                "course_name": r["course_name"],
                "class_code": r["class_code"],
                "teacher_name": r["teacher_name"],
                "days": r["days_of_week"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
            })
        return jsonify({"room_id": room_id, "slots": slots})


def _render_class_management(conn, *, form_data=None, form_error=None, status=200):
    user_id = session.get("user_id")
    role = session.get("role")
    if role == "Dean":
        classes = conn.execute(
            """
            SELECT c.*, u.username as teacher_name, cd.name AS camera_name,
                   r.room_code AS camera_room, cd.placement_notes AS camera_placement,
                   r.room_code, r.room_name, b.name AS building, f.name AS floor,
                   r.department, r.room_type,
                   cr.room_code AS camera_room_code, cr.room_name AS camera_room_name,
                   COUNT(es.id) AS enrolled_count
            FROM classes c
            JOIN users u ON c.teacher_id = u.id AND u.role = 'Teacher'
            LEFT JOIN camera_devices cd ON cd.room_id = c.room_id AND cd.is_active = 1 AND cd.lifecycle_status = 'Ready'
            LEFT JOIN rooms r ON c.room_id = r.id
            LEFT JOIN floors f ON r.floor_id = f.id
            LEFT JOIN buildings b ON f.building_id = b.id
            LEFT JOIN rooms cr ON cd.room_id = cr.id
            LEFT JOIN class_enrollments ce ON c.id = ce.class_id
            LEFT JOIN students es ON es.id = ce.student_id AND es.is_active = 1 AND es.permanently_removed_at IS NULL
            WHERE c.is_archived = 0
            GROUP BY c.id
            ORDER BY c.id DESC
            """
        ).fetchall()
    else:
        classes = conn.execute(
            """
            SELECT c.*, u.username as teacher_name, cd.name AS camera_name,
                   r.room_code AS camera_room, cd.placement_notes AS camera_placement,
                   r.room_code, r.room_name, b.name AS building, f.name AS floor,
                   r.department, r.room_type,
                   cr.room_code AS camera_room_code, cr.room_name AS camera_room_name,
                   COUNT(es.id) AS enrolled_count
            FROM classes c
            JOIN users u ON c.teacher_id = u.id AND u.role = 'Teacher'
            LEFT JOIN camera_devices cd ON cd.room_id = c.room_id AND cd.is_active = 1 AND cd.lifecycle_status = 'Ready'
            LEFT JOIN rooms r ON c.room_id = r.id
            LEFT JOIN floors f ON r.floor_id = f.id
            LEFT JOIN buildings b ON f.building_id = b.id
            LEFT JOIN rooms cr ON cd.room_id = cr.id
            LEFT JOIN class_enrollments ce ON c.id = ce.class_id
            LEFT JOIN students es ON es.id = ce.student_id AND es.is_active = 1 AND es.permanently_removed_at IS NULL
            WHERE teacher_id = ? AND c.is_archived = 0
            GROUP BY c.id
            ORDER BY c.id DESC
            """,
            (user_id,)
        ).fetchall()
    today = _institution_today().isoformat()
    classes = [class_schedule_view(conn, c, today) for c in classes]
    rooms = [_serialize_room(room) for room in _fetch_active_rooms(conn)]
    teachers = conn.execute(
        "SELECT id, username FROM users WHERE role = 'Teacher' AND is_active = 1 ORDER BY username"
    ).fetchall()
    return render_template("classes/manage.html", classes=classes, rooms=rooms, teachers=teachers,
                           form_data=form_data or {}, form_error=form_error), status


def _render_class_creation(conn, *, form_data=None, form_error=None, status=200):
    rooms = [_serialize_room(room) for room in _fetch_active_rooms(conn)]
    teachers = conn.execute(
        "SELECT id, username FROM users WHERE role = 'Teacher' AND is_active = 1 ORDER BY username"
    ).fetchall()
    return render_template("classes/create.html", rooms=rooms, teachers=teachers,
                           form_data=form_data or {}, form_error=form_error), status


@classes_bp.route("/classes/new", methods=["GET"])
@login_required
@permission_required("manage_classes")
def create_class():
    """Dean-only creation form; saving uses the existing guarded POST /classes."""
    if session.get("role") != "Dean":
        abort(403, description="Only the Dean can create and assign classes.")
    with managed_transaction(commit=False) as conn:
        return _render_class_creation(conn)


@classes_bp.route("/classes", methods=["GET", "POST"])
@login_required
@permission_required("manage_classes")
def manage_classes():
    """Render class management panel and handle class creation.

    Routes:
        GET /classes: Displays list of active classes.
        POST /classes: Creates a Teacher-owned class.

    HTTP Methods:
        GET, POST

    Roles Permitted:
        GET: Dean, Teacher
        POST: Dean (for an assigned Teacher)

    Form Fields (POST):
        course_name (str): Name of the class/subject (e.g. 'Intro to CS').
        class_code (str): Code identifying the class (e.g. 'CS101').
        room_id (int): Assigned active institutional Room.
        days_of_week (list of str): Selected days (e.g. ['Monday', 'Wednesday']).
        start_time (str): Scheduled start time (e.g. '09:00').
        late_threshold_minutes (int, optional): Grace period for lateness. Defaults to 15.

    Database Queries:
        GET (Dean):
            - SELECT c.*, u.username as teacher_name FROM classes c JOIN users u ON c.teacher_id = u.id ORDER BY c.id DESC
        GET (Teacher):
            - SELECT c.*, u.username as teacher_name FROM classes c JOIN users u ON c.teacher_id = u.id WHERE teacher_id = ? ORDER BY c.id DESC
        POST:
            - INSERT INTO classes (teacher_id, course_name, class_code, room_number, days_of_week, start_time, late_threshold_minutes) VALUES (?, ?, ?, ?, ?, ?, ?)

    Security Boundaries:
        - Class Ownership: Every class is assigned to a Teacher, including classes
          created administratively by the Dean.
        - Class Visibility Isolation: Teachers can only view classes they own. Deans can view all classes globally.
    """
    user_id = session.get("user_id")
    role = session.get("role")
    if request.method == "POST" and role != "Dean":
        abort(403, description="Only the Dean can create and assign classes.")
    class_draft = {field: request.form.get(field, "") for field in (
        "teacher_id", "course_name", "class_code", "section", "room_id",
        "start_time", "end_time", "late_threshold_minutes",
    )}
    class_draft["days_of_week"] = request.form.getlist("days_of_week")
    form_error = None
    form_status = 400
    with managed_transaction(commit=True) as conn:
        if request.method == "POST":
            try:
                _begin_class_schedule_write(conn)
            except sqlite3.OperationalError:
                conn.rollback()
                logger.exception("Could not reserve the class schedule for creation.")
                return _render_class_creation(conn, form_data=class_draft, status=503,
                                                form_error="The schedule is temporarily busy. Wait a moment and try again.")
            if role == "Dean":
                raw_teacher_id = request.form.get("teacher_id")
                try:
                    target_teacher_id = int(raw_teacher_id)
                except (TypeError, ValueError):
                    return _render_class_creation(conn, form_data=class_draft,
                                                    form_error="Selected teacher is invalid.", status=400)
                teacher = _eligible_teacher(conn, target_teacher_id)
                if not teacher:
                    return _render_class_creation(conn, form_data=class_draft,
                                                    form_error="Selected teacher is invalid.", status=400)
            else:
                abort(403)

            course_name = " ".join(request.form.get("course_name", "").split())
            class_code = request.form.get("class_code", "").strip()
            section = request.form.get("section", "").strip()
            room_id = request.form.get("room_id", "").strip()
            days_of_week_list = request.form.getlist("days_of_week")
            start_time = request.form.get("start_time", "").strip()
            end_time = request.form.get("end_time", "").strip()
            late_threshold_minutes = request.form.get("late_threshold_minutes", 15)

            resolved_room_id, _ = _resolve_room_from_payload(conn, room_id)
            room_camera = _get_active_room_camera(conn, resolved_room_id)
            camera_id = room_camera["id"] if room_camera else None

            try:
                late_threshold_minutes = _parse_late_marking_minutes(late_threshold_minutes)
            except ValueError as exc:
                late_threshold_minutes = None
                late_error = str(exc)
            try:
                proposed_schedule = canonical_schedule(days_of_week_list, start_time, end_time)
                schedule_error = None
            except ScheduleValidationError as exc:
                proposed_schedule = None
                schedule_error = str(exc)

            if room_id and not resolved_room_id:
                form_error = "Selected Room is inactive or unavailable."
            elif not course_name or not class_code or not resolved_room_id:
                form_error = "All fields except section are required."
            elif schedule_error:
                form_error = schedule_error
            elif late_threshold_minutes is None:
                form_error = late_error
            elif not re.match(r"^[a-zA-Z0-9\-\_]+$", class_code) or len(class_code) > 20:
                form_error = "Class code must be alphanumeric, dashes, or underscores only and up to 20 characters."
            elif section and (not re.match(r"^[a-zA-Z0-9\-\_ ]+$", section) or len(section) > 30):
                form_error = "Section must be alphanumeric, dashes, underscores, or spaces only and up to 30 characters."
            elif normalized_class_code_exists(conn, target_teacher_id, class_code):
                form_error = "A class with this code already exists for the selected Teacher."
                form_status = 409
            else:
                conflict_err = find_schedule_conflict(
                    conn,
                    resolved_room_id,
                    target_teacher_id,
                    proposed_schedule,
                )
                if conflict_err:
                    form_error = conflict_err
                    form_status = 409
                else:
                    try:
                        conn.execute(
                            """
                            INSERT INTO classes
                            (teacher_id, camera_id, room_id, course_name, class_code, section,
                             days_of_week, start_time, end_time, late_threshold_minutes,
                             effective_start_date)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                target_teacher_id,
                                int(camera_id) if camera_id else None,
                                resolved_room_id,
                                course_name,
                                class_code,
                                section if section else 'Section A',
                                proposed_schedule["days_text"],
                                proposed_schedule["start_time"],
                                proposed_schedule["end_time"],
                                late_threshold_minutes,
                                _institution_today().isoformat(),
                            )
                        )
                        flash(f"Class '{course_name}' created successfully.", "success")
                        return redirect(url_for("classes.manage_classes"))
                    except sqlite3.IntegrityError:
                        conn.rollback()
                        form_status = 409
                        form_error = "A class with this code already exists for this teacher."
                    except Exception:
                        conn.rollback()
                        form_status = 500
                        logger.exception("Failed to create class.")
                        form_error = "An error occurred while creating the class."
            return _render_class_creation(conn, form_data=class_draft, form_error=form_error, status=form_status)

        return _render_class_management(conn)


@classes_bp.route("/classes/<int:class_id>", methods=["GET"])
@login_required
@permission_required("manage_classes")
def class_detail(class_id):
    """Read-only Class Detail Page."""
    user_id = session.get("user_id")
    role = session.get("role")
    with managed_transaction(commit=True) as conn:
        class_info = conn.execute(
            """
            SELECT c.*, u.username as teacher_name, cd.name AS camera_name,
                   r.room_code AS camera_room, cd.placement_notes AS camera_placement,
                   r.room_code, r.room_name, b.name AS building, f.name AS floor,
                   r.department, r.room_type,
                   cr.room_code AS camera_room_code, cr.room_name AS camera_room_name,
                   COUNT(es.id) AS enrolled_count
            FROM classes c
            JOIN users u ON c.teacher_id = u.id AND u.role = 'Teacher'
            LEFT JOIN camera_devices cd ON cd.room_id = c.room_id AND cd.is_active = 1 AND cd.lifecycle_status = 'Ready'
            LEFT JOIN rooms r ON c.room_id = r.id
            LEFT JOIN floors f ON r.floor_id = f.id
            LEFT JOIN buildings b ON f.building_id = b.id
            LEFT JOIN rooms cr ON cd.room_id = cr.id
            LEFT JOIN class_enrollments ce ON c.id = ce.class_id
            LEFT JOIN students es ON es.id = ce.student_id AND es.is_active = 1 AND es.permanently_removed_at IS NULL
            WHERE c.id = ?
            GROUP BY c.id
            """,
            (class_id,)
        ).fetchone()

        if not class_info:
            flash("Class not found.", "error")
            return redirect(url_for("classes.manage_classes"))

        if role != "Dean" and class_info["teacher_id"] != user_id:
            abort(403)
        sessions = conn.execute(
            """
            SELECT id, scheduled_date, scheduled_start_time, actual_started_at, actual_ended_at, status
            FROM attendance_sessions
            WHERE class_id = ?
            ORDER BY scheduled_date DESC, scheduled_start_time DESC
            LIMIT 10
            """,
            (class_id,)
        ).fetchall()

        from services.session_agenda import saved_presentation
        sessions = [dict(s, **saved_presentation(conn, s)) for s in sessions]
        session_availability = session_window_for_class(class_info)
        occurrence = _live_occurrence_for_class(conn, class_info, session_availability["scheduled_date"])
        session_availability = _occurrence_availability(class_info, occurrence, session_availability)
        class_info = class_schedule_view(conn, class_info, _institution_today().isoformat())

    return render_template(
        "classes/detail.html", class_info=class_info, sessions=sessions,
        session_availability=session_availability,
    )

def _render_class_edit(conn, class_info, *, form_data=None, form_error=None, status=200):
    view = dict(class_info)
    if session.get('role') != 'Dean':
        # Never echo forged class details as if they were the saved context.
        if form_data is not None:
            view['late_threshold_minutes'] = form_data['late_threshold_minutes']
        return render_template('classes/late_marking.html', class_info=view,
                               form_error=form_error), status
    if form_data is not None:
        view.update(form_data)
    rooms = [_serialize_room(room) for room in _fetch_active_rooms(conn)]
    teachers = conn.execute(
        "SELECT id, username FROM users WHERE role = 'Teacher' AND is_active = 1 ORDER BY username"
    ).fetchall()
    today = _institution_today()
    effective_date = schedule_edit_effective_date(conn, class_info['id'], today)
    return render_template("classes/edit.html", class_info=view, rooms=rooms, teachers=teachers,
                           form_error=form_error,
                           schedule_view=class_schedule_view(conn, class_info, today.isoformat()),
                           schedule_effective_from=effective_date.isoformat(),
                           schedule_deferred=effective_date > today), status


@classes_bp.route("/classes/<int:class_id>/edit", methods=["GET", "POST"])
@login_required
@permission_required("manage_classes")
def edit_class(class_id):
    """Edit a class schedule and authoritative Room assignment."""
    user_id = session.get("user_id")
    role = session.get("role")
    form_data = {field: request.form.get(field, "") for field in (
        "teacher_id", "course_name", "class_code", "section", "room_id",
        "start_time", "end_time", "late_threshold_minutes",
    )}
    form_data["days_of_week"] = request.form.getlist("days_of_week")
    with managed_transaction(commit=True) as conn:
        schedule_busy = False
        if request.method == "POST":
            try:
                _begin_class_schedule_write(conn)
            except sqlite3.OperationalError:
                conn.rollback()
                schedule_busy = True
                logger.exception("Could not reserve the class schedule for editing.")
        class_info = conn.execute(
            """
            SELECT c.*, u.username AS teacher_name
            FROM classes c
            JOIN users u ON c.teacher_id = u.id AND u.role = 'Teacher'
            WHERE c.id = ?
            """,
            (class_id,),
        ).fetchone()
        if not class_info:
            flash("Class not found.", "error")
            return redirect(url_for("manage_classes"))
        if role != "Dean" and class_info["teacher_id"] != user_id:
            abort(403)
        if class_info["is_archived"]:
            flash("Archived classes are read-only. Restore the class before editing it.", "warning")
            return redirect(url_for("classes.class_detail", class_id=class_id))

        if request.method == "POST":
            if role != "Dean" or "teacher_id" not in request.form:
                form_data["teacher_id"] = str(class_info["teacher_id"])

            def reject(message, status=400):
                return _render_class_edit(conn, class_info, form_data=form_data,
                                          form_error=message, status=status)

            if schedule_busy:
                return reject("The schedule is temporarily busy. Wait a moment and try again.", 503)
            if role != "Dean":
                # A narrow write avoids re-deriving the camera, normalizing class
                # details, or touching schedule history through a Teacher request.
                if set(request.form) - {'csrf_token', 'late_threshold_minutes'}:
                    return reject("Only the Dean can change class details, the assigned room or schedule. You can change the late-marking time here.", 403)
                values = request.form.getlist('late_threshold_minutes')
                try:
                    if len(values) != 1:
                        raise ValueError("Enter a valid whole number of minutes, zero or greater.")
                    minutes = _parse_late_marking_minutes(values[0])
                except ValueError as exc:
                    return reject(str(exc))
                try:
                    conn.execute('UPDATE classes SET late_threshold_minutes = ? WHERE id = ?',
                                 (minutes, class_id))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    logger.exception("Could not save the class late-marking setting.")
                    return reject("The late-marking setting could not be saved. Try again.", 500)
                flash("Late-marking setting saved. Sessions already started keep their existing timing.", "success")
                return redirect(url_for("manage_classes"))
            if role == "Dean":
                raw_teacher_id = request.form.get("teacher_id")
                if raw_teacher_id is not None and str(raw_teacher_id).strip() != "":
                    try:
                        parsed_t_id = int(raw_teacher_id)
                        t_row = _eligible_teacher(conn, parsed_t_id)
                        if not t_row:
                            return reject("Selected teacher is invalid.")
                        target_teacher_id = parsed_t_id
                    except (ValueError, TypeError):
                        return reject("Selected teacher is invalid.")
                else:
                    target_teacher_id = class_info["teacher_id"]
            else:
                target_teacher_id = class_info["teacher_id"]

            course_name = " ".join(request.form.get("course_name", "").split())
            class_code = request.form.get("class_code", "").strip()
            section = request.form.get("section", "").strip()
            room_id = request.form.get("room_id", "").strip()
            days_of_week_list = request.form.getlist("days_of_week")
            start_time = request.form.get("start_time", "").strip()
            end_time = request.form.get("end_time", "").strip()
            late_threshold_minutes = request.form.get("late_threshold_minutes", 15)
            
            resolved_room_id, _ = _resolve_room_from_payload(conn, room_id)
            room_camera = _get_active_room_camera(conn, resolved_room_id)
            camera_id = room_camera["id"] if room_camera else None

            try:
                late_threshold_minutes = _parse_late_marking_minutes(late_threshold_minutes)
            except ValueError as exc:
                late_threshold_minutes = None
                late_error = str(exc)
            try:
                proposed_schedule = canonical_schedule(days_of_week_list, start_time, end_time)
                schedule_error = None
            except ScheduleValidationError as exc:
                proposed_schedule = None
                schedule_error = str(exc)

            if room_id and not resolved_room_id:
                return reject("Selected Room is inactive or unavailable.")
            elif not course_name or not class_code or not resolved_room_id:
                return reject("All fields except section are required.")
            elif schedule_error:
                return reject(schedule_error)
            elif late_threshold_minutes is None:
                return reject(late_error)
            elif not re.match(r"^[a-zA-Z0-9\-\_]+$", class_code) or len(class_code) > 20:
                return reject("Class code must be alphanumeric, dashes, or underscores only and up to 20 characters.")
            elif section and (not re.match(r"^[a-zA-Z0-9\-\_ ]+$", section) or len(section) > 30):
                return reject("Section must be alphanumeric, dashes, underscores, or spaces only and up to 30 characters.")
            elif normalized_class_code_exists(conn, target_teacher_id, class_code, exclude_class_id=class_id):
                return reject("A class with this code already exists for the selected Teacher.", 409)
            elif (target_teacher_id != class_info["teacher_id"] or resolved_room_id != class_info["room_id"]) and (
                class_info["is_active"] or _class_has_open_occurrence(conn, class_id)
            ):
                return reject("End the open attendance session before changing its Teacher or Room.", 409)
            else:
                conflict_err = find_schedule_conflict(
                    conn,
                    resolved_room_id,
                    target_teacher_id,
                    proposed_schedule,
                    exclude_class_id=class_id,
                )
                if conflict_err:
                    return reject(conflict_err, 409)
                else:
                    try:
                        conn.execute("SAVEPOINT class_edit_write")
                        try:
                            existing_schedule = canonical_schedule(
                                class_info["days_of_week"],
                                class_info["start_time"],
                                class_info["end_time"],
                            )
                        except ScheduleValidationError:
                            existing_schedule = None
                        schedule_changed = not existing_schedule or any(
                            existing_schedule[key] != proposed_schedule[key]
                            for key in ("days_text", "start_time", "end_time")
                        )
                        if schedule_changed:
                            today = _institution_today()
                            schedule_effective_from = schedule_edit_effective_date(conn, class_id, today)
                        if target_teacher_id != class_info['teacher_id']:
                            from services.attendance_display import revoke_class_displays
                            revoke_class_displays(conn, class_id, user_id, 'teacher_reassigned')
                        conn.execute(
                            """
                            UPDATE classes
                            SET teacher_id = ?,
                                camera_id = ?,
                                room_id = ?,
                                course_name = ?,
                                class_code = ?,
                                section = ?,
                                days_of_week = ?,
                                start_time = ?,
                                end_time = ?,
                                late_threshold_minutes = ?
                            WHERE id = ?
                            """,
                            (
                                target_teacher_id,
                                camera_id,
                                resolved_room_id,
                                course_name,
                                class_code,
                                section if section else 'Section A',
                                proposed_schedule["days_text"],
                                proposed_schedule["start_time"],
                                proposed_schedule["end_time"],
                                late_threshold_minutes,
                                class_id,
                            ),
                        )
                        if schedule_changed:
                            transition_class_schedule(
                                conn,
                                class_info,
                                proposed_schedule,
                                schedule_effective_from.isoformat(),
                            )
                        if target_teacher_id != class_info["teacher_id"]:
                            log_system_event(
                                conn,
                                "class_teacher_reassigned",
                                actor_user_id=user_id,
                                details={
                                    "class_id": class_id,
                                    "previous_teacher_id": class_info["teacher_id"],
                                    "new_teacher_id": target_teacher_id,
                                },
                            )
                        conn.execute("RELEASE SAVEPOINT class_edit_write")
                        conn.commit()
                    except sqlite3.IntegrityError:
                        conn.rollback()
                        return reject("A class with this code already exists for this teacher.", 409)
                    except Exception:
                        conn.rollback()
                        logger.exception("Could not save the class edit.")
                        return reject("The class could not be saved. Try again.", 500)
                    if schedule_changed:
                        flash(render_template(
                            'classes/_schedule_saved.txt', schedule=proposed_schedule,
                            effective_from=schedule_effective_from.isoformat(),
                            deferred=schedule_effective_from > today,
                        ).strip(), 'success')
                    else:
                        flash("Class updated successfully.", "success")
                    return redirect(url_for("manage_classes"))

        return _render_class_edit(conn, class_info)





@classes_bp.route("/classes/<int:class_id>/enrollments", methods=["GET"])
@login_required
@permission_required("manage_rosters")
def class_enrollments(class_id):
    """Render the dual-pane roster enrollments management interface.

    Allows adding or removing students to/from a class roster.

    Routes:
        GET /classes/<int:class_id>/enrollments

    HTTP Methods:
        GET

    Roles Permitted:
        Dean, Teacher

    Parameters:
        class_id (int): Primary key ID of the class to manage.

    Database Queries:
        - SELECT * FROM classes WHERE id = ?
        - SELECT s.id, s.student_id, s.name FROM students s JOIN class_enrollments ce ON s.id = ce.student_id WHERE ce.class_id = ? ORDER BY s.name ASC
        - SELECT id, student_id, name FROM students WHERE id NOT IN (SELECT student_id FROM class_enrollments WHERE class_id = ?) ORDER BY name ASC

    Security Boundaries:
        - Teacher Isolation: A Teacher can only access this page if they are the owner of the class (i.e. `class_info["teacher_id"] == user_id`). Attempting to access another teacher's class roster returns a 403 forbidden error.
        - Dean Privilege: Deans bypass ownership checks and can access any class roster.
    """
    user_id = session.get("user_id")
    role = session.get("role")
    
    with managed_transaction(commit=False) as conn:
        # Validate ownership
        class_info = conn.execute(
            "SELECT * FROM classes WHERE id = ?", (class_id,)
        ).fetchone()
        
        if not class_info:
            flash("Class not found.", "error")
            return redirect(url_for("manage_classes"))
            
        if role != "Dean" and class_info["teacher_id"] != user_id:
            logger.warning("Teacher %s attempted to access enrollments for class %s unauthorized.", user_id, class_id)
            return "You do not have permission to manage this class.", 403
        if class_info["is_archived"]:
            flash("Archived classes are read-only. Restore this class to manage its roster.", "warning")
            return redirect(url_for("classes.class_detail", class_id=class_id))

        # Fetch currently enrolled students
        enrolled_students = conn.execute(
            """
            SELECT s.id, s.student_id, s.name, s.is_active,
                   s.permanently_removed_at,
                   EXISTS(
                       SELECT 1 FROM attendance_records ar
                       WHERE ar.class_id = ? AND ar.student_id = s.id
                   ) AS has_attendance_history
            FROM students s
            JOIN class_enrollments ce ON s.id = ce.student_id
            WHERE ce.class_id = ?
            ORDER BY s.name ASC
            """,
            (class_id, class_id)
        ).fetchall()

        programs = conn.execute("SELECT * FROM academic_programs WHERE is_active = 1 ORDER BY name ASC").fetchall()
        roster_locked = bool(class_info["is_active"] or _class_has_open_occurrence(conn, class_id))

    return render_template(
        "classes/roster.html", 
        class_info=class_info, 
        enrolled=enrolled_students, 
        available=[],
        programs=programs,
        roster_locked=roster_locked,
    )


@classes_bp.route("/api/classes/<int:class_id>/enrollments", methods=["POST"])
@login_required
@permission_required("manage_rosters")
def api_class_enrollments(class_id):
    """API endpoint to atomically add or remove a student from a class roster.

    Routes:
        POST /api/classes/<int:class_id>/enrollments

    HTTP Methods:
        POST

    Roles Permitted:
        Dean, Teacher

    Parameters:
        class_id (int): Primary key ID of the class to update.

    JSON Payload (POST):
        action (str): Roster action to perform, either 'add' or 'remove'.
        student_id (int): Database primary key (id) of the target student.

    Database Queries:
        - SELECT teacher_id FROM classes WHERE id = ?
        - Action 'add':
            - INSERT INTO class_enrollments (class_id, student_id) VALUES (?, ?)
        - Action 'remove':
            - DELETE FROM class_enrollments WHERE class_id = ? AND student_id = ?

    Security Boundaries:
        - Teacher Isolation: A Teacher can only modify class rosters for classes they own. Modifying another teacher's class roster returns a 403 Forbidden JSON error.
        - Dean Privilege: Deans bypass ownership checks and can modify any class roster.
        - Duplicate Protection: Attempts to add a student already enrolled are caught by database UNIQUE constraints and return a 400 Bad Request.
    """
    user_id = session.get("user_id")
    role = session.get("role")
    data = request.get_json()
    
    if not isinstance(data, dict) or not data:
        return jsonify({"error": "Invalid JSON payload."}), 400
        
    action = data.get("action")
    target_student_id = parse_record_id(data.get("student_id"))
    
    if action not in ["add", "remove"] or not target_student_id:
        return jsonify({"error": "Invalid action or missing student_id."}), 400

    try:
        with managed_transaction(commit=True) as conn:
            # Serialize ownership and open-occurrence checks with attendance
            # start/archive/student status writes before changing the roster.
            conn.execute("BEGIN IMMEDIATE")
            # Validate ownership
            class_info = conn.execute(
                "SELECT teacher_id, is_archived, is_active FROM classes WHERE id = ?", (class_id,)
            ).fetchone()
        
            if not class_info:
                return jsonify({"error": "Class not found."}), 404
            
            if role != "Dean" and class_info["teacher_id"] != user_id:
                return jsonify({"error": "Forbidden"}), 403
            if class_info["is_archived"]:
                return jsonify({"error": "Archived classes are read-only. Restore the class first."}), 409
            if class_info["is_active"] or _class_has_open_occurrence(conn, class_id):
                return jsonify({"error": "Roster changes are locked while attendance is open."}), 409

            if action == "add":
                active_student = conn.execute(
                    """
                    SELECT id FROM students
                    WHERE id = ? AND is_active = 1 AND permanently_removed_at IS NULL
                    """,
                    (target_student_id,),
                ).fetchone()
                if not active_student:
                    return jsonify({"error": "Only active students can be added to a roster."}), 409
                try:
                    conn.execute(
                        "INSERT INTO class_enrollments (class_id, student_id) VALUES (?, ?)",
                        (class_id, target_student_id)
                    )
                    message = "Student added to roster."
                except Exception as e:
                    if "UNIQUE constraint failed" in str(e):
                        return jsonify({"error": "Student already enrolled."}), 400
                    raise
            elif action == "remove":
                cursor = conn.execute(
                    "DELETE FROM class_enrollments WHERE class_id = ? AND student_id = ?",
                    (class_id, target_student_id)
                )
                if not cursor.rowcount:
                    return jsonify({"error": "Student is not currently enrolled."}), 404
                message = "Student removed from roster. Historical occurrences were preserved."
            
            return jsonify({"message": message, "student_id": target_student_id, "action": action}), 200

    except Exception as e:
        logger.exception("Error updating enrollments.")
        return jsonify({"error": "An internal server error occurred."}), 500
@classes_bp.route("/api/classes/<int:class_id>/enrollments/batch", methods=["POST"])
@login_required
@permission_required("manage_rosters")
def api_class_enrollments_batch(class_id):
    """Batch-enroll active students into a class roster."""
    user_id = session.get("user_id")
    role = session.get("role")
    data = request.get_json()

    if not isinstance(data, dict) or not data:
        return jsonify({"error": "Invalid JSON payload."}), 400

    raw_student_ids = data.get("student_ids", [])
    if not isinstance(raw_student_ids, list) or len(raw_student_ids) > 500:
        return jsonify({"error": "Select a list of no more than 500 students at a time."}), 400

    normalized_ids = []
    for raw_id in raw_student_ids:
        student_pk = parse_record_id(raw_id)
        if student_pk is None:
            return jsonify({"error": "Every selected student must have a valid record ID. Reload the roster and select again."}), 400
        if student_pk not in normalized_ids:
            normalized_ids.append(student_pk)

    if not normalized_ids:
        return jsonify({"error": "Select at least one valid student."}), 400

    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            class_info = conn.execute(
                "SELECT teacher_id, is_archived, is_active FROM classes WHERE id = ?",
                (class_id,),
            ).fetchone()

            if not class_info:
                return jsonify({"error": "Class not found."}), 404

            if role != "Dean" and class_info["teacher_id"] != user_id:
                return jsonify({"error": "Forbidden"}), 403
            if class_info["is_archived"]:
                return jsonify({"error": "Archived classes are read-only. Restore the class first."}), 409
            if class_info["is_active"] or _class_has_open_occurrence(conn, class_id):
                return jsonify({"error": "Roster changes are locked while attendance is open."}), 409

            placeholders = ",".join("?" for _ in normalized_ids)
            valid_rows = conn.execute(
                f"""
                SELECT id FROM students
                WHERE id IN ({placeholders})
                  AND is_active = 1
                  AND permanently_removed_at IS NULL
                """,
                normalized_ids,
            ).fetchall()
            valid_student_ids = {row["id"] for row in valid_rows}

            if not valid_student_ids:
                return jsonify({"error": "No active students were found for the selected IDs."}), 400

            addable_ids = [
                student_id
                for student_id in normalized_ids
                if student_id in valid_student_ids
            ]

            cursor = conn.cursor()
            cursor.executemany(
                "INSERT OR IGNORE INTO class_enrollments (class_id, student_id) VALUES (?, ?)",
                [(class_id, student_id) for student_id in addable_ids]
            )

            added_count = cursor.rowcount
            skipped_count = len(normalized_ids) - added_count
            # Return confirmed membership, including requested active students
            # already enrolled by another tab. Counts alone cannot identify rows.
            enrolled_ids = {row["student_id"] for row in conn.execute(
                f"""SELECT ce.student_id FROM class_enrollments ce
                    JOIN students s ON s.id = ce.student_id
                    WHERE ce.class_id = ? AND ce.student_id IN ({placeholders})
                      AND s.is_active = 1 AND s.permanently_removed_at IS NULL""",
                [class_id, *normalized_ids],
            ).fetchall()}

            return jsonify({
                "message": f"{added_count} student(s) enrolled. {skipped_count} skipped.",
                "added_count": added_count,
                "skipped_count": skipped_count,
                "enrolled_student_ids": [sid for sid in normalized_ids if sid in enrolled_ids],
            }), 200
    except Exception:
        logger.exception("Error batch updating enrollments.")
        return jsonify({"error": "An internal server error occurred."}), 500
# ---------------------------------------------------------------------------
# Routes -- Live Session & Streaming
# ---------------------------------------------------------------------------

@classes_bp.route("/classes/<int:class_id>/session", methods=["GET"])
@login_required
@permission_required("run_attendance_session")
@_bounded_runtime_read
def class_session(class_id):
    """Render the Live Session UI."""
    user_id = session.get("user_id")
    with session_manager.lifecycle_guard(), managed_transaction(commit=True) as conn:
        class_info = _class_with_teacher_and_camera(conn, class_id)
        if not class_info:
            flash("Class not found.", "error")
            return redirect(url_for("manage_classes"))
            
        if class_info["teacher_id"] != user_id:
            flash("Unauthorized access.", "error")
            return redirect(url_for("manage_classes"))
        if class_info["is_archived"]:
            flash("Archived classes do not have an operational attendance workspace.", "warning")
            return redirect(url_for("classes.class_session_history", class_id=class_id))
            
        class_info = dict(class_info)
        class_info["camera_id"] = class_info.get("resolved_camera_id")
        has_camera = bool(class_info["camera_id"] and class_info.get("camera_name"))
        
        session_availability = session_window_for_class(class_info)
        occurrence = _live_occurrence_for_class(conn, class_info, session_availability["scheduled_date"])

        intended_date = request.args.get('session_date')
        intended_id = request.args.get('session_id')
        resolved_date = occurrence['scheduled_date'] if occurrence else session_availability['scheduled_date']
        if intended_id is not None and (not occurrence or str(occurrence['id']) != intended_id):
            selected = conn.execute('SELECT id FROM attendance_sessions WHERE id=? AND class_id=?', (intended_id, class_id)).fetchone()
            if not selected:
                abort(404)
            flash('This link refers to a different saved session. Its results are shown below; no attendance was started.', 'info')
            return redirect(url_for('classes.class_session_report', class_id=class_id, attendance_session_id=selected['id']))
        if intended_date and intended_date != resolved_date:
            flash('The selected date is not the current attendance session. Review its dated details below.', 'info')
            return redirect(url_for('classes.session_preview', class_id=class_id, date=intended_date))

        if occurrence and not row_value(occurrence, 'policy_version') and occurrence["status"] == "Open" and not occurrence_can_resume(class_info, occurrence):
            close_attendance_session(conn, occurrence["id"], class_id, "resume_window_elapsed")
            conn.execute("UPDATE classes SET is_active = 0 WHERE id = ?", (class_id,))
            class_info["is_active"] = 0
            occurrence = conn.execute(
                "SELECT * FROM attendance_sessions WHERE id = ?", (occurrence["id"],)
            ).fetchone()
            if occurrence["scheduled_date"] != session_availability["scheduled_date"]:
                occurrence = _live_occurrence_for_class(conn, class_info, session_availability["scheduled_date"])

        if class_info["is_active"] == 1 and not occurrence:
            conn.execute("UPDATE classes SET is_active = 0 WHERE id = ?", (class_id,))
            class_info = dict(class_info)
            class_info["is_active"] = 0

        # Legacy reconciliation can retire an old occurrence. Never let that
        # silently move a dated link into a different meeting.
        if intended_id and (not occurrence or str(occurrence['id']) != intended_id):
            return redirect(url_for('classes.class_session_report', class_id=class_id, attendance_session_id=intended_id))
        if intended_date and intended_date != (occurrence['scheduled_date'] if occurrence else session_availability['scheduled_date']):
            return redirect(url_for('classes.session_preview', class_id=class_id, date=intended_date))
        runtime = _reconcile_session_runtime(conn, class_info, occurrence)
        class_info["is_active"] = runtime["is_active"]
        resume_available = runtime["resume_available"]
        if occurrence:
            has_camera = occurrence["camera_id"] is not None
        session_availability = _occurrence_availability(class_info, occurrence, session_availability)
        institution_zone = timezone(timedelta(minutes=config.INSTITUTION_UTC_OFFSET_MINUTES))
        scheduled_start = (
            datetime.fromisoformat(f"{occurrence['scheduled_date']}T{occurrence['scheduled_start_time']}")
            if occurrence else session_availability["scheduled_start"]
        ).replace(tzinfo=institution_zone).isoformat()
        resume_deadline = (
            occurrence_deadline(class_info, occurrence["scheduled_date"], scheduled_end_time=occurrence["scheduled_end_time"])
            if occurrence else session_availability["resume_deadline"]
        ).replace(tzinfo=institution_zone).isoformat()
        if row_value(occurrence, 'policy_version') == 2:
            resume_deadline = datetime.fromtimestamp(occurrence['cutoff'], timezone.utc).isoformat()
        display_url, display_enabled = _cached_display_url(conn, occurrence)
    return render_template(
        "session.html",
        class_info=class_info,
        duration_mins=config.SESSION_DURATION_MINUTES,
        resume_grace_mins=config.SESSION_RESUME_GRACE_MINUTES,
        session_availability=session_availability,
        active_session=occurrence,
        resume_available=resume_available,
        has_camera=has_camera,
        display_enabled=display_enabled,
        display_url=display_url,
        session_runtime=runtime,
        resume_deadline=resume_deadline,
        scheduled_start=scheduled_start,
        session_date=occurrence['scheduled_date'] if occurrence else session_availability['scheduled_date'],
    )


def _requested_display_occurrence(conn, class_id):
    payload = request.get_json(silent=True) or {}
    sid = payload.get('session_id') if isinstance(payload, dict) else None
    if sid is None:
        return get_open_attendance_session(conn, class_id)  # Legacy open-page compatibility.
    if isinstance(sid, bool) or not isinstance(sid, int):
        return None
    return conn.execute('SELECT * FROM attendance_sessions WHERE id=? AND class_id=?', (sid, class_id)).fetchone()


@classes_bp.route("/api/classes/<int:class_id>/session/display/enable", methods=["POST"])
@login_required
@permission_required("run_attendance_session")
def api_enable_attendance_display(class_id):
    actor_user_id = session.get("user_id")
    try:
        with managed_transaction(commit=True) as conn:
            # A pre-reset request must not mint a new bearer grant after reset
            # retires the existing ones. Serialize issuance with credential reset.
            conn.execute("BEGIN IMMEDIATE")
            actor = authenticated_session_user(conn, session)
            if not actor:
                session.clear()
                return jsonify({"error": "Your login session expired.",
                                "session_expired": True,
                                "login_url": url_for("auth.login")}), 401
            if actor["requires_password_change"] or not has_permission(actor["role"], "run_attendance_session"):
                return jsonify({"error": "Forbidden"}), 403
            class_info = conn.execute(
                "SELECT teacher_id, is_archived FROM classes WHERE id = ?", (class_id,)
            ).fetchone()
            if not class_info:
                return jsonify({"error": "Class not found."}), 404
            if class_info["teacher_id"] != actor_user_id:
                return jsonify({"error": "Forbidden"}), 403
            if class_info["is_archived"]:
                return jsonify({"error": "Archived classes are non-operational."}), 409
            occurrence = _requested_display_occurrence(conn, class_id)
            if not occurrence:
                return jsonify({"error": "Select the exact attendance occurrence before enabling its results display."}), 409
            token = enable_display_grant(conn, occurrence["id"], actor_user_id)
            log_system_event(
                conn,
                "attendance_display_enabled",
                actor_user_id,
                {"attendance_session_id": occurrence["id"], "class_id": class_id},
            )
        _cache_display_token(occurrence["id"], token)
        return jsonify(
            {
                "display_enabled": True,
                "attendance_session_id": occurrence["id"],
                "display_url": url_for(
                    "entrance.attendance_display",
                    grant_token=token,
                ),
            }
        )
    except DisplayGrantForbidden as exc:
        return jsonify({"error": str(exc)}), 403
    except DisplayGrantNotFound as exc:
        return jsonify({"error": str(exc)}), 409


@classes_bp.route("/api/classes/<int:class_id>/session/display/deactivate", methods=["POST"])
@login_required
@permission_required("run_attendance_session")
def api_deactivate_attendance_display(class_id):
    actor_user_id = session.get("user_id")
    with managed_transaction(commit=True) as conn:
        class_info = conn.execute(
            "SELECT teacher_id, is_active, is_archived FROM classes WHERE id = ?", (class_id,)
        ).fetchone()
        if not class_info:
            return jsonify({"error": "Class not found."}), 404
        if str(class_info["teacher_id"]) != str(actor_user_id):
            return jsonify({"error": "Forbidden"}), 403
        if class_info["is_archived"]:
            return jsonify({"error": "Archived classes are non-operational."}), 409
        occurrence = _requested_display_occurrence(conn, class_id)
        if not occurrence:
            return jsonify({"display_enabled": False, "message": "Attendance Display is inactive."})
        try:
            revoke_owned_display_grants(conn, occurrence["id"], actor_user_id)
        except DisplayGrantForbidden as exc:
            return jsonify({"error": str(exc)}), 403
        except DisplayGrantNotFound as exc:
            return jsonify({"error": str(exc)}), 409
        log_system_event(
            conn,
            "attendance_display_deactivated",
            actor_user_id,
            {"attendance_session_id": occurrence["id"], "class_id": class_id},
        )
    _forget_display_token(occurrence["id"])
    return jsonify(
        {
            "display_enabled": False,
            "attendance_session_id": occurrence["id"],
            "message": "Display cleared. Attendance records are unchanged.",
        }
    )

def _start_snapshot(conn, sid):
    occurrence = conn.execute('SELECT * FROM attendance_sessions WHERE id=?', (sid,)).fetchone()
    state = lifecycle.snapshot(conn, sid)
    startup = state.get('startup')
    mode = startup['mode'] if startup else ('camera' if occurrence['camera_id'] else 'manual')
    phase = 'starting' if state['collection'] == 'starting' else ('active' if mode == 'camera' else 'manual') if state['collection_allowed'] else 'interrupted' if state['collection'] == 'interrupted' else 'inactive'
    return {'session_id': sid, 'actual_started_at': occurrence['actual_started_at'],
            'mode': mode, 'worker_mode': mode, 'session_state': phase,
            'is_active': state['collection_allowed'], 'occurrence_open': state['end_available'],
            'resume_available': state['resume_available'], 'lifecycle': state,
            'startup': startup, 'message': state['message'], 'session_message': state['message']}


@classes_bp.route('/api/classes/<int:class_id>/session/readiness', methods=['GET'])
@login_required
@permission_required('run_attendance_session')
def api_session_readiness(class_id):
    try:
        with managed_transaction(commit=False) as conn:
            result, _ = session_start.readiness(conn, class_id, session.get('user_id'),
                request.args.get('mode', 'camera'), request.args.get('action', 'start'), current_app.secret_key,
                occurrence_id=request.args.get('session_id', type=int),
                expected_date=request.args.get('session_date'))
        return jsonify(result)
    except session_start.StartError as exc:
        return jsonify(error=str(exc)), exc.status
    except Exception:
        return jsonify(error='Readiness could not be checked. No attendance was started. Retry the check.'), 503


@classes_bp.route('/api/classes/<int:class_id>/session/start/reconcile', methods=['POST'])
@login_required
@permission_required('run_attendance_session')
def api_reconcile_start(class_id):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify(error='A JSON object is required.'), 400
    try:
        with lifecycle.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            actor = session.get('user_id')
            session_start.class_row(conn, class_id, actor)
            op = session_start.operation(conn, payload.get('operation_id'), actor, class_id)
            if op:
                return jsonify(outcome='accepted', operation_id=op['operation_id'], **_start_snapshot(conn, op['session_id']))
            review = session_start.decode_review(payload.get('review_token'), current_app.secret_key, actor, class_id)
            if session_start.expired(review):
                return jsonify(outcome='not_accepted', message='The reviewed Start expired without acceptance. Review the current details and confirm again.')
            return jsonify(outcome='unknown', message='Start has not been confirmed. Waiting for the original request to settle; no new Start was sent.'), 202
    except session_start.StartError as exc:
        return jsonify(error=str(exc)), exc.status
    except sqlite3.Error:
        return jsonify(outcome='unknown', error='Start outcome is unconfirmed. Reload to verify before another action.'), 503


@classes_bp.route('/api/classes/<int:class_id>/session/start', methods=['POST'])
@login_required
@permission_required('run_attendance_session')
def api_start_session(class_id):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify(error='A JSON object is required.'), 400
    # Retain only the pre-v2 Open continuation protocol. New occurrence creation
    # always requires the reviewed command, including on older browser pages.
    if not payload:
        with managed_transaction(commit=False) as conn:
            info = conn.execute('SELECT * FROM classes WHERE id=?', (class_id,)).fetchone()
            old = get_open_attendance_session(conn, class_id) if info else None
            legacy = bool(old and not row_value(old, 'policy_version') and occurrence_can_resume(info, old))
        if legacy:
            return _legacy_api_start_session(class_id)
    try:
        with lifecycle.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            sid = session_start.accept(conn, class_id, session.get('user_id'), payload, current_app.secret_key)
        with lifecycle.connection() as conn:
            result = _start_snapshot(conn, sid)
        return jsonify(operation_id=payload.get('operation_id'), outcome='accepted', **result), 202 if result['session_state'] == 'starting' else 200
    except session_start.StartError as exc:
        return jsonify(error=str(exc), outcome='not_accepted', selected_mode=payload.get('mode')), exc.status
    except (sqlite3.Error, ValueError):
        return jsonify(error='Start outcome cannot be confirmed. Check this operation before trying another Start.', outcome_unknown=True), 503


def _legacy_api_start_session(class_id):
    """Starts the background OpenCV AI loop for the class."""
    user_id = session.get("user_id")
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({'error': 'A JSON object is required.'}), 400

    # Cross-system class lifecycle operations always acquire the worker
    # lifecycle guard before SQLite. Archive uses the same order, so a start
    # can never hold BEGIN IMMEDIATE while waiting for archive's worker lock.
    with session_manager.lifecycle_guard():
        # Tuner shutdown may wait for native capture containment. Finish the
        # authorization/schedule read before stopping it, outside any SQLite
        # transaction, while retaining the camera lifecycle guard.
        with managed_transaction(commit=False) as conn:
            preflight = conn.execute(
                """SELECT c.*, cd.id AS resolved_camera_id FROM classes c
                   LEFT JOIN camera_devices cd ON cd.room_id = c.room_id AND cd.is_active = 1 AND cd.lifecycle_status = 'Ready'
                   WHERE c.id = ?""", (class_id,),
            ).fetchone()
            preflight_allowed = False
            if preflight and preflight["teacher_id"] == user_id and not preflight["is_archived"]:
                window = session_window_for_class(preflight)
                occurrence = _live_occurrence_for_class(conn, preflight, window["scheduled_date"])
                can_resume = occurrence_can_resume(preflight, occurrence)
                prior_expired = occurrence and occurrence["scheduled_date"] < window["scheduled_date"] and not can_resume
                preflight_allowed = can_resume if occurrence and not prior_expired else window["allowed"]
        if preflight_allowed and session_manager.get_camera_owner(preflight["resolved_camera_id"]) == "sandbox":
            if not session_manager.stop_sandbox():
                return jsonify({"error": "The tuner camera is still stopping. Wait for cleanup, then retry."}), 409
        with managed_transaction(commit=True) as conn:
                conn.execute("BEGIN IMMEDIATE")
                class_info = conn.execute(
                    """
                    SELECT c.*, cd.id AS resolved_camera_id,
                           cd.is_active AS camera_active,
                           cd.match_threshold AS camera_match_threshold
                    FROM classes c
                    LEFT JOIN camera_devices cd
                      ON cd.room_id = c.room_id AND cd.is_active = 1
                    WHERE c.id = ?
                    """,
                    (class_id,),
                ).fetchone()
                if not class_info:
                    return jsonify({"error": "Class not found."}), 404
                if class_info["teacher_id"] != user_id:
                    logger.warning("Unauthorized start session attempt for class %s by user %s.", class_id, user_id)
                    return jsonify({"error": "Forbidden"}), 403
                if class_info["is_archived"]:
                    return jsonify({"error": "Archived classes are non-operational."}), 409
                    
                camera_id = class_info["resolved_camera_id"]
                has_camera = bool(camera_id and class_info["camera_active"])

                availability = session_window_for_class(class_info)
                occurrence = _live_occurrence_for_class(conn, class_info, availability["scheduled_date"])
    
                worker_state = session_manager.worker_state(class_id)
                if worker_state != "missing":
                    if worker_state != "running":
                        return jsonify({"error": "The camera worker is still stopping or its status is unverified. Wait for confirmed cleanup before resuming."}), 409
                    if occurrence and not (lifecycle.collection_allowed(occurrence) if row_value(occurrence, 'policy_version') == 2 else occurrence_can_resume(class_info, occurrence)):
                        return jsonify({"error": "The attendance occurrence has ended. Wait for camera cleanup before starting again."}), 409
                    running_context = session_manager.active_sessions.get(class_id, {})
                    return jsonify({
                        "message": "Session already running.",
                        "session_id": running_context.get("attendance_session_id"),
                    }), 200

                # Validate the normal startup policy before closing, reopening,
                # or creating any occurrence. Repeated Start above deliberately
                # leaves a running worker's explicit tuning overrides intact.
                if has_camera:
                    overrides = load_settings(conn)
                    try:
                        overrides["MATCH_THRESHOLD"] = normal_match_threshold(
                            overrides["MATCH_THRESHOLD"], class_info["camera_match_threshold"]
                        )
                    except ValueError as exc:
                        return jsonify({"error": str(exc), "can_start": False}), 409
    
                if (occurrence and occurrence["status"] == "Open"
                        and occurrence["scheduled_date"] < availability["scheduled_date"]
                        and not occurrence_can_resume(class_info, occurrence)):
                    # The previous occurrence no longer owns a worker. Finalize
                    # it before selecting or creating today's occurrence.
                    close_attendance_session(conn, occurrence["id"], class_id, "resume_window_elapsed")
                    conn.execute("UPDATE classes SET is_active = 0 WHERE id = ?", (class_id,))
                    occurrence = _live_occurrence_for_class(conn, class_info, availability["scheduled_date"])
                can_resume = occurrence_can_resume(class_info, occurrence)
                if not (can_resume if occurrence else availability["allowed"]):
                    return jsonify({
                        "error": (
                            "This attendance occurrence is outside its resume window."
                            if occurrence else availability["message"]
                        ),
                        "can_start": False,
                        "window_start": availability["window_start"].strftime("%H:%M"),
                        "window_end": availability["window_end"].strftime("%H:%M"),
                    }), 400
    
                if has_camera:
                    camera_owner = session_manager.get_camera_owner(camera_id)
                    if camera_owner is not None and camera_owner != class_id:
                        return jsonify({
                            "error": (f"Camera is currently in use by another active class session (Class ID: {camera_owner})."
                                      if isinstance(camera_owner, int) else
                                      "Camera is currently in use. Stop its tuner or camera preview, then retry.")
                        }), 400
    
                class_info = dict(class_info)
                class_info["camera_id"] = camera_id
                if occurrence:
                    if row_value(occurrence, 'policy_version') == 2 and payload.get('session_id') != occurrence['id']:
                        return jsonify({'error': 'This view does not identify the current occurrence. Reload before resuming.'}), 409
                    try:
                        attendance_session_id = reopen_attendance_session(conn, occurrence["id"], payload.get('version'), user_id)
                    except ValueError as exc:
                        return jsonify({'error': str(exc)}), 409
                    # Snapshot the explicitly selected mode for this run; a
                    # later room-camera edit must not change crash detection.
                    conn.execute("UPDATE attendance_sessions SET camera_id = ? WHERE id = ?", (camera_id, attendance_session_id))
                    resumed = True
                else:
                    try:
                        attendance_session_id = create_attendance_session(conn, class_info, user_id)
                    except ValueError as exc:
                        return jsonify({'error': str(exc)}), 409
                    resumed = False
                session_row = conn.execute("SELECT actual_started_at FROM attendance_sessions WHERE id = ?", (attendance_session_id,)).fetchone()
                actual_started_at = session_row["actual_started_at"] if session_row else ""
                conn.execute("UPDATE classes SET is_active = 1 WHERE id = ?", (class_id,))
    
                worker_mode = "manual"
        # Persist the occurrence before reserving/spawning its durable owners.
        # Native startup never holds a SQLite write lock.
        if has_camera:
            try:
                session_context, _ = session_manager.start(class_id, attendance_session_id, overrides=overrides, camera_id=camera_id)
                worker_mode = session_context.get("worker_mode")
            except Exception:
                with managed_transaction(commit=True) as conn:
                    interrupt_attendance_session(conn, attendance_session_id)
                    conn.execute('UPDATE classes SET is_active=0 WHERE id=?', (class_id,))
                return jsonify({'error': 'Camera startup could not be confirmed. Attendance is interrupted; check session status before retrying.', 'session_id': attendance_session_id}), 409
            
        logger.info("Session %s for class %s in %s mode.", "resumed" if resumed else "started", class_id, worker_mode)
        return jsonify({
            "message": "Attendance resumed." if resumed else "Session started.",
            "session_id": attendance_session_id,
            "actual_started_at": actual_started_at,
            "worker_mode": worker_mode,
            "resumed": resumed,
        }), 200


@classes_bp.route("/api/classes/<int:class_id>/session/stop", methods=["POST"])
@login_required
@permission_required("run_attendance_session")
def api_stop_session(class_id):
    """Stops the background OpenCV AI loop."""
    user_id = session.get("user_id")
    response = _durable_session_action(class_id, user_id)
    if response is not None:
        return response

    # End and Resume share one ordering boundary. Keep both DB phases inside
    # that boundary, but release the first connection before native joins.
    with session_manager.lifecycle_guard():
        with managed_transaction(commit=True) as conn:
            class_info = conn.execute(
                "SELECT teacher_id, is_archived FROM classes WHERE id = ?", (class_id,)
            ).fetchone()
            if not class_info:
                return jsonify({"error": "Class not found."}), 404
            if class_info["teacher_id"] != user_id:
                logger.warning("Unauthorized stop session attempt for class %s by user %s.", class_id, user_id)
                return jsonify({"error": "Forbidden"}), 403
            if class_info["is_archived"]:
                return jsonify({"error": "Archived classes are non-operational."}), 409
            conn.execute("UPDATE classes SET is_active = 0 WHERE id = ?", (class_id,))

        if session_manager.is_running(class_id):
            if not session_manager.stop(class_id):
                return jsonify({"error": "The camera worker is still stopping. Its camera remains reserved; retry shortly."}), 409

        with managed_transaction(commit=True) as conn:
            closed_session_id = _close_open_session_for_class(conn, class_id, "manual_stop")
    logger.info(f"Session stopped for class {class_id}.")
    return jsonify({"message": "Session stopped.", "session_id": closed_session_id}), 200


def _durable_session_action(class_id, user_id, retry=False):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({'error': 'A JSON object is required.'}), 400
    try:
        with lifecycle.connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            owner = conn.execute('SELECT teacher_id,is_archived FROM classes WHERE id=?', (class_id,)).fetchone()
            if not owner:
                return jsonify({'error': 'Class not found.'}), 404
            if owner['teacher_id'] != user_id:
                return jsonify({'error': 'Forbidden'}), 403
            if owner['is_archived']:
                return jsonify({'error': 'Archived classes are non-operational.'}), 409
            sid = payload.get('session_id')
            occurrence = conn.execute('SELECT * FROM attendance_sessions WHERE id=? AND class_id=?', (sid, class_id)).fetchone() if type(sid) is int else get_latest_attendance_session(conn, class_id)
            state = lifecycle.lifecycle(conn, occurrence['id']) if occurrence else None
            if not state:
                if sid is not None or retry:
                    return jsonify({'error': 'The intended attendance occurrence is unavailable. Reload current session status.'}), 409
                return None
            if type(sid) is not int or type(payload.get('version')) is not int:
                return jsonify({'error': 'Session ID and current version are required. Reload current session status.'}), 409
            if retry:
                lifecycle.retry(conn, sid, user_id, payload['version'],
                                clock_review_confirmed=payload.get('clock_review_confirmed'),
                                clock_review_reason=payload.get('clock_review_reason', ''))
            else:
                operation = payload.get('operation_id')
                if not isinstance(operation, str) or not 8 <= len(operation) <= 80:
                    return jsonify({'error': 'A valid End operation ID is required.'}), 400
                lifecycle.accept_end(conn, sid, operation=operation, actor=user_id, version=payload['version'], reason='teacher_end')
        with lifecycle.connection() as conn:
            state = lifecycle.snapshot(conn, sid)
        return jsonify({'session_id': sid, 'message': state['message'], 'lifecycle': state}), 200 if state['closure_confirmed'] else 202
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    except sqlite3.Error:
        return jsonify({'error': 'Closure outcome cannot be confirmed. Refresh this exact session status before another action; collection still stops at the scheduled cutoff.', 'outcome_unknown': True}), 503


@classes_bp.route('/api/classes/<int:class_id>/session/retry', methods=['POST'])
@login_required
@permission_required('run_attendance_session')
def api_retry_session_closure(class_id):
    return _durable_session_action(class_id, session.get('user_id'), retry=True)


@classes_bp.route("/api/classes/<int:class_id>/video_feed")
@login_required
@permission_required("run_attendance_session")
def video_feed(class_id):
    """Returns the MJPEG stream for the live session."""
    user_id = session.get("user_id")
    
    with managed_transaction(commit=False) as conn:
        class_info = conn.execute(
            "SELECT teacher_id, is_active, is_archived FROM classes WHERE id = ?", (class_id,)
        ).fetchone()
        if not class_info:
            return jsonify({"error": "Class not found."}), 404
        if class_info["teacher_id"] != user_id:
            logger.warning("Unauthorized video feed access attempt for class %s by user %s.", class_id, user_id)
            return jsonify({"error": "Forbidden"}), 403
        if class_info["is_archived"]:
            return jsonify({"error": "Archived classes are non-operational."}), 409
        if not class_info["is_active"] or not session_manager.is_running(class_id):
            return jsonify({"error": "No live attendance session is running."}), 409
    return Response(
        generate_mjpeg_stream(
            class_id,
            access_check=_class_stream_access_check(user_id, class_id),
            **({"view": "live"} if request.args.get("view") == "live" else {}),
        ),
        mimetype='multipart/x-mixed-replace; boundary=frame',
    )


@classes_bp.route("/api/classes/<int:class_id>/override", methods=["POST"])
@login_required
@permission_required("correct_attendance")
def api_manual_override(class_id):
    """API endpoint to manually override a student's attendance status."""
    user_id = session.get("user_id")
    
    data = request.get_json()
    if not isinstance(data, dict) or not data:
        return jsonify({"error": "Invalid JSON payload."}), 400
        
    student_id = data.get("student_id")
    new_status = data.get("new_status")
    raw_reason = data.get("reason", "")
    
    if not isinstance(student_id, str) or not student_id:
        return jsonify({"error": "A student ID is required."}), 400
        
    if not isinstance(new_status, str) or new_status not in EDITABLE_ATTENDANCE_STATUSES:
        return jsonify({"error": "Invalid status."}), 400
    if not isinstance(raw_reason, str):
        return jsonify({"error": "The reason must be text."}), 400
    reason = raw_reason.strip()

    try:
        with managed_transaction(commit=True) as conn:
            # Validate ownership
            class_info = conn.execute(
                "SELECT teacher_id, is_archived FROM classes WHERE id = ?", (class_id,)
            ).fetchone()
            if not class_info:
                return jsonify({"error": "Class not found."}), 404
            if class_info["teacher_id"] != user_id:
                logger.warning("Unauthorized override attempt for class %s by user %s.", class_id, user_id)
                return jsonify({"error": "Forbidden"}), 403
            if class_info["is_archived"]:
                return jsonify({"error": "Archived classes are non-operational."}), 409

            # Validate student enrollment & get internal student PK
            student = conn.execute(
                """
                SELECT s.id as internal_id
                FROM students s
                JOIN class_enrollments ce ON s.id = ce.student_id
                WHERE ce.class_id = ? AND s.student_id = ?
                  AND s.is_active = 1
                  AND s.permanently_removed_at IS NULL
                """,
                (class_id, student_id)
            ).fetchone()
        
            if not student:
                return jsonify({"error": "Student is not enrolled in this class."}), 400
            
            internal_student_id = student["internal_id"]

            active_session = get_open_attendance_session(conn, class_id)
            if not active_session:
                return jsonify({"error": "Start a live attendance session before submitting live overrides."}), 400
            if row_value(active_session, 'policy_version') == 2 and (
                type(data.get('session_id')) is not int
                or data['session_id'] != active_session['id']
                or type(data.get('version')) is not int
            ):
                return jsonify({'error': 'This view is stale. Refresh the intended session before changing attendance.'}), 409

            upsert_attendance_record(
                conn,
                active_session["id"],
                class_id,
                internal_student_id,
                new_status,
                source="manual_edit",
                reason=reason,
                actor_user_id=user_id,
                live=True,
                version=data.get('version'),
            )
        
            return jsonify({
                "message": f"Attendance updated to {new_status}.",
                "session_id": active_session["id"],
            }), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("Error during manual override.")
        return jsonify({"error": "An internal server error occurred."}), 500
@classes_bp.route("/api/classes/<int:class_id>/attendance_status", methods=["GET"])
@login_required
@permission_required("run_attendance_session")
@_bounded_runtime_read
def api_attendance_status(class_id):
    """AJAX endpoint providing live attendance stats for the UI."""
    user_id = session.get("user_id")

    with session_manager.lifecycle_guard(), managed_transaction(commit=True) as conn:
        # Ownership and existence check
        class_info = conn.execute(
            "SELECT * FROM classes WHERE id = ?", (class_id,)
        ).fetchone()
        
        if not class_info:
            return jsonify({"error": "Class not found."}), 404
        if class_info["teacher_id"] != user_id:
            logger.warning("Unauthorized attendance status access attempt for class %s by user %s.", class_id, user_id)
            return jsonify({"error": "Forbidden"}), 403
        if class_info["is_archived"]:
            return jsonify({"error": "Archived classes are non-operational."}), 409

        # Only an authorized owner poll may keep its worker alive. In
        # particular, rejected guesses for another class must not mutate that
        # class worker's liveness timestamp.
        session_manager.heartbeat(class_id)

        is_active = bool(class_info["is_active"])
        
        active_session = get_open_attendance_session(conn, class_id)
        # A surviving Open camera occurrence is resumable even if the child
        # managed to publish an error but could not finish its DB cleanup.
        # Do not turn that interrupted occurrence into finalized absences.
        camera_occurrence_open = bool(active_session and active_session["camera_id"] is not None)
        stop_info = None if camera_occurrence_open else session_manager.cleanup_if_inactive(class_id, is_active)
        if stop_info:
            open_session = get_open_attendance_session(conn, class_id)
            if open_session:
                close_attendance_session(conn, open_session["id"], class_id, stop_info.get("reason") or "session_stopped")

        active_session = get_open_attendance_session(conn, class_id)
        if active_session and not row_value(active_session, 'policy_version') and not occurrence_can_resume(class_info, active_session):
            close_attendance_session(conn, active_session["id"], class_id, "resume_window_elapsed")
            conn.execute("UPDATE classes SET is_active = 0 WHERE id = ?", (class_id,))
            is_active = False
            active_session = conn.execute("SELECT * FROM attendance_sessions WHERE id = ?", (active_session["id"],)).fetchone()
            stop_info = {"reason": "resume_window_elapsed", "message": "The attendance occurrence and its resume window have ended."}
        if not active_session:
            active_session = _live_occurrence_for_class(conn, class_info, session_window_for_class(class_info)['scheduled_date'])
        runtime_info = dict(class_info)
        runtime_info["is_active"] = is_active
        runtime = _reconcile_session_runtime(conn, runtime_info, active_session)
        is_active = runtime["is_active"]
        active_session_id = active_session["id"] if active_session else None
        
        # Resolve membership at the occurrence's scheduled time. Current roster
        # changes are locked while Open, and period history keeps this stable.
        if active_session_id:
            enrolled_rows = occurrence_roster_rows(conn, active_session_id, class_id)
        else:
            enrolled_rows = conn.execute(
                """
                SELECT s.student_id, s.name
                FROM students s
                JOIN class_enrollments ce ON s.id = ce.student_id
                WHERE ce.class_id = ?
                  AND s.is_active = 1
                  AND s.permanently_removed_at IS NULL
                """,
                (class_id,),
            ).fetchall()
        
        # Attended
        if active_session_id:
            attended_rows = conn.execute(
                f"""
                SELECT s.student_id, s.name,
                       {_current_attendance_status_expr('a')} AS status,
                       a.timestamp,
                       a.source,
                       (SELECT aca.previous_status FROM attendance_change_audit aca WHERE aca.attendance_record_id=a.id ORDER BY aca.id LIMIT 1) AS initial_status,
                       a.reason
                FROM attendance_records a
                JOIN students s ON a.student_id = s.id
                WHERE a.class_id = ? AND a.attendance_session_id = ?
                """,
                (class_id, active_session_id),
            ).fetchall()
        else:
            attended_rows = []

        attended = {row["student_id"]: dict(row) for row in attended_rows}
        enrolled = [dict(row) for row in enrolled_rows]
        
        absent_list = []
        present_list = []
        pending_list = []
        excused_list = []
        
        for student in enrolled:
            if not active_session_id:
                continue  # Neutral preview below; no attendance result exists yet.
            sid = student["student_id"]
            if runtime.get('lifecycle'):
                mark = attended.get(sid)
                student.update(status=mark['status'] if mark else 'Pending', reason=mark['reason'] if mark else '', time='')
                if student['status'] == 'Pending':
                    pending_list.append(student)
                    continue
                if student['status'] == 'Absent':
                    absent_list.append(student)
                    continue
                if student['status'] == 'Excused':
                    excused_list.append(student)
                    continue
            if sid in attended and attended[sid]["status"] != "Absent":
                # Format time
                try:
                    dt = datetime.strptime(attended[sid]["timestamp"], "%Y-%m-%d %H:%M:%S")
                    student["time"] = dt.strftime("%H:%M")
                except (TypeError, ValueError):
                    student["time"] = ""
                student["status"] = attended[sid]["status"]
                student["reason"] = attended[sid]["reason"]
                if runtime.get('lifecycle') and attended[sid]['source'] != 'face_recognition' and attended[sid]['initial_status'] not in ('Present', 'Late'):
                    student['time'] = ''
                present_list.append(student)
            else:
                absent_list.append(student)
                
        # Sort present by time descending (latest first)
        present_list.sort(key=lambda x: x.get("time", ""), reverse=True)
        absent_list.sort(key=lambda x: x["name"])

        publication_sequence, current_frame_age_ms = (
            _session_publication_status(class_id)
            if is_active
            else (0, None)
        )
        preview_stats = session_manager.get_preview_stats(class_id) if is_active else {
            "preview_publication_sequence": 0, "preview_current_frame_age_ms": None,
            "preview_fps": 0.0, "preview_state": "stopped",
        }
        
        display_url, display_enabled = _cached_display_url(conn, active_session)
        from services.stream_capacity import viewing_availability
        try:
            capture_unstable = session_manager.active_sessions.get(class_id, {}).get('capture_unstable') if is_active else None
            if type(capture_unstable) is not bool:
                capture_unstable = None
        except (OSError, EOFError, BrokenPipeError):
            capture_unstable = None
        return jsonify({
            "viewing_capacity": viewing_availability(), "capture_unstable": capture_unstable,
            "display_enabled": display_enabled, "display_url": display_url,
            **runtime,
            "session_end_reason": stop_info.get("reason") if stop_info else None,
            "session_message": runtime["session_message"] or (stop_info.get("message") if stop_info else None),
            "session_id": active_session_id,
            "roster_preview": enrolled if not active_session_id else [],
            "can_start_new": not active_session_id and session_window_for_class(class_info)['allowed'],
            "publication_sequence": publication_sequence,
            "current_frame_age_ms": current_frame_age_ms,
            **preview_stats,
            "present": present_list,
            "absent": absent_list,
            "pending": pending_list,
            "excused": excused_list,
        }), 200
@classes_bp.route('/classes/<int:class_id>/sessions/preview', methods=['GET'])
@login_required
@permission_required('view_attendance_reports')
def session_preview(class_id):
    from services.session_agenda import agenda, scoped_classes
    actor, role = session.get('user_id'), session.get('role')
    day = request.args.get('date', '')
    try:
        if datetime.strptime(day, '%Y-%m-%d').date().isoformat() != day:
            raise ValueError()
    except ValueError:
        abort(400)
    with managed_transaction(commit=False) as conn:
        classes = scoped_classes(conn, actor, role, class_id)
        if not classes:
            abort(403)
        result = agenda(conn, classes, dict(view='date', start_date=day, end_date=day, state=''), 1, actor, role)
        if not result['sessions']:
            abort(404)
        item = result['sessions'][0]
        occurrence, window = session_start.lookup_occurrence(conn, classes[0])
        can_open = bool(role == 'Teacher' and not classes[0]['is_archived'] and
                        item.get('window_open') and window['allowed'] and
                        window['scheduled_date'] == day and not occurrence)
        earlier = occurrence if occurrence and occurrence['scheduled_date'] != day else None
    return render_template('classes/session_preview.html', item=item, can_open=can_open, earlier=earlier)


@classes_bp.route("/classes/<int:class_id>/sessions", methods=["GET"])
@login_required
@permission_required("view_attendance_reports")
def class_session_history(class_id):
    return _sessions_page(class_id)


@classes_bp.route("/sessions", methods=["GET"])
@login_required
@permission_required("view_attendance_reports")
def session_agenda():
    return _sessions_page()


def _sessions_page(class_id=None):
    from services.session_agenda import agenda, filters, scoped_classes, VIEWS
    role, actor = session.get('role'), session.get('user_id')
    if role not in ('Teacher', 'Dean'):
        abort(403)
    selected, page, error = {}, 1, ''
    default = 'past' if class_id is not None else 'today'
    now = lifecycle.institutional_now()
    try:
        selected, page = filters(request.args, now.date(), default)
    except ValueError as exc:
        error = str(exc)
        selected, page = filters({}, now.date(), default)
    with managed_transaction(commit=False) as conn:
        classes = scoped_classes(conn, actor, role, class_id)
        if class_id is not None and not classes:
            return 'You do not have permission to view these class sessions.', 403
        result = dict(sessions=[], history_total=0, history_page=1, history_pages=1)
        if not error:
            result = agenda(conn, classes, selected, page, actor, role, now=now)
    return render_template('classes/sessions.html', class_info=classes[0] if class_id is not None else None,
                           history_filters=selected, views=VIEWS, filter_error=error, **result), 400 if error else 200


@classes_bp.route("/classes/<int:class_id>/sessions/missed", methods=["POST"])
@login_required
@permission_required("correct_attendance")
def log_missed_session(class_id):
    try:
        return _save_session_explanation(class_id)
    except sqlite3.Error:
        logger.exception('Session explanation save could not be confirmed.')
        flash('We could not confirm that your explanation was saved. Review the saved explanation before trying again.', 'error')
        return redirect(url_for('classes.class_session_history', class_id=class_id))


def _save_session_explanation(class_id):
    """Explain missing attendance without changing student results."""
    from services.session_agenda import record_explanation
    user_id = session.get("user_id")
    attendance_session_id = request.form.get("attendance_session_id", "").strip()
    schedule_period_id = request.form.get("schedule_period_id", "").strip()
    scheduled_date = request.form.get("scheduled_date", "").strip()
    reason = request.form.get("reason", "").strip()
    notes = request.form.get("notes", "").strip()

    full_reason = f"{reason} - {notes}" if notes else reason

    if len(full_reason) > 1000:
        return "Keep the explanation to 1,000 characters or fewer.", 400

    if not scheduled_date or not reason:
        flash("Date and reason are required.", "error")
        return redirect(url_for("classes.class_session_history", class_id=class_id))

    try:
        occurrence_date = datetime.strptime(scheduled_date, "%Y-%m-%d").date()
    except ValueError:
        flash("Enter a valid missed-session date.", "error")
        return redirect(url_for("classes.class_session_history", class_id=class_id))

    with managed_transaction(commit=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        class_info = _class_with_teacher_and_camera(conn, class_id)
        if not class_info:
            flash("Class not found.", "error")
            return redirect(url_for("manage_classes"))
        if class_info["teacher_id"] != user_id:
            return "Unauthorized to log missed session for this class.", 403
        if class_info["is_archived"]:
            return "Archived classes are read-only.", 409

        try:
            effective_start = datetime.strptime(
                str(class_info["effective_start_date"]), "%Y-%m-%d"
            ).date()
        except (TypeError, ValueError):
            effective_start = _institution_today()
        if occurrence_date < effective_start:
            flash("A missed occurrence cannot predate the class effective start.", "error")
            return redirect(url_for("classes.class_session_history", class_id=class_id))
        if occurrence_date >= _institution_today():
            flash("Only past scheduled occurrences can be marked missed.", "error")
            return redirect(url_for("classes.class_session_history", class_id=class_id))
        if attendance_session_id:
            try:
                existing_id = int(attendance_session_id)
            except (TypeError, ValueError):
                existing_id = 0
            existing = conn.execute(
                """
                SELECT * FROM attendance_sessions
                WHERE id = ? AND class_id = ?
                """,
                (existing_id, class_id),
            ).fetchone()
            if not existing or existing["status"] != "Missed":
                flash("Select a valid missed occurrence before updating its reason.", "error")
                return redirect(url_for("classes.class_session_history", class_id=class_id))
            if str(existing["scheduled_date"]) != scheduled_date:
                flash("The missed occurrence identity no longer matches this page. Reload history and try again.", "error")
                return redirect(url_for("classes.class_session_history", class_id=class_id))
            if request.form.get('expected_reason') != existing['close_reason']:
                flash("This explanation changed or the page is out of date. Review the saved explanation before editing again.", "error")
                return redirect(url_for("classes.class_session_history", class_id=class_id))
            if full_reason != existing['close_reason']:
                record_explanation(conn, existing['id'], user_id, existing['close_reason'], full_reason)
                conn.execute(
                    "UPDATE attendance_sessions SET close_reason = ? WHERE id = ? AND status = 'Missed'",
                    (full_reason, existing["id"]),
                )
        else:
            try:
                requested_period_id = int(schedule_period_id)
            except (TypeError, ValueError):
                requested_period_id = 0
            schedule_period = conn.execute(
                """
                SELECT * FROM class_schedule_periods
                WHERE id = ? AND class_id = ?
                  AND effective_from <= ?
                  AND (effective_until IS NULL OR effective_until > ?)
                """,
                (requested_period_id, class_id, scheduled_date, scheduled_date),
            ).fetchone()
            if not schedule_period:
                flash("Select a valid scheduled occurrence before logging a reason.", "error")
                return redirect(url_for("classes.class_session_history", class_id=class_id))
            if occurrence_date.strftime("%a") not in normalized_day_tokens(schedule_period["days_of_week"]):
                flash("That date is not a scheduled meeting day for this class.", "error")
                return redirect(url_for("classes.class_session_history", class_id=class_id))

            existing = get_attendance_occurrence(
                conn, class_id, scheduled_date, schedule_period["start_time"]
            )
            if existing:
                flash(
                    "An actual attendance occurrence already exists for that date; reload history to target it directly.",
                    "error",
                )
                return redirect(url_for("classes.class_session_history", class_id=class_id))

            occurrence_key = attendance_occurrence_key(
                scheduled_date, schedule_period["start_time"]
            )
            new_occurrence = conn.execute(
                """
                INSERT INTO attendance_sessions
                (class_id, teacher_id, camera_id, scheduled_date, scheduled_start_time,
                 scheduled_end_time, occurrence_key, roster_anchor_at,
                 actual_started_at, actual_ended_at, status, close_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 'Missed', ?)
                """,
                (
                    class_id,
                    class_info["teacher_id"],
                    class_info["camera_id"],
                    scheduled_date,
                    schedule_period["start_time"],
                    schedule_period["end_time"],
                    occurrence_key,
                    attendance_occurrence_datetime(
                        scheduled_date, schedule_period["start_time"]
                    ),
                    full_reason,
                ),
            )
            record_explanation(conn, new_occurrence.lastrowid, user_id, '', full_reason)
        flash(f"Explanation saved for {scheduled_date}. Student attendance was not changed.", "success")
    return redirect(url_for("classes.class_session_history", class_id=class_id))


@classes_bp.route("/classes/<int:class_id>/sessions/<int:attendance_session_id>", methods=["GET"])
@login_required
@permission_required("view_attendance_reports")
def class_session_report(class_id, attendance_session_id):
    """Render one session attendance report."""
    from services.reporting import safe_report_return
    user_id = session.get("user_id")
    role = session.get("role")
    with managed_transaction(commit=False) as conn:
        conn.execute('BEGIN')  # Keep rows, audits and ending state in one read snapshot.
        class_info, session_row, records, audits = _session_report(conn, class_id, attendance_session_id)
        if not class_info:
            flash("Class not found.", "error")
            return redirect(url_for("manage_classes"))
        if not _can_view_class_history(class_info, user_id, role):
            return "You do not have permission to view this attendance report.", 403
        if not session_row:
            flash("Attendance session not found.", "error")
            return redirect(url_for("class_session_history", class_id=class_id))
        profile_ids = set()
        if records:
            record_ids = [record["internal_student_id"] for record in records]
            placeholders = ",".join("?" for _ in record_ids)
            profile_query = f"""SELECT s.id FROM students s
                WHERE s.id IN ({placeholders}) AND s.permanently_removed_at IS NULL"""
            if role == "Teacher":
                # Match the profile's current-roster permission, even when the
                # occurrence retains students who have since left the class.
                profile_query += """ AND EXISTS (SELECT 1 FROM class_enrollments ce
                    JOIN classes c ON c.id = ce.class_id
                    WHERE ce.student_id = s.id AND c.teacher_id = ?)"""
                record_ids.append(user_id)
            profile_ids = {row["id"] for row in conn.execute(profile_query, record_ids)}
        for record in records:
            record["can_view_profile"] = record["internal_student_id"] in profile_ids
        counts = _session_counts(records)
        display_url, display_enabled = _cached_display_url(conn, session_row)
        lifecycle_state = lifecycle.snapshot(conn, attendance_session_id)
        lifecycle_events = [{**dict(item), 'time_label': lifecycle.local_sql(item['at']),
            'event_label': item['event'].replace('_', ' ').capitalize()}
            for item in conn.execute("SELECT e.event,e.at,e.detail,COALESCE(u.username,'System') AS actor_name FROM attendance_lifecycle_events e LEFT JOIN users u ON u.id=e.actor WHERE e.session_id=? AND e.event<>'live_decision_accepted' ORDER BY e.id", (attendance_session_id,))] if lifecycle_state else []
    grouped_records = {
        "Present": [record for record in records if record["status"] == "Present"],
        "Late": [record for record in records if record["status"] == "Late"],
        "Absent": [record for record in records if record["status"] == "Absent"],
        "Excused": [record for record in records if record["status"] == "Excused"],
    }
    return render_template(
        "classes/session_report.html",
        report_return_url=safe_report_return(request.args.get('return_report')),
        class_info=class_info,
        attendance_session=session_row,
        records=records,
        grouped_records=grouped_records,
        audits=audits,
        counts=counts,
        lifecycle_state=lifecycle_state,
        lifecycle_events=lifecycle_events,
        display_url=display_url, display_enabled=display_enabled,
    ), 200, {'Cache-Control': 'no-store'}


@classes_bp.route("/classes/<int:class_id>/sessions/<int:attendance_session_id>/export.xlsx", methods=["GET"])
@login_required
@permission_required("export_reports")
def class_session_report_export(class_id, attendance_session_id):
    """Export an XLSX session report, with correction history for Dean audits."""
    from services.reporting import session_report_rows
    user_id = session.get("user_id")
    role = session.get("role")
    export_type = (request.args.get("type") or "").lower()
    try:
        with managed_transaction(commit=False) as conn:
            conn.execute('BEGIN')
            class_info = _class_with_teacher_and_camera(conn, class_id)
            if not class_info:
                return "Class not found.", 404

            can_export = (
                role == "Dean"
                or class_info["teacher_id"] == user_id
            )
            if not can_export:
                return "You do not have permission to export this attendance report.", 403

            if export_type == "audit" and role != "Dean":
                return "You do not have permission to export full audit logs. Dean role required.", 403

            session_row = conn.execute(
                """
                SELECT ats.*, cd.name AS camera_name
                FROM attendance_sessions ats
                LEFT JOIN camera_devices cd ON ats.camera_id = cd.id
                WHERE ats.id = ? AND ats.class_id = ?
                """,
                (attendance_session_id, class_id),
            ).fetchone()
            if not session_row:
                return "Attendance session not found.", 404

            rows = session_report_rows(conn, class_id, attendance_session_id)
            for row in rows:
                row['date_logged'] = row['recorded_at'][:10]
                row['time_logged'] = row['recorded_time']
                row['edited'] = ({'initial_manual': 'No', 'legacy_manual': 'Type not recorded'}
                                 .get(row['manual_kind'], 'Yes') if row['initial_status'] is not None else 'No')

            duration_str = (format_duration(session_row["actual_started_at"], session_row["actual_ended_at"])
                            if session_row['status'] == 'Open' or session_row['actual_ended_at'] else 'Not recorded')
            
            wb = openpyxl.Workbook()
            details = wb.create_sheet('Report Details')
            for label, value in [
                ('Generated at', datetime.now(timezone(timedelta(minutes=config.INSTITUTION_UTC_OFFSET_MINUTES))).isoformat(timespec='seconds')),
                ('Timezone', f'Institutional time UTC{config.INSTITUTION_UTC_OFFSET_MINUTES / 60:+g}'),
                ('Session ID', session_row['id']),
                ('Session date', session_row['scheduled_date']),
                ('Scheduled start', session_row['scheduled_start_time']),
                ('Class', f"{class_info['course_name']} ({class_info['class_code']})"),
                ('Scope', 'Historical session roster and current saved attendance results.'),
                ('Snapshot', 'Corrections made after generation are not included in this download.'),
            ]:
                details.append([label, spreadsheet_safe_text(value)])
            state = lifecycle.snapshot(conn, attendance_session_id)
            if state:
                details = wb.create_sheet('Closure Details')
                for label, value in [('Collection cutoff', lifecycle.local_sql(state['cutoff'])),
                                     ('Closure confirmed at', lifecycle.local_sql(state['closed_at']) if state['closed_at'] else 'Not confirmed'),
                                     ('Reason', state['end_reason'] or ''), ('Camera cleanup', state['cleanup']),
                                     ('Pending / needs review', state['counts']['Pending']), ('Summary', state['message'])]:
                    details.append([label, value])
                details.column_dimensions['A'].width = 26
                details.column_dimensions['B'].width = 95
                events = wb.create_sheet('Lifecycle Audit')
                events.append(['Event', 'Institutional time', 'Actor user ID', 'Detail'])
                for item in conn.execute('SELECT * FROM attendance_lifecycle_events WHERE session_id=? ORDER BY id', (attendance_session_id,)):
                    events.append([item['event'], lifecycle.local_sql(item['at']), item['actor'], item['detail']])
            ws = wb.active
            ws.title = "Session Report"
            
            bold_font = Font(bold=True)

            if export_type == "audit":
                filename = (
                    f"session_audit_log_{class_info['class_code']}_"
                    f"{session_row['scheduled_date']}_{session_row['id']}.xlsx"
                )
                
                headers = [
                    "Session ID", "Session Date", "Scheduled Start", "Session Started", "Session Ended", "Session Duration",
                    "Course Name", "Class Code", "Room Number", "Teacher", "Camera",
                    "Student ID", "Student Name", "Program", "Year Level",
                    "Date Logged", "Time Logged", "Status", "Reason", "Source", "Edited", "Latest Editor",
                ]
                ws.append(headers)
                
                for cell in ws[1]:
                    cell.font = bold_font
                    
                for row in rows:
                    ws.append([spreadsheet_safe_text(value) for value in [
                        session_row["id"],
                        session_row["scheduled_date"],
                        session_row["scheduled_start_time"],
                        session_row["actual_started_at"] or "",
                        session_row["actual_ended_at"] or "",
                        duration_str,
                        class_info["course_name"],
                        class_info["class_code"],
                        class_info["room_code"] or "",
                        class_info["teacher_name"],
                        session_row["camera_name"] or "",
                        row["student_public_id"],
                        row["student_name"],
                        row["program"],
                        row["year_level"],
                        row["date_logged"],
                        row["time_logged"],
                        row["status"],
                        row["reason"],
                        row["source"],
                        row["edited"],
                        row["latest_editor"],
                    ]])
                    
                ws.freeze_panes = "A2"
                audit_sheet = wb.create_sheet("Change Audit")
                audit_sheet.append([
                    "Audit ID", "Session ID", "Class Code", "Changed At",
                    "Student ID", "Student Name", "Changed By", "Previous Status",
                    "New Status", "Previous Reason", "New Reason", "Change Reason",
                ])
                audit_rows = conn.execute(
                    """
                    SELECT aca.*, s.student_id AS public_student_id,
                           s.name AS student_name, u.username AS actor_name
                    FROM attendance_change_audit aca
                    LEFT JOIN students s ON s.id = aca.student_id
                    LEFT JOIN users u ON u.id = aca.actor_user_id
                    WHERE aca.attendance_session_id = ? AND aca.class_id = ?
                    ORDER BY aca.changed_at ASC, aca.id ASC
                    """,
                    (attendance_session_id, class_id),
                ).fetchall()
                for audit in audit_rows:
                    audit_sheet.append([spreadsheet_safe_text(value) for value in [
                        audit["id"], session_row["id"], class_info["class_code"],
                        audit["changed_at"], audit["public_student_id"] or "",
                        audit["student_name"] or "", audit["actor_name"] or "",
                        audit["previous_status"], audit["new_status"],
                        audit["previous_reason"], audit["new_reason"], audit["change_reason"],
                    ]])
                for cell in audit_sheet[1]:
                    cell.font = bold_font
                audit_sheet.freeze_panes = "A2"
                audit_sheet.auto_filter.ref = audit_sheet.dimensions
            else:
                filename = (
                    f"session_report_{class_info['class_code']}_"
                    f"{session_row['scheduled_date']}_{session_row['id']}.xlsx"
                )
                ws.append(["Course Name:", spreadsheet_safe_text(class_info["course_name"])])
                ws.append(["Class Code:", spreadsheet_safe_text(class_info["class_code"])])
                ws.append(["Session Date:", spreadsheet_safe_text(session_row["scheduled_date"])])
                ws.append(["Teacher:", spreadsheet_safe_text(class_info["teacher_name"])])
                ws.append(["Duration:", spreadsheet_safe_text(duration_str)])
                ws.append([])
                
                for i in range(1, 6):
                    ws.cell(row=i, column=1).font = bold_font
                
                headers = [
                    "Student ID", "Student Name", "Program", "Year Level",
                    "Status", "Time Logged", "Reason", "Recorded By"
                ]
                ws.append(headers)
                
                header_row = 7
                for cell in ws[header_row]:
                    cell.font = bold_font
                
                for row in rows:
                    if row["latest_editor"]:
                        recorded_by = row["latest_editor"]
                    elif row["source"] == "face_recognition":
                        recorded_by = "System (Camera)"
                    elif row["source"]:
                        recorded_by = row["source"]
                    else:
                        recorded_by = "Not recorded"
                        
                    ws.append([spreadsheet_safe_text(value) for value in [
                        row["student_public_id"],
                        row["student_name"],
                        row["program"],
                        row["year_level"],
                        row["status"],
                        row["time_logged"],
                        row["reason"],
                        recorded_by,
                    ]])
                    
                ws.freeze_panes = "A8"
                
            # All report/audit reads are complete; release the snapshot before formatting.
            conn.rollback()
            # Keep long correction reasons readable without oversized columns.
            for sheet in wb.worksheets:
                for column_cells in sheet.columns:
                    length = max(len(str(cell.value or "")) for cell in column_cells)
                    sheet.column_dimensions[get_column_letter(column_cells[0].column)].width = min(length + 2, 60)
                    for cell in column_cells:
                        cell.alignment = Alignment(vertical="top", wrap_text=True)

            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            
            response = send_file(
                output,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                as_attachment=True,
                download_name=filename,
            )
            response.headers['Cache-Control'] = 'no-store'
            return response
    except Exception:
        logger.exception("Error generating session XLSX export.")
        return "An internal server error occurred.", 500
@classes_bp.route(
    "/api/classes/<int:class_id>/sessions/<int:attendance_session_id>/attendance/<student_public_id>",
    methods=["POST"],
)
@login_required
@permission_required("correct_attendance")
def api_update_session_attendance(class_id, attendance_session_id, student_public_id):
    """Update one student's attendance status for a specific session."""
    user_id = session.get("user_id")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Submit an attendance status and reason as a JSON object."}), 400
    new_status = data.get("status")
    raw_reason = data.get("reason", "")

    if not isinstance(new_status, str) or new_status not in EDITABLE_ATTENDANCE_STATUSES:
        return jsonify({"error": "Invalid attendance status."}), 400
    if not isinstance(raw_reason, str):
        return jsonify({"error": "The reason must be text."}), 400
    reason = raw_reason.strip()
    if not reason:
        return jsonify({"error": "A reason is required for attendance changes."}), 400

    try:
        with managed_transaction(commit=True) as conn:
            class_info = conn.execute(
                "SELECT teacher_id, is_archived FROM classes WHERE id = ?",
                (class_id,),
            ).fetchone()
            if not class_info:
                return jsonify({"error": "Class not found."}), 404
            if class_info["teacher_id"] != user_id:
                return jsonify({"error": "Forbidden"}), 403
            if class_info["is_archived"]:
                return jsonify({"error": "Archived classes are read-only."}), 409

            session_row = conn.execute(
                """
                SELECT id, scheduled_date, scheduled_start_time, status
                FROM attendance_sessions WHERE id = ? AND class_id = ?
                """,
                (attendance_session_id, class_id),
            ).fetchone()
            if not session_row:
                return jsonify({"error": "Attendance session not found."}), 404
            if session_row["status"] not in {"Open", "Closed"}:
                return jsonify({"error": "Attendance can only be corrected for an actual occurrence."}), 409

            student = next(
                (
                    roster_student
                    for roster_student in occurrence_roster_rows(
                        conn, attendance_session_id, class_id
                    )
                    if roster_student["student_id"] == student_public_id
                ),
                None,
            )
            if not student:
                return jsonify({"error": "Student was not enrolled for this attendance occurrence."}), 400

            upsert_attendance_record(
                conn,
                attendance_session_id,
                class_id,
                student["id"],
                new_status,
                source="manual_edit",
                reason=reason,
                actor_user_id=user_id,
            )
            return jsonify({"message": f"Attendance updated to {new_status}."}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        logger.exception("Failed to update session attendance.")
        return jsonify({"error": "An internal server error occurred."}), 500
# ---------------------------------------------------------------------------
# Camera Management
# ---------------------------------------------------------------------------

@classes_bp.route("/classes/<int:class_id>/archive", methods=["POST"])
@login_required
@permission_required("manage_classes")
def archive_class(class_id):
    """Soft-delete/archive a class."""
    user_id = session.get("user_id")
    role = session.get("role")
    try:
        # Authorize before affecting the worker. Do not hold SQLite's write
        # lock while waiting: a cooperative worker may need to finish a final
        # database operation before its thread/process can exit.
        with managed_transaction(commit=False) as conn:
            class_info = conn.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone()
            if not class_info:
                flash("Class not found.", "error")
                return redirect(url_for("classes.manage_classes"))

            if role != "Dean" and class_info["teacher_id"] != user_id:
                abort(403)

        with session_manager.lifecycle_guard():
            stopped = session_manager.stop(class_id)
            if not stopped or session_manager.is_running(class_id):
                raise RuntimeError("The active attendance worker did not stop cleanly.")

            with managed_transaction(commit=True) as conn:
                conn.execute("BEGIN IMMEDIATE")
                class_info = conn.execute(
                    "SELECT * FROM classes WHERE id = ?", (class_id,)
                ).fetchone()
                if not class_info:
                    raise RuntimeError("Class disappeared during archive.")
                if role != "Dean" and class_info["teacher_id"] != user_id:
                    abort(403)

                from services.attendance_display import revoke_class_displays
                revoke_class_displays(conn, class_id, user_id, 'class_archived')
                _close_open_session_for_class(conn, class_id, "class_archived")
                close_class_schedule(conn, class_id, _institution_today().isoformat())
                conn.execute(
                    "UPDATE classes SET is_active = 0, is_archived = 1 WHERE id = ?",
                    (class_id,),
                )
        flash("Class archived successfully.", "success")
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to archive class.")
        flash("An error occurred while archiving the class.", "error")

    return redirect(url_for("classes.manage_classes"))


@classes_bp.route("/classes/archived")
@login_required
@permission_required("manage_classes")
def archived_classes():
    """View archived classes."""
    user_id = session.get("user_id")
    role = session.get("role")
    with managed_transaction(commit=False) as conn:
        if role == "Dean":
            classes = conn.execute(
                """
                SELECT c.*, u.username as teacher_name, cd.name AS camera_name, cd.is_active AS camera_active,
                       r.room_code AS camera_room, cd.placement_notes AS camera_placement,
                       r.room_code, r.room_name, b.name AS building, f.name AS floor, r.department, r.room_type, r.is_active AS room_active,
                       cr.room_code AS camera_room_code, cr.room_name AS camera_room_name,
                       COUNT(ce.id) AS enrolled_count
                FROM classes c
                JOIN users u ON c.teacher_id = u.id
                LEFT JOIN camera_devices cd ON cd.room_id = c.room_id AND cd.is_active = 1 AND cd.lifecycle_status = 'Ready'
                LEFT JOIN rooms r ON c.room_id = r.id
                LEFT JOIN floors f ON r.floor_id = f.id
                LEFT JOIN buildings b ON f.building_id = b.id
                LEFT JOIN rooms cr ON cd.room_id = cr.id
                LEFT JOIN class_enrollments ce ON c.id = ce.class_id
                WHERE c.is_archived = 1
                GROUP BY c.id
                ORDER BY c.id DESC
                """
            ).fetchall()
        else:
            classes = conn.execute(
                """
                SELECT c.*, u.username as teacher_name, cd.name AS camera_name, cd.is_active AS camera_active,
                       r.room_code AS camera_room, cd.placement_notes AS camera_placement,
                       r.room_code, r.room_name, b.name AS building, f.name AS floor, r.department, r.room_type, r.is_active AS room_active,
                       cr.room_code AS camera_room_code, cr.room_name AS camera_room_name,
                       COUNT(ce.id) AS enrolled_count
                FROM classes c
                JOIN users u ON c.teacher_id = u.id
                LEFT JOIN camera_devices cd ON cd.room_id = c.room_id AND cd.is_active = 1 AND cd.lifecycle_status = 'Ready'
                LEFT JOIN rooms r ON c.room_id = r.id
                LEFT JOIN floors f ON r.floor_id = f.id
                LEFT JOIN buildings b ON f.building_id = b.id
                LEFT JOIN rooms cr ON cd.room_id = cr.id
                LEFT JOIN class_enrollments ce ON c.id = ce.class_id
                WHERE teacher_id = ? AND c.is_archived = 1
                GROUP BY c.id
                ORDER BY c.id DESC
                """,
                (user_id,),
            ).fetchall()

        users = conn.execute("SELECT id, username FROM users WHERE role='Teacher' AND is_active=1 ORDER BY username").fetchall()
        cameras = []
        rooms = [_serialize_room(room) for room in _fetch_active_rooms(conn)]

    return render_template("classes/manage.html", classes=classes, users=users, cameras=cameras, rooms=rooms, is_archived_view=True)


@classes_bp.route("/classes/<int:class_id>/restore", methods=["POST"])
@login_required
@permission_required("manage_classes")
def restore_class(class_id):
    """Restore an archived class."""
    user_id = session.get("user_id")
    role = session.get("role")
    with managed_transaction(commit=True) as conn:
        _begin_class_schedule_write(conn)
        class_info = conn.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone()
        if not class_info:
            flash("Class not found.", "error")
            return redirect(url_for("classes.archived_classes"))

        if role != "Dean" and class_info["teacher_id"] != user_id:
            abort(403)

        if not _eligible_teacher(conn, class_info["teacher_id"]):
            flash("Cannot restore class: Assigned Teacher is inactive or unavailable.", "error")
            return redirect(url_for("classes.archived_classes"))

        proposed_room_id = request.form.get("room_id") or class_info["room_id"]
        resolved_room_id, _ = _resolve_room_from_payload(conn, proposed_room_id)
        if not resolved_room_id:
            flash("Cannot restore class: Assigned room is inactive.", "error")
            return redirect(url_for("classes.archived_classes"))

        try:
            schedule = canonical_schedule(
                class_info["days_of_week"],
                class_info["start_time"],
                class_info["end_time"],
            )
        except ScheduleValidationError as exc:
            flash(f"Cannot restore class: {exc}", "error")
            return redirect(url_for("classes.archived_classes"))

        conflict = find_schedule_conflict(
            conn,
            resolved_room_id,
            class_info["teacher_id"],
            schedule,
            exclude_class_id=class_id,
        )
        if conflict:
            flash(f"Cannot restore class: {conflict}", "error")
            return redirect(url_for("classes.archived_classes"))

        room_camera = _get_active_room_camera(conn, resolved_room_id)

        try:
            if class_info["is_archived"]:
                resume_class_schedule(
                    conn,
                    class_info,
                    schedule,
                    _institution_today().isoformat(),
                )
            conn.execute(
                """
                UPDATE classes
                SET room_id = ?, camera_id = ?, days_of_week = ?,
                    start_time = ?, end_time = ?, is_archived = 0
                WHERE id = ?
                """,
                (
                    resolved_room_id,
                    room_camera["id"] if room_camera else None,
                    schedule["days_text"],
                    schedule["start_time"],
                    schedule["end_time"],
                    class_id,
                ),
            )
            flash("Class restored successfully.", "success")
            return redirect(url_for("classes.class_detail", class_id=class_id))
        except Exception:
            logger.exception("Failed to restore class.")
            flash("An error occurred while restoring the class.", "error")

    return redirect(url_for("classes.archived_classes"))
