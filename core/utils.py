__all__ = ['_is_fallback_room_value', '_room_display', '_serialize_room', '_ensure_room_for_code', '_resolve_room_from_payload', '_fetch_active_rooms', '_get_active_room_camera', 'login_required', '_camera_source_for_capture', '_mask_camera_source', '_camera_is_fallback', '_upsert_student_with_embedding', '_pending_student_registration_exists', '_camera_room_display', '_camera_health_state', '_camera_display_name', '_serialize_camera', '_fetch_cameras_for_management', '_get_active_cameras', '_get_default_camera_id', '_camera_exists_active', '_fetch_room_coverage', '_camera_coverage_summary', '_mark_camera_health_from_session', '_class_with_teacher_and_camera', '_can_view_class_history', '_current_attendance_status_expr', '_institution_local_date_expr', '_institution_local_time_expr', '_institution_local_datetime_expr', '_session_counts', '_session_report', '_close_open_session_for_class', 'format_duration',
    "VALID_YEAR_LEVELS",
    "ROOM_TYPES",
    "CAMERA_LIFECYCLE_STATUSES",
]

import functools
import datetime
import config
from flask import flash, redirect, url_for, session, request, jsonify
from services.camera_security import decrypt_camera_source, mask_camera_source
from services.attendance_sessions import get_open_attendance_session, close_attendance_session
from security.session_auth import authenticated_session_user


def _institution_local_date_expr(timestamp_expression):
    """Return a SQLite DATE expression using the configured institution offset."""
    offset = int(config.INSTITUTION_UTC_OFFSET_MINUTES)
    return f"DATE({timestamp_expression}, '{offset:+d} minutes')"


def _institution_local_time_expr(timestamp_expression):
    """Return a SQLite TIME expression using the configured institution offset."""
    offset = int(config.INSTITUTION_UTC_OFFSET_MINUTES)
    return f"TIME({timestamp_expression}, '{offset:+d} minutes')"


def _institution_local_datetime_expr(timestamp_expression):
    """Return a SQLite DATETIME expression using the institution offset."""
    offset = int(config.INSTITUTION_UTC_OFFSET_MINUTES)
    return f"DATETIME({timestamp_expression}, '{offset:+d} minutes')"


def format_duration(start_time, end_time=None):
    """Format duration between start_time and end_time (or current time if end_time is None/empty/Open)."""
    if not start_time:
        return "N/A"
    
    def _parse_dt(dt_val):
        if isinstance(dt_val, datetime.datetime):
            return dt_val
        if not dt_val or str(dt_val).strip().lower() in ("", "none", "open"):
            return None
        dt_str = str(dt_val).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return datetime.datetime.strptime(dt_str, fmt)
            except ValueError:
                pass
        try:
            return datetime.datetime.fromisoformat(dt_str)
        except ValueError:
            return None

    dt_start = _parse_dt(start_time)
    if not dt_start:
        return "N/A"
        
    dt_end = _parse_dt(end_time) if end_time else None
    if not dt_end and end_time:
        if str(end_time).strip().lower() == "open":
            dt_end = datetime.datetime.now()
        else:
            return "N/A"
    elif not dt_end:
        dt_end = datetime.datetime.now()

    total_seconds = int((dt_end - dt_start).total_seconds())
    if total_seconds < 0:
        total_seconds = 0
        
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    
    return " ".join(parts)



def _is_fallback_room_value(value):
    return str(value or "").strip().lower() in ("", "unassigned", "fallback", "demo")


def _room_display(room):
    if not room:
        return ""
    code = room.get("room_code", "")
    name = room.get("room_name", "")
    if name and name != f"Room {code}":
        return f"{code} - {name}"
    return f"Room {code}"


def _serialize_room(room):
    data = dict(room)
    data["building"] = data.get("building_name") or data.get("building") or ""
    data["floor"] = data.get("floor_name") or data.get("floor") or ""
    data["display_name"] = _room_display(data)
    hierarchy = " / ".join(
        value for value in (data["building"], data["floor"], data["display_name"]) if value
    )
    data["hierarchy_display"] = hierarchy
    return data


def _ensure_room_for_code(conn, room_code):
    room_code = str(room_code or "").strip()
    if _is_fallback_room_value(room_code):
        return None
    building = conn.execute(
        "SELECT id, name FROM buildings WHERE normalized_name = 'unspecified building'"
    ).fetchone()
    if not building:
        cursor = conn.execute(
            """
            INSERT INTO buildings (name, normalized_name, is_active)
            VALUES ('Unspecified Building', 'unspecified building', 1)
            """
        )
        building_id = cursor.lastrowid
        building_name = "Unspecified Building"
    else:
        building_id = building["id"]
        building_name = building["name"]
    floor = conn.execute(
        """
        SELECT id, name FROM floors
        WHERE building_id = ? AND normalized_name = 'unspecified floor'
        """,
        (building_id,),
    ).fetchone()
    if not floor:
        cursor = conn.execute(
            """
            INSERT INTO floors
                (building_id, name, normalized_name, sort_order, is_active)
            VALUES (?, 'Unspecified Floor', 'unspecified floor', 0, 1)
            """,
            (building_id,),
        )
        floor_id = cursor.lastrowid
        floor_name = "Unspecified Floor"
    else:
        floor_id = floor["id"]
        floor_name = floor["name"]
    conn.execute(
        """
        INSERT OR IGNORE INTO rooms
            (floor_id, room_code, room_name, building, floor, department, room_type, is_active)
        VALUES (?, ?, ?, ?, ?, 'Computer Studies Department', 'Classroom', 1)
        """,
        (floor_id, room_code, f"Room {room_code}", building_name, floor_name),
    )
    row = conn.execute("SELECT id FROM rooms WHERE room_code = ?", (room_code,)).fetchone()
    return row["id"] if row else None


def _resolve_room_from_payload(conn, room_id):
    raw_room = str(room_id or "").strip()
    try:
        parsed_room = int(raw_room) if raw_room.isdecimal() else 0
    except ValueError:
        parsed_room = 0
    if 0 < parsed_room <= 9223372036854775807:
        row = conn.execute(
            """
            SELECT r.*
            FROM rooms r
            JOIN floors f ON f.id = r.floor_id AND f.is_active = 1
            JOIN buildings b ON b.id = f.building_id AND b.is_active = 1
            WHERE r.id = ? AND r.is_active = 1
            """,
            (parsed_room,),
        ).fetchone()
        if row:
            return row["id"], row["room_code"]
    return None, ""


def _fetch_active_rooms(conn):
    return conn.execute(
        """
        SELECT r.*, f.name AS floor_name, f.sort_order AS floor_sort_order,
               b.id AS building_id, b.name AS building_name
        FROM rooms r
        JOIN floors f ON r.floor_id = f.id
        JOIN buildings b ON f.building_id = b.id
        WHERE r.is_active = 1 AND f.is_active = 1 AND b.is_active = 1
        ORDER BY b.name COLLATE NOCASE, f.sort_order, f.name COLLATE NOCASE,
                 r.room_code COLLATE NOCASE
        """
    ).fetchall()


def _get_active_room_camera(conn, room_id):
    """Return the one active attendance camera assigned to a room, if any."""
    if not room_id:
        return None
    return conn.execute(
        """
        SELECT * FROM camera_devices
        WHERE room_id = ? AND is_active = 1
        ORDER BY id ASC
        LIMIT 1
        """,
        (room_id,),
    ).fetchone()


def _request_wants_json_auth_response():
    """Keep fetch/API auth expiry machine-readable, including non-/api URLs."""
    return (
        request.path.startswith("/api/")
        or request.is_json
        or (
            request.accept_mimetypes.accept_json
            and not request.accept_mimetypes.accept_html
        )
    )


def login_required(view_func):
    """Require a live, active database account and refresh its authorization."""
    @functools.wraps(view_func)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            if _request_wants_json_auth_response():
                return jsonify({
                    "error": "Your login session expired.",
                    "session_expired": True,
                    "login_url": url_for("auth.login"),
                }), 401
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("auth.login"))

        from database.db_manager import managed_transaction
        with managed_transaction(commit=False) as conn:
            current_user = authenticated_session_user(conn, session)
        if not current_user:
            session.clear()
            if _request_wants_json_auth_response():
                return jsonify({
                    "error": "Your account is no longer authorized.",
                    "session_expired": True,
                    "login_url": url_for("auth.login"),
                }), 401
            flash("Your account is no longer authorized. Please contact the Dean.", "error")
            return redirect(url_for("auth.login"))

        # Role and first-login state are authoritative in the database on every
        # request, so an already-issued session cannot retain superseded access.
        session["username"] = current_user["username"]
        session["role"] = current_user["role"]
        session["requires_password_change"] = current_user["requires_password_change"]
        endpoint = request.endpoint or ""
        if session.get("requires_password_change") and endpoint not in ["static"] and not endpoint.endswith(("password_reset", "logout")):
            if _request_wants_json_auth_response():
                return jsonify({
                    "error": "You must reset your default password before continuing.",
                    "password_reset_required": True,
                    "password_reset_url": url_for("auth.password_reset"),
                }), 401
            flash("You must reset your default password before continuing.", "warning")
            return redirect(url_for("auth.password_reset"))
        try:
            from services.network_monitor import observe_authenticated_client
            observe_authenticated_client()
        except Exception:
            # Optional observations must never interrupt attendance or login.
            pass
        return view_func(*args, **kwargs)
    return wrapped


def _camera_source_for_capture(camera):
    source = decrypt_camera_source(camera["camera_type"], camera["source"])
    if camera["camera_type"] == "local" and str(source).isdigit():
        return int(source)
    return source


def _mask_camera_source(camera):
    """Keep RTSP URLs from being exposed in full through admin APIs."""
    return mask_camera_source(camera["camera_type"], camera["source"])


def _camera_is_fallback(camera):
    """Return whether a camera is a fallback/demo device instead of a room camera."""
    return bool(camera.get("is_default", 0))


def _upsert_student_with_embedding(
    conn,
    student_id,
    name,
    program_id,
    year_level,
    consent_granted,
    face_blob,
    *,
    allow_update=True,
):
    """
    Create or update an active student and replace their stored embedding(s).
    Supports a single face vector (bytes) or a list/tuple of face vectors.
    Also handles JSON-encoded lists of base64 vectors if passed as bytes/str.
    Deletes prior embeddings for the student and inserts new rows for each vector (R2).
    """
    import json
    import base64
    from services.biometric_security import is_encrypted, encrypt_face_vector

    # Normalize face_blob into a list of individual vector blobs
    def _parse_item(item):
        if isinstance(item, str):
            if item.startswith("gAAAAA"):
                return item.encode("utf-8")
            try:
                return base64.b64decode(item)
            except Exception:
                return item.encode("utf-8")
        elif isinstance(item, bytes):
            return item
        return item

    if isinstance(face_blob, (bytes, str)):
        if isinstance(face_blob, str) and face_blob.strip().startswith("["):
            try:
                items = json.loads(face_blob)
                if isinstance(items, list):
                    face_blobs = [_parse_item(item) for item in items]
                else:
                    face_blobs = [face_blob]
            except Exception:
                face_blobs = [face_blob]
        elif isinstance(face_blob, bytes) and face_blob.strip().startswith(b"["):
            try:
                items = json.loads(face_blob.decode("utf-8"))
                if isinstance(items, list):
                    face_blobs = [_parse_item(item) for item in items]
                else:
                    face_blobs = [face_blob]
            except Exception:
                face_blobs = [face_blob]
        else:
            face_blobs = [face_blob]
    elif isinstance(face_blob, (list, tuple)):
        face_blobs = list(face_blob)
    else:
        face_blobs = [face_blob] if face_blob is not None else []

    if allow_update:
        conn.execute(
            """
            INSERT INTO students (student_id, name, program_id, year_level, consent_granted)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(student_id) DO UPDATE SET
                name = excluded.name,
                program_id = excluded.program_id,
                year_level = excluded.year_level,
                consent_granted = excluded.consent_granted
            """,
            (student_id, name, program_id, year_level, consent_granted),
        )
    else:
        conn.execute(
            """
            INSERT INTO students (student_id, name, program_id, year_level, consent_granted)
            VALUES (?, ?, ?, ?, ?)
            """,
            (student_id, name, program_id, year_level, consent_granted),
        )

    student_pk = conn.execute(
        "SELECT id FROM students WHERE student_id = ?",
        (student_id,),
    ).fetchone()["id"]

    conn.execute("DELETE FROM embeddings WHERE student_id = ?", (student_pk,))
    for vector in face_blobs:
        if vector:
            if not is_encrypted(vector):
                vector = encrypt_face_vector(vector)
            conn.execute(
                "INSERT INTO embeddings (student_id, face_vector) VALUES (?, ?)",
                (student_pk, vector),
            )
    return student_pk


def _pending_student_registration_exists(conn, student_id):
    row = conn.execute(
        """
        SELECT id
        FROM pending_student_registrations
        WHERE UPPER(TRIM(student_id)) = UPPER(TRIM(?)) AND status = 'Pending'
        """,
        (student_id,),
    ).fetchone()
    return row is not None


def _camera_room_display(camera):
    """Return the reader-facing room label for management UIs."""
    if _camera_is_fallback(camera):
        return "Fallback / Demo Camera"
    if camera.get("room_code"):
        return _room_display(camera)
    return "Unassigned Room"


def _camera_health_state(camera):
    """Describe the saved camera check using the shared freshness policy."""
    from services.camera_health import describe_stored_camera_health
    return describe_stored_camera_health(camera)["label"]


def _camera_display_name(camera):
    parts = [camera.get("name", ""), _camera_room_display(camera)]
    placement = (camera.get("placement_notes") or "").strip()
    if placement:
        parts.append(placement)
    return " - ".join(parts)


def _serialize_camera(camera):
    data = dict(camera)
    data["assigned_class_count"] = int(data.get("assigned_class_count") or 0)
    data["is_fallback"] = _camera_is_fallback(data)
    data["room_display"] = _camera_room_display(data)
    from services.camera_health import describe_stored_camera_health
    observation = describe_stored_camera_health(data)
    data["health_state"] = observation["label"]
    data["health_detail"] = observation["detail"]
    data["source_display"] = _mask_camera_source(data)
    data["lifecycle_status"] = data.get("lifecycle_status") or "Ready"
    if data["camera_type"] == "rtsp":
        data["source"] = ""
    data["display_name"] = _camera_display_name(data)
    return data


def _fetch_cameras_for_management(conn):
    """Fetch camera rows with computed assignment counts for management views."""
    return conn.execute(
        """
        SELECT cd.*, r.room_code, r.room_name, b.name AS building, f.name AS floor,
               r.department, r.room_type,
               (r.is_active=1 AND f.is_active=1 AND b.is_active=1) AS location_active,
               COUNT(c.id) AS assigned_class_count
        FROM camera_devices cd
        LEFT JOIN rooms r ON cd.room_id = r.id
        LEFT JOIN floors f ON r.floor_id = f.id
        LEFT JOIN buildings b ON f.building_id = b.id
        LEFT JOIN classes c ON c.room_id = cd.room_id
        GROUP BY cd.id
        ORDER BY
            cd.is_default DESC,
            r.room_code ASC,
            cd.name ASC
        """
    ).fetchall()


def _get_active_cameras(conn, room_id=None):
    """Fetch active cameras, optionally restricted to one authoritative room."""
    cameras = conn.execute(
        """
        SELECT cd.*, r.room_code, r.room_name, b.name AS building, f.name AS floor,
               r.department, r.room_type
        FROM camera_devices cd
        LEFT JOIN rooms r ON cd.room_id = r.id
        LEFT JOIN floors f ON r.floor_id = f.id
        LEFT JOIN buildings b ON f.building_id = b.id
        WHERE cd.is_active = 1 AND cd.lifecycle_status = 'Ready' AND (? IS NULL OR cd.room_id = ?)
        ORDER BY cd.is_default DESC, r.room_code ASC, cd.name ASC
        """,
        (room_id, room_id),
    ).fetchall()
    return cameras


def _get_default_camera_id(conn):
    row = conn.execute(
        """
        SELECT id
        FROM camera_devices
        WHERE is_active = 1 AND lifecycle_status = 'Ready'
        ORDER BY is_default DESC, id ASC
        LIMIT 1
        """
    ).fetchone()
    return row["id"] if row else None


def _camera_exists_active(conn, camera_id):
    if not camera_id:
        return False
    return conn.execute(
        "SELECT id FROM camera_devices WHERE id = ? AND is_active = 1 AND lifecycle_status = 'Ready'",
        (camera_id,),
    ).fetchone() is not None


def _fetch_room_coverage(conn):
    """Return active rooms and age-aware stored checks without device access."""
    from services.camera_health import describe_stored_camera_health
    from datetime import datetime, timezone
    rows = conn.execute("""
        SELECT r.*, b.name AS building_name, f.name AS floor_name,
               COUNT(DISTINCT c.id) AS assigned_class_count
        FROM rooms r JOIN floors f ON r.floor_id = f.id
        JOIN buildings b ON f.building_id = b.id
        LEFT JOIN classes c ON c.room_id = r.id
        WHERE r.is_active = 1 AND f.is_active = 1 AND b.is_active = 1
        GROUP BY r.id
        ORDER BY b.name COLLATE NOCASE, f.sort_order, f.name COLLATE NOCASE, r.room_code COLLATE NOCASE
    """).fetchall()
    cameras = conn.execute("""SELECT room_id, lifecycle_status, last_test_status, last_health_check_at
        FROM camera_devices WHERE is_active = 1 AND room_id IS NOT NULL""").fetchall()
    now = datetime.now(timezone.utc)
    by_room = {}
    for camera in cameras:
        by_room.setdefault(camera["room_id"], []).append(describe_stored_camera_health(camera, now=now))
    coverage = []
    priority = ("Retired", "Maintenance", "Failed", "Stale", "Not tested", "Healthy")
    for row in rows:
        data = _serialize_room(row)
        observations = by_room.get(row["id"], [])
        labels = [item["label"] for item in observations]
        worst = next((label for label in priority if label in labels), "No Camera")
        detail = next((item["detail"] for item in observations if item["label"] == worst),
                      "No active camera is assigned. Manual Mode is available.")
        data.update({"camera_count":len(observations),
            "assigned_class_count":int(row["assigned_class_count"] or 0),
            "health_state":"Not Tested" if worst == "Not tested" else worst,
            "health_detail":detail,
            "healthy_count":labels.count("Healthy"), "failed_count":labels.count("Failed"),
            "not_tested_count":labels.count("Not tested"), "maintenance_count":labels.count("Maintenance"),
            "stale_count":labels.count("Stale"), "retired_count":labels.count("Retired")})
        coverage.append(data)
    return coverage


def _camera_coverage_summary(conn):
    coverage = _fetch_room_coverage(conn)
    return {
        "total_rooms": len(coverage),
        "rooms_without_camera": sum(1 for room in coverage if room["camera_count"] == 0),
    }


def _mark_camera_health_from_session(conn, camera_id, status, reason):
    if not camera_id:
        return
    conn.execute(
        """
        UPDATE camera_devices
        SET last_test_status = ?,
            last_health_check_at = CURRENT_TIMESTAMP,
            last_successful_frame = CASE WHEN ? = 'ok' THEN CURRENT_TIMESTAMP ELSE last_successful_frame END,
            failure_count = CASE WHEN ? = 'ok' THEN 0 ELSE failure_count + 1 END,
            last_failure_reason = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, status, status, reason, camera_id),
    )


def _class_with_teacher_and_camera(conn, class_id):
    return conn.execute(
        """
        SELECT c.*, u.username AS teacher_name, cd.id AS resolved_camera_id,
               cd.name AS camera_name,
               cd.placement_notes AS camera_placement,
               r.room_code, r.room_name, b.name AS building, f.name AS floor,
               r.department, r.room_type
        FROM classes c
        JOIN users u ON c.teacher_id = u.id
        LEFT JOIN rooms r ON c.room_id = r.id
        LEFT JOIN floors f ON r.floor_id = f.id
        LEFT JOIN buildings b ON f.building_id = b.id
        LEFT JOIN camera_devices cd ON cd.room_id = c.room_id AND cd.is_active = 1 AND cd.lifecycle_status = 'Ready'
        WHERE c.id = ?
        """,
        (class_id,),
    ).fetchone()


def _can_view_class_history(class_info, user_id, role):
    return role == "Dean" or class_info["teacher_id"] == user_id


def _current_attendance_status_expr(alias="a"):
    return f"""
        CASE
            WHEN {alias}.status = 'Manual_Override' THEN COALESCE((
                SELECT aca.new_status
                FROM attendance_change_audit aca
                WHERE aca.attendance_record_id = {alias}.id
                ORDER BY aca.changed_at DESC, aca.id DESC
                LIMIT 1
            ), (
                SELECT al.new_status
                FROM audit_logs al
                WHERE al.attendance_record_id = {alias}.id
                ORDER BY al.timestamp DESC
                LIMIT 1
            ), 'Present')
            ELSE {alias}.status
        END
    """


def _session_counts(records):
    counts = {"Present": 0, "Late": 0, "Absent": 0, "Excused": 0, "Total": len(records)}
    for record in records:
        status = record.get("status") or "Absent"
        if status == 'Pending':
            counts.setdefault('Pending', 0)
        if status in counts:
            counts[status] += 1
    return counts


def _session_report(conn, class_id, session_id):
    from services.reporting import session_report_rows
    class_info = _class_with_teacher_and_camera(conn, class_id)
    if not class_info:
        return None, None, [], []

    session_row = conn.execute(
        """
        SELECT ats.*, cd.name AS camera_name
        FROM attendance_sessions ats
        LEFT JOIN camera_devices cd ON ats.camera_id = cd.id
        WHERE ats.id = ? AND ats.class_id = ?
        """,
        (session_id, class_id),
    ).fetchone()
    if not session_row:
        return class_info, None, [], []

    record_dicts = [dict(row, internal_student_id=row['student_pk'],
                        student_id=row['student_public_id'], name=row['student_name'],
                        timestamp=row['recorded_at'], updated_at=row['corrected_at'],
                        attendance_record_id=row['record_id'])
                    for row in session_report_rows(conn, class_id, session_id)]
    audits = conn.execute(
        """
        SELECT aca.*, s.student_id AS public_student_id, s.name AS student_name,
               u.username AS actor_name
        FROM attendance_change_audit aca
        JOIN students s ON aca.student_id = s.id
        JOIN users u ON aca.actor_user_id = u.id
        WHERE aca.attendance_session_id = ?
        ORDER BY aca.changed_at DESC, aca.id DESC
        """,
        (session_id,),
    ).fetchall()
    for record in record_dicts:
        record['initial_manual'] = record['source'] == 'manual_edit' and record['manual_kind'] == 'initial_manual'
    return class_info, session_row, record_dicts, audits


def _close_open_session_for_class(conn, class_id, reason):
    open_session = get_open_attendance_session(conn, class_id)
    if not open_session:
        return None
    close_attendance_session(conn, open_session["id"], class_id, reason)
    return open_session["id"]


VALID_YEAR_LEVELS = (
    "1st Year",
    "2nd Year",
    "3rd Year",
    "4th Year",
    "5th Year",
    "6th Year",
    "Irregular",
)

ROOM_TYPES = ("Classroom", "Laboratory", "Office", "Lecture Hall")
CAMERA_LIFECYCLE_STATUSES = ("Planned", "Installed", "Testing", "Ready", "Maintenance", "Retired")
