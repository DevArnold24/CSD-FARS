import json
import base64
import os
import uuid
import numpy as np
import sqlite3
from urllib.parse import parse_qsl, urlencode

from flask import abort, current_app, jsonify, request, session, render_template, flash, redirect, url_for

from core import get_logger
from core.routing import parse_record_id
from core.utils import *
from security.rbac import current_user_has_permission, has_permission, permission_required, role_required
from security.session_auth import authenticated_session_user
from database.db_manager import get_db_connection, managed_transaction
from core.ai_pipeline import extract_embedding, validate_single_pose_quality
from services.audit import log_system_event
from routes.blueprints import students_bp
import config

logger = get_logger("students")


def _student_history_time_expr(alias="a"):
    """Match reporting: legacy records use UTC; session records use wall time."""
    return (
        f"CASE WHEN {alias}.attendance_session_id IS NULL "
        f"THEN {_institution_local_datetime_expr(alias + '.timestamp')} "
        f"ELSE {alias}.timestamp END"
    )


def _canonical_student_id(value):
    """Normalize the institution-issued identity used for collision checks."""
    return str(value or "").strip().upper()


def _positive_record_id(value):
    """Accept integer IDs or their form-string representation, never coercions."""
    return parse_record_id(value)


def _student_write_actor(conn, *permissions):
    """Bind student writes to current credentials and permissions under the lock."""
    actor = authenticated_session_user(conn, session)
    if not actor or actor["requires_password_change"]:
        session.clear()
        return None, (jsonify({"error": "Your login session expired.",
                               "session_expired": True, "login_url": url_for("auth.login")}), 401)
    if not any(has_permission(actor["role"], permission) for permission in permissions):
        return None, (jsonify({"error": "Forbidden"}), 403)
    return actor, None


def _student_metadata_payload():
    # Preserve the shared JSON error contract for syntactically broken JSON;
    # validate otherwise parseable values locally before using their fields.
    data = request.get_json()
    if not isinstance(data, dict):
        return None, "Send a valid student details object."
    for field, label in (("student_id", "Student ID"), ("name", "Full Name"), ("year_level", "Year Level")):
        if not isinstance(data.get(field, ""), str):
            return None, f"{label} must be text."
    raw_program = data.get("program_id", "")
    if raw_program is not None and raw_program != "":
        program_id = _positive_record_id(raw_program)
        if program_id is None:
            return None, "Invalid Program ID. Select an academic program."
        data["program_id"] = program_id
    return data, None


def _student_with_canonical_id(conn, student_id, exclude_pk=None):
    query = "SELECT id, is_active, permanently_removed_at FROM students WHERE UPPER(TRIM(student_id)) = ?"
    params = [_canonical_student_id(student_id)]
    if exclude_pk is not None:
        query += " AND id != ?"
        params.append(exclude_pk)
    return conn.execute(query, params).fetchone()


def _owned_registration_request(conn, request_id):
    """Select only a teacher's own request metadata, never its biometric payload."""
    return conn.execute(
        """
        SELECT id, student_id, name, program_id, year_level, status, review_note
        FROM pending_student_registrations
        WHERE id = ? AND submitted_by_user_id = ?
        """,
        (request_id, session.get("user_id")),
    ).fetchone()


@students_bp.route("/students/requests")
@login_required
@role_required("Teacher")
def my_registration_requests():
    """Show a teacher's submission outcomes without exposing another submitter."""
    selected_status = request.args.get("status", "").strip()
    query = request.args.get("q", "").strip()[:120]
    statuses = ("Pending", "Approved", "Rejected")
    validation_error = None
    if selected_status and selected_status not in statuses:
        validation_error = "Choose Pending, Approved, Rejected, or All statuses."
        selected_status = ""
    page = max(1, request.args.get("page", 1, type=int) or 1)
    page_size = 20
    clauses = ["psr.submitted_by_user_id = ?"]
    params = [session.get("user_id")]
    if selected_status:
        clauses.append("psr.status = ?")
        params.append(selected_status)
    if query:
        # INSTR treats wildcard-like input literally and all values stay bound.
        clauses.append("(INSTR(LOWER(psr.student_id), LOWER(?)) > 0 OR INSTR(LOWER(psr.name), LOWER(?)) > 0)")
        params.extend([query, query])
    where = " AND ".join(clauses)
    with managed_transaction(commit=False) as conn:
        counts = {status: 0 for status in statuses}
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM pending_student_registrations WHERE submitted_by_user_id = ? GROUP BY status",
            (session.get("user_id"),),
        ):
            counts[row["status"]] = row["count"]
        total = conn.execute(
            f"SELECT COUNT(*) FROM pending_student_registrations psr WHERE {where}", params
        ).fetchone()[0]
        page_count = max(1, (total + page_size - 1) // page_size)
        page = min(page, page_count)
        rows = conn.execute(
            f"""
            SELECT psr.id, psr.student_id, psr.name, psr.year_level, psr.status,
                   psr.submitted_at, psr.reviewed_at, psr.review_note,
                   ap.name AS program_name,
                   EXISTS(SELECT 1 FROM students s
                          WHERE UPPER(TRIM(s.student_id)) = UPPER(TRIM(psr.student_id))) AS already_registered,
                   EXISTS(SELECT 1 FROM pending_student_registrations other
                          WHERE UPPER(TRIM(other.student_id)) = UPPER(TRIM(psr.student_id))
                            AND other.status = 'Pending') AS has_pending_request
            FROM pending_student_registrations psr
            LEFT JOIN academic_programs ap ON ap.id = psr.program_id
            WHERE {where}
            ORDER BY psr.submitted_at DESC, psr.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    return render_template(
        "students/requests.html", registration_requests=rows, counts=counts,
        statuses=statuses, selected_status=selected_status, query=query,
        page=page, page_count=page_count, total=total,
        validation_error=validation_error,
    ), 400 if validation_error else 200


def _three_pose_image_input(data):
    """Return exactly Center/Left/Right captures without retaining raw images."""
    raw_images = data.get("images_base64")
    if raw_images is None:
        raw_images = data.get("images")
    legacy_image = data.get("image_base64", "")

    if raw_images is not None:
        if (not isinstance(raw_images, list) or len(raw_images) != 3
                or not all(isinstance(image, str) and image.strip() for image in raw_images)):
            return None, "Exactly three captured poses are required: Center, Left, and Right."
        return raw_images, None

    # Retain concise legacy test fixtures while production requests are required
    # to submit the complete three-pose browser workflow.
    if current_app.testing and legacy_image == "MOCK_BASE64_TESTING":
        return [legacy_image, legacy_image, legacy_image], None
    if legacy_image:
        return None, "Exactly three captured poses are required: Center, Left, and Right."
    return None, "Center, Left, and Right captures are required for enrollment."


def _extract_three_face_blobs(image_input):
    import struct

    mock_vec = struct.pack(f"{128}d", *[0.1] * 128)
    face_blobs = []
    for image in image_input:
        if current_app.testing and image == "MOCK_BASE64_TESTING":
            face_blobs.append(mock_vec)
            continue
        blob = extract_embedding(image)
        if isinstance(blob, list):
            if len(blob) != 1:
                raise ValueError("Each pose must contain exactly one face representation.")
            blob = blob[0]
        face_blobs.append(blob)

    if len(face_blobs) != 3 or not all(face_blobs):
        raise ValueError("Exactly three valid face representations are required.")
    return face_blobs


@students_bp.route("/students/register")
@login_required
@permission_required("submit_student_registration", "register_students_direct")
def student_registration():
    """Render the Student Registration and Biometric Enrollment UI."""
    correction = None
    reenrollment = None
    if "reenroll" in request.args:
        if not current_user_has_permission("manage_student_biometrics"):
            abort(403)
        if "resubmit" in request.args:
            abort(400)
    with managed_transaction(commit=False) as conn:
        programs = conn.execute("SELECT * FROM academic_programs WHERE is_active = 1 ORDER BY name ASC").fetchall()
        if "reenroll" in request.args:
            reenrollment = conn.execute(
                """SELECT s.id, s.student_id, s.name, s.program_id, s.year_level,
                          s.is_active, s.permanently_removed_at, ap.name AS program_name
                   FROM students s JOIN academic_programs ap ON ap.id = s.program_id
                   WHERE s.id = ?""",
                (request.args.get("reenroll", type=int),),
            ).fetchone()
            if reenrollment is None or reenrollment["permanently_removed_at"] is not None:
                abort(404)
            if not reenrollment["is_active"]:
                flash("This student is inactive. Ask the Dean to reactivate the student before updating face enrollment.", "warning")
                return redirect(url_for("students.student_profile", student_id=reenrollment["id"]))
        if "resubmit" in request.args:
            if session.get("role") != "Teacher":
                abort(404)
            request_id = request.args.get("resubmit", type=int)
            correction = _owned_registration_request(conn, request_id)
            if correction is None:
                abort(404)
            if correction["status"] != "Rejected":
                flash("Only a rejected request can be corrected and resubmitted.", "warning")
                return redirect(url_for("students.my_registration_requests"))
            if (_student_with_canonical_id(conn, correction["student_id"])
                    or _pending_student_registration_exists(conn, correction["student_id"])):
                flash("This Student ID is already registered or has a pending request. Check My Registration Requests or contact the Office Admin.", "warning")
                return redirect(url_for("students.my_registration_requests"))
    quality_config = {
        "min_brightness": getattr(config, "ENROLLMENT_MIN_BRIGHTNESS", 45.0),
        "max_brightness": getattr(config, "ENROLLMENT_MAX_BRIGHTNESS", 220.0),
        "min_blur_variance": getattr(config, "ENROLLMENT_MIN_BLUR_VARIANCE", 40.0),
        "min_face_area_ratio": getattr(config, "ENROLLMENT_MIN_FACE_AREA_RATIO", 0.03),
        "min_edge_margin_ratio": getattr(config, "ENROLLMENT_MIN_EDGE_MARGIN_RATIO", 0.02),
    }
    return render_template("students/register.html", programs=programs, quality_config=quality_config,
                           correction=correction, reenrollment=reenrollment)


@students_bp.route("/api/students/validate-pose", methods=["POST"])
@login_required
@permission_required("submit_student_registration", "register_students_direct", "manage_student_biometrics")
def api_validate_pose():
    """API endpoint to perform fast quality check on a single captured pose image."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"valid": False, "error": "Send a valid pose capture request."}), 400
    image_base64 = data.get("image_base64") or data.get("image")
    pose_name = data.get("pose_name", "Pose")

    if not isinstance(image_base64, str) or not image_base64.strip():
        return jsonify({"valid": False, "error": "An image is required for pose validation."}), 400
    if not isinstance(pose_name, str) or not pose_name.strip() or len(pose_name) > 80:
        return jsonify({"valid": False, "error": "Select a valid pose and capture it again."}), 400

    try:
        metrics = validate_single_pose_quality(image_base64)
        return jsonify({
            "valid": True,
            "message": f"{pose_name} validated successfully.",
            "metrics": metrics
        }), 200
    except ValueError as ve:
        return jsonify({"valid": False, "error": str(ve)}), 400
    except Exception as e:
        logger.exception("Error during pose validation.")
        return jsonify({"valid": False, "error": "An internal server error occurred during pose validation."}), 500



@students_bp.route("/students")
@login_required
@permission_required("view_student_directory")
def student_directory():
    """Render the global student directory for academic administrators."""
    with managed_transaction(commit=False) as conn:
        programs = conn.execute(
            """SELECT ap.* FROM academic_programs ap
               WHERE ap.is_active = 1 OR EXISTS (
                   SELECT 1 FROM students s WHERE s.program_id = ap.id AND s.permanently_removed_at IS NULL
                   AND (s.is_active = 1 OR ? = 1)
               ) ORDER BY ap.name ASC""", (int(current_user_has_permission("manage_student_status")),)
        ).fetchall()
        classes = []
        if current_user_has_permission("manage_rosters"):
            classes = conn.execute(
                """
                SELECT c.id, c.class_code, c.course_name, r.room_code AS room_number, u.username AS teacher_name
                FROM classes c
                JOIN users u ON c.teacher_id = u.id AND u.role = 'Teacher'
                LEFT JOIN rooms r ON c.room_id = r.id
                WHERE c.is_archived = 0
                ORDER BY c.course_name ASC, c.class_code ASC
                """
            ).fetchall()
    return render_template("students/directory.html", programs=programs, classes=classes)


@students_bp.route("/api/students/directory", methods=["GET"])
@login_required
@permission_required("view_student_directory")
def api_students_directory():
    """Return a paged directory with totals for the complete authorized filter."""
    q = request.args.get("q", "").strip()
    program_raw = request.args.get("program_id", "").strip()
    class_raw = request.args.get("class_id", "").strip()
    year = request.args.get("year_level", "").strip()
    biometric = request.args.get("biometric_status", "").strip()
    roster = request.args.get("roster_status", "").strip()
    attendance = request.args.get("attendance_status", "").strip()
    status = request.args.get("student_status", "active").strip() or "active"
    can_view_class_data = current_user_has_permission("manage_rosters")
    can_view_attendance = current_user_has_permission("view_attendance_reports")
    can_manage_status = current_user_has_permission("manage_student_status")
    if ((class_raw or roster) and not can_view_class_data) or (attendance and not can_view_attendance):
        return jsonify({"error": "Forbidden"}), 403
    if status in ("inactive", "all") and not can_manage_status:
        return jsonify({"error": "Forbidden"}), 403
    if (status not in ("active", "inactive", "all") or
            biometric not in ("", "ready", "consent_missing", "embedding_missing", "needs_attention") or
            roster not in ("", "enrolled", "unenrolled") or attendance not in ("", "seen", "never") or
            (year not in ("", "All Years") and year not in VALID_YEAR_LEVELS)):
        return jsonify({"error": "Choose an available directory filter or clear the filters."}), 400
    if len(q) > 120:
        return jsonify({"error": "Keep the search to 120 characters or fewer."}), 400
    program_id = None if program_raw in ("", "All Programs") else _positive_record_id(program_raw)
    class_id = _positive_record_id(class_raw) if class_raw else None
    if (program_raw not in ("", "All Programs") and program_id is None) or (class_raw and class_id is None):
        return jsonify({"error": "Select a valid program or class."}), 400
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 200)
    except ValueError:
        limit = 100
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        offset = 0
    clauses, params = ["s.permanently_removed_at IS NULL"], []
    if status != "all":
        clauses.append("s.is_active = ?")
        params.append(int(status == "active"))
    if q:
        clauses.append("(INSTR(LOWER(s.name), LOWER(?)) > 0 OR INSTR(LOWER(s.student_id), LOWER(?)) > 0)")
        params.extend([q, q])
    if program_id is not None:
        clauses.append("s.program_id = ?")
        params.append(program_id)
    if year not in ("", "All Years"):
        clauses.append("s.year_level = ?")
        params.append(year)
    if class_id is not None:
        clauses.append("s.id IN (SELECT student_id FROM class_enrollments WHERE class_id = ?)")
        params.append(class_id)
    ready = "s.is_active = 1 AND s.consent_granted = 1 AND COALESCE(e.embedding_count, 0) > 0"
    if biometric == "ready":
        clauses.append("(" + ready + ")")
    elif biometric == "needs_attention":
        clauses.append("NOT (" + ready + ")")
    elif biometric == "consent_missing":
        clauses.append("s.consent_granted = 0")
    elif biometric == "embedding_missing":
        clauses.append("s.consent_granted = 1 AND COALESCE(e.embedding_count, 0) = 0")
    if roster:
        clauses.append("COALESCE(c.class_count, 0) " + ("> 0" if roster == "enrolled" else "= 0"))
    if attendance:
        clauses.append("a.latest_attendance_at IS " + ("NOT NULL" if attendance == "seen" else "NULL"))
    # Aggregate each relation independently; joining every face, class and
    # attendance row first multiplies the workload for long-lived records.
    query = f"""
        SELECT s.id, s.student_id, s.name, ap.name AS program, s.year_level,
               s.is_active, s.consent_granted, COALESCE(e.embedding_count, 0) AS embedding_count,
               COALESCE(c.class_count, 0) AS class_count, c.class_codes, a.latest_attendance_at
        FROM students s JOIN academic_programs ap ON s.program_id = ap.id
        LEFT JOIN (SELECT student_id, COUNT(*) AS embedding_count FROM embeddings GROUP BY student_id) e ON e.student_id = s.id
        LEFT JOIN (
            SELECT ce.student_id, COUNT(DISTINCT cls.id) AS class_count, GROUP_CONCAT(DISTINCT cls.class_code) AS class_codes
            FROM class_enrollments ce JOIN classes cls ON ce.class_id = cls.id AND cls.is_archived = 0
            GROUP BY ce.student_id
        ) c ON c.student_id = s.id
        LEFT JOIN (
            SELECT student_id, MAX({_student_history_time_expr('history_row')}) AS latest_attendance_at
            FROM attendance_records history_row GROUP BY student_id
        ) a ON a.student_id = s.id
        WHERE """ + " AND ".join(clauses)
    try:
        with managed_transaction(commit=False) as conn:
            # Counts and rows must describe the same snapshot if another user
            # registers or deactivates a student between the two reads.
            conn.execute("BEGIN")
            if class_id is not None:
                selected_class = conn.execute("SELECT is_archived FROM classes WHERE id = ?", (class_id,)).fetchone()
                if not selected_class:
                    return jsonify({"error": "Class not found."}), 404
                if selected_class["is_archived"]:
                    return jsonify({"error": "That class has been archived. Clear Filters or select an active class."}), 409
            totals = conn.execute("""SELECT COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN is_active = 1 AND consent_granted = 1 AND embedding_count > 0 THEN 1 ELSE 0 END), 0) AS ready,
                COALESCE(SUM(CASE WHEN class_count = 0 THEN 1 ELSE 0 END), 0) AS unenrolled,
                COALESCE(SUM(CASE WHEN latest_attendance_at IS NULL THEN 1 ELSE 0 END), 0) AS never_attended
                FROM (""" + query + ") matched", params).fetchone()
            total = totals["total"]
            # Recover an old bookmarked page if filtering or removals shortened it.
            offset = min(offset, ((max(total, 1) - 1) // limit) * limit)
            rows = conn.execute(query + " ORDER BY program ASC, s.year_level ASC, s.name ASC, s.id ASC LIMIT ? OFFSET ?",
                                [*params, limit, offset]).fetchall()
            summary = {"total": total, "ready": totals["ready"], "needs_setup": total - totals["ready"]}
            if can_view_class_data:
                summary["unenrolled"] = totals["unenrolled"]
            if can_view_attendance:
                summary["never_attended"] = totals["never_attended"]
            students = []
            for row in rows:
                label = "Ready"
                if not row["is_active"]:
                    label = "Deactivated"
                elif not row["consent_granted"]:
                    label = "Consent Missing"
                elif row["embedding_count"] == 0:
                    label = "Embedding Missing"
                student = {"id": row["id"], "student_id": row["student_id"], "name": row["name"],
                           "program": row["program"], "year_level": row["year_level"],
                           "consent_granted": bool(row["consent_granted"]), "biometric_status": label}
                if can_manage_status:
                    student["is_active"] = bool(row["is_active"])
                if can_view_class_data:
                    student.update(class_count=row["class_count"], class_codes=row["class_codes"] or "")
                if can_view_attendance:
                    student["latest_attendance_at"] = row["latest_attendance_at"]
                students.append(student)
            return jsonify({"students": students, "summary": summary, "limit": limit, "offset": offset,
                            "has_next": offset + len(students) < total, "has_previous": offset > 0}), 200
    except Exception:
        logger.exception("Error loading student directory.")
        return jsonify({"error": "An internal database error occurred."}), 500


@students_bp.route("/api/students/enroll", methods=["POST"])
@login_required
@permission_required("submit_student_registration", "register_students_direct")
def api_student_enroll():
    """Create a direct registration or stage a Teacher registration request."""
    data, payload_error = _student_metadata_payload()
    if payload_error:
        return jsonify({"error": payload_error}), 400

    student_id = _canonical_student_id(data.get("student_id", ""))
    name = data.get("name", "").strip()
    program_id = data.get("program_id", "")
    year_level = data.get("year_level", "").strip()
    consent_granted = data.get("consent_granted", False)
    image_input, image_error = _three_pose_image_input(data)
    is_mock_enrollment = bool(image_input) and current_app.testing and all(
        image == "MOCK_BASE64_TESTING" for image in image_input
    )
    if consent_granted is not True:
        return jsonify({"error": "Biometric consent must be explicitly granted."}), 400
    if image_error:
        return jsonify({"error": image_error}), 400

    # Test-only fallback keeps older mock enrollment fixtures concise while
    # real enrollment requests must provide explicit academic categorization.
    if not program_id and is_mock_enrollment:
        with managed_transaction(commit=True) as conn:
            row = conn.execute("SELECT id FROM academic_programs WHERE name = ?", ("General / Undecided",)).fetchone()
            if row:
                program_id = row["id"]
            else:
                cursor = conn.execute("INSERT INTO academic_programs (name, is_active) VALUES (?, 1)", ("General / Undecided",))
                program_id = cursor.lastrowid

    if not year_level and is_mock_enrollment:
        year_level = "1st Year"

    if not student_id or not name or not program_id or not year_level:
        return jsonify({"error": "Student ID, Name, Program, and Year Level are required."}), 400
    if year_level not in VALID_YEAR_LEVELS:
        return jsonify({"error": "Invalid Year Level."}), 400

    try:
        program_id = int(program_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid Program ID."}), 400

    actor_user_id = session.get("user_id")
    actor_role = session.get("role")
    resubmit_id = data.get("resubmit_request_id")
    if resubmit_id is not None:
        if actor_role != "Teacher":
            return jsonify({"error": "Only the submitting teacher can resubmit a rejected request."}), 403
        resubmit_id = _positive_record_id(resubmit_id)
        if resubmit_id is None:
            return jsonify({"error": "Select a rejected request from My Registration Requests."}), 400
    try:
        face_blobs = _extract_three_face_blobs(image_input)

        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            actor, denied = _student_write_actor(conn, "submit_student_registration", "register_students_direct")
            if denied is not None:
                return denied
            actor_user_id, actor_role = actor["id"], actor["role"]
            if resubmit_id is not None:
                if actor_role != "Teacher":
                    return jsonify({"error": "Only the submitting teacher can resubmit a rejected request."}), 403
                original = _owned_registration_request(conn, resubmit_id)
                if original is None:
                    return jsonify({"error": "Registration request not found."}), 404
                if original["status"] != "Rejected":
                    return jsonify({"error": "Only a rejected request can be corrected and resubmitted."}), 409
            # Check program existence
            prog = conn.execute(
                "SELECT id FROM academic_programs WHERE id = ? AND is_active = 1",
                (program_id,),
            ).fetchone()
            if not prog:
                return jsonify({"error": "Select an active academic program."}), 400

            existing_student = _student_with_canonical_id(conn, student_id)
            if existing_student:
                message = "This Student ID is already registered."
                if actor_role == "Office_Admin":
                    message += " Use biometric re-enrollment without changing student metadata."
                return jsonify({"error": message}), 409

            if _pending_student_registration_exists(conn, student_id):
                return jsonify({
                    "error": "A pending registration approval request already exists for this Student ID. Check My Registration Requests or contact the Office Admin; another request was not created."
                    if actor_role == "Teacher" else
                    "A pending registration approval request already exists for this Student ID. Review that request instead of creating another registration."
                }), 409

            if actor_role == "Teacher":

                from services.biometric_security import is_encrypted, encrypt_face_vector
                encrypted_blobs = [
                    b if is_encrypted(b) else encrypt_face_vector(b) for b in face_blobs
                ]
                if len(encrypted_blobs) == 1:
                    staged_vector = encrypted_blobs[0]
                else:
                    staged_vector = json.dumps([base64.b64encode(b).decode("ascii") for b in encrypted_blobs]).encode("utf-8")

                cursor = conn.execute(
                    """
                    INSERT INTO pending_student_registrations
                        (student_id, name, program_id, year_level, consent_granted, face_vector, submitted_by_user_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (student_id, name, program_id, year_level, consent_granted, staged_vector, actor_user_id),
                )
                log_system_event(
                    conn,
                    "student_registration_submitted",
                    actor_user_id=actor_user_id,
                    details={"request_id": cursor.lastrowid, "student_id": student_id,
                             "submitted_by_role": actor_role, "resubmitted_from_request_id": resubmit_id},
                )
                logger.info("Teacher submitted student '%s' for administrative approval.", student_id)
                return jsonify({
                    "message": f"Student registration for {name} submitted for administrative approval.",
                    "status": "Pending",
                    "request_id": cursor.lastrowid,
                }), 202

            _upsert_student_with_embedding(
                conn,
                student_id,
                name,
                program_id,
                year_level,
                consent_granted,
                face_blobs,
                allow_update=False,
            )
        
        logger.info("Student '%s' enrolled successfully.", student_id)
        return jsonify({"message": f"Student {name} enrolled successfully."}), 200
        
    except ValueError as ve:
        # Expected AI Pipeline errors (e.g. no face found)
        return jsonify({"error": str(ve)}), 400
    except sqlite3.IntegrityError:
        return jsonify({"error": "This Student ID already has an active or pending registration."}), 409
    except Exception as e:
        logger.exception("Error during student enrollment.")
        return jsonify({"error": "An internal server error occurred."}), 500


@students_bp.route("/students/approvals")
@login_required
@permission_required("approve_student_registrations")
def student_approvals():
    """Render the authorized approval queue for Teacher-submitted registrations."""
    with managed_transaction(commit=False) as conn:
        pending_requests = conn.execute(
            """
            SELECT
                psr.id,
                psr.student_id,
                psr.name,
                psr.year_level,
                psr.submitted_at,
                ap.name AS program_name,
                u.username AS submitted_by_username
            FROM pending_student_registrations psr
            JOIN academic_programs ap ON ap.id = psr.program_id
            JOIN users u ON u.id = psr.submitted_by_user_id
            WHERE psr.status = 'Pending'
            ORDER BY psr.submitted_at ASC
            """
        ).fetchall()
    return render_template("students/approvals.html", pending_requests=pending_requests)


@students_bp.route("/students/approvals/history")
@login_required
@permission_required("approve_student_registrations")
def student_approval_history():
    """Read-only decisions for the same reviewers who can access the queue."""
    statuses = ("Approved", "Rejected")
    selected_status = request.args.get("status", "").strip()
    query = request.args.get("q", "").strip()[:120]
    validation_error = None
    clauses = ["psr.status IN ('Approved', 'Rejected')"]
    params = []
    if selected_status:
        if selected_status not in statuses:
            validation_error = "Choose Approved, Rejected, or All decisions."
            clauses.append("0")
        else:
            clauses.append("psr.status = ?")
            params.append(selected_status)
    if query:
        clauses.append("(INSTR(LOWER(psr.student_id), LOWER(?)) > 0 OR INSTR(LOWER(psr.name), LOWER(?)) > 0)")
        params.extend([query, query])
    page = max(1, request.args.get("page", 1, type=int) or 1)
    page_size = 20
    where = " AND ".join(clauses)
    with managed_transaction(commit=False) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM pending_student_registrations psr WHERE {where}", params,
        ).fetchone()[0]
        page_count = max(1, (total + page_size - 1) // page_size)
        page = min(page, page_count)
        decisions = conn.execute(
            f"""SELECT psr.id, psr.student_id, psr.name, psr.year_level,
                       psr.status, psr.submitted_at, psr.reviewed_at, psr.review_note,
                       ap.name AS program_name, submitter.username AS submitted_by,
                       reviewer.username AS reviewed_by
                FROM pending_student_registrations psr
                LEFT JOIN academic_programs ap ON ap.id = psr.program_id
                LEFT JOIN users submitter ON submitter.id = psr.submitted_by_user_id
                LEFT JOIN users reviewer ON reviewer.id = psr.reviewed_by_user_id
                WHERE {where}
                ORDER BY psr.reviewed_at DESC, psr.id DESC LIMIT ? OFFSET ?""",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    return render_template(
        "students/approval_history.html", decisions=decisions, statuses=statuses,
        selected_status=selected_status, query=query, validation_error=validation_error,
        total=total, page=page, page_count=page_count,
    ), 400 if validation_error else 200


def _registration_review_note():
    # Empty bodies remain supported for existing approve/reject callers. A
    # nonempty malformed body must never silently become an empty decision.
    data = request.get_json(silent=True) if request.get_data() else {}
    if not isinstance(data, dict) or not isinstance(data.get("review_note", ""), str):
        return None, "Send a valid review request with an optional text note."
    note = data.get("review_note", "").strip()
    if len(note) > 1000:
        return None, "Keep the review note to 1,000 characters or fewer."
    return note, None


@students_bp.route("/api/students/approvals/<int:request_id>/approve", methods=["POST"])
@login_required
@permission_required("approve_student_registrations")
def api_approve_student_registration(request_id):
    """Approve a pending Teacher-submitted registration and activate the student."""
    actor_user_id = session.get("user_id")
    review_note, payload_error = _registration_review_note()
    if payload_error:
        return jsonify({"error": payload_error}), 400

    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _, denied = _student_write_actor(conn, "approve_student_registrations")
            if denied is not None:
                return denied
            pending = conn.execute(
                """
                SELECT *
                FROM pending_student_registrations
                WHERE id = ?
                """,
                (request_id,),
            ).fetchone()

            if not pending:
                return jsonify({"error": "Approval request not found."}), 404
            if pending["status"] != "Pending":
                return jsonify({
                    "error": f"This registration request was already {pending['status'].lower()}."
                }), 409
            if pending["face_vector"] is None:
                return jsonify({"error": "This approval request no longer contains a biometric vector."}), 400
            if pending["consent_granted"] != 1:
                return jsonify({"error": "Biometric consent was not explicitly granted."}), 409
            active_program = conn.execute(
                "SELECT 1 FROM academic_programs WHERE id = ? AND is_active = 1",
                (pending["program_id"],),
            ).fetchone()
            if not active_program:
                return jsonify({"error": "Activate the request's academic program before approval."}), 409

            existing_student = _student_with_canonical_id(conn, pending["student_id"])
            if existing_student:
                return jsonify({
                    "error": "This Student ID became registered before this request was approved; no data was overwritten."
                }), 409

            face_vector = pending["face_vector"]
            from services.biometric_security import is_encrypted, decrypt_face_vector
            if face_vector and is_encrypted(face_vector):
                face_vector = decrypt_face_vector(face_vector)

            student_pk = _upsert_student_with_embedding(
                conn,
                _canonical_student_id(pending["student_id"]),
                pending["name"],
                pending["program_id"],
                pending["year_level"],
                pending["consent_granted"],
                face_vector,
                allow_update=False,
            )
            conn.execute(
                """
                UPDATE pending_student_registrations
                SET status = 'Approved',
                    reviewed_by_user_id = ?,
                    reviewed_at = CURRENT_TIMESTAMP,
                    review_note = ?,
                    face_vector = NULL
                WHERE id = ?
                """,
                (actor_user_id, review_note, request_id),
            )
            log_system_event(
                conn,
                "student_registration_approved",
                actor_user_id=actor_user_id,
                details={
                    "request_id": request_id,
                    "student_pk": student_pk,
                    "student_id": pending["student_id"],
                    "submitted_by_user_id": pending["submitted_by_user_id"],
                },
            )
            logger.info("Authorized reviewer approved student registration request %s.", request_id)
            return jsonify({"message": (
                f"Registration approved for {pending['name']}. "
                "The Teacher can now add the student to a class roster."
            )}), 200
    except sqlite3.IntegrityError:
        return jsonify({"error": "This Student ID became registered before approval; no data was overwritten."}), 409
    except Exception:
        logger.exception("Error approving student registration request %s.", request_id)
        return jsonify({"error": "An internal server error occurred."}), 500
@students_bp.route("/api/students/approvals/<int:request_id>/reject", methods=["POST"])
@login_required
@permission_required("approve_student_registrations")
def api_reject_student_registration(request_id):
    """Reject a pending Teacher-submitted registration and clear its vector."""
    actor_user_id = session.get("user_id")
    review_note, payload_error = _registration_review_note()
    if payload_error:
        return jsonify({"error": payload_error}), 400

    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _, denied = _student_write_actor(conn, "approve_student_registrations")
            if denied is not None:
                return denied
            pending = conn.execute(
                """
                SELECT id, student_id, name, submitted_by_user_id, status
                FROM pending_student_registrations
                WHERE id = ?
                """,
                (request_id,),
            ).fetchone()

            if not pending:
                return jsonify({"error": "Approval request not found."}), 404
            if pending["status"] != "Pending":
                return jsonify({
                    "error": f"This registration request was already {pending['status'].lower()}."
                }), 409

            conn.execute(
                """
                UPDATE pending_student_registrations
                SET status = 'Rejected',
                    reviewed_by_user_id = ?,
                    reviewed_at = CURRENT_TIMESTAMP,
                    review_note = ?,
                    face_vector = NULL
                WHERE id = ?
                """,
                (actor_user_id, review_note, request_id),
            )
            log_system_event(
                conn,
                "student_registration_rejected",
                actor_user_id=actor_user_id,
                details={
                    "request_id": request_id,
                    "student_id": pending["student_id"],
                    "submitted_by_user_id": pending["submitted_by_user_id"],
                },
            )
            logger.info("Authorized reviewer rejected student registration request %s.", request_id)
            return jsonify({"message": f"Student registration for {pending['name']} rejected."}), 200
    except Exception:
        logger.exception("Error rejecting student registration request %s.", request_id)
        return jsonify({"error": "An internal server error occurred."}), 500
@students_bp.route("/api/students/search", methods=["GET"])
@login_required
@permission_required("search_students_for_roster")
def api_students_search():
    """Search authorized roster candidates; opt-in pages preserve array clients."""
    q = request.args.get("q", "").strip()
    program_raw = request.args.get("program_id", "").strip()
    class_raw = request.args.get("class_id", "").strip()
    year = request.args.get("year_level", "").strip()
    class_id = _positive_record_id(class_raw) if class_raw else None
    program_id = _positive_record_id(program_raw) if program_raw not in ("", "All Programs") else None
    if class_raw and class_id is None:
        return jsonify({"error": "A valid class is required."}), 400
    if program_raw not in ("", "All Programs") and program_id is None:
        return jsonify({"error": "Select a valid program."}), 400
    if len(q) > 120 or (year not in ("", "All Years") and year not in VALID_YEAR_LEVELS):
        return jsonify({"error": "Use a search of up to 120 characters and an available year level."}), 400
    paged = request.args.get("paged") == "1"
    try:
        offset = max(0, int(request.args.get("offset", "0"))) if paged else 0
    except ValueError:
        return jsonify({"error": "Choose a valid results page."}), 400
    limit = 50
    query = """SELECT s.id, s.student_id, s.name, ap.name AS program, s.year_level
        FROM students s JOIN academic_programs ap ON s.program_id = ap.id
        WHERE s.is_active = 1 AND s.permanently_removed_at IS NULL"""
    params = []
    if q:
        query += " AND (INSTR(LOWER(s.name), LOWER(?)) > 0 OR INSTR(LOWER(s.student_id), LOWER(?)) > 0)"
        params.extend([q, q])
    if program_id is not None:
        query += " AND s.program_id = ?"
        params.append(program_id)
    if year not in ("", "All Years"):
        query += " AND s.year_level = ?"
        params.append(year)
    if class_id is not None:
        query += " AND s.id NOT IN (SELECT student_id FROM class_enrollments WHERE class_id = ?)"
        params.append(class_id)
    try:
        with managed_transaction(commit=False) as conn:
            # Authorization, total and page all describe one read snapshot.
            conn.execute("BEGIN")
            if class_id is not None:
                roster_class = conn.execute("SELECT teacher_id,is_archived FROM classes WHERE id=?", (class_id,)).fetchone()
                if not roster_class:
                    return jsonify({"error": "Class not found."}), 404
                if roster_class["is_archived"]:
                    return jsonify({"error": "Archived classes cannot authorize roster search."}), 409
                if session.get("role") == "Teacher" and roster_class["teacher_id"] != session.get("user_id"):
                    return jsonify({"error": "You may only search rosters for your own classes."}), 403
            elif session.get("role") == "Teacher":
                return jsonify({"error": "A valid owned class is required."}), 400
            if paged:
                total = conn.execute("SELECT COUNT(*) FROM (" + query + ")", params).fetchone()[0]
                offset = min(offset, max(0, ((total - 1) // limit) * limit))
            rows = conn.execute(query + " ORDER BY s.name COLLATE NOCASE, s.id LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
            students = [dict(row) for row in rows]
            if not paged:
                return jsonify(students), 200
            return jsonify({"students": students, "total": total, "limit": limit, "offset": offset,
                            "has_next": offset + len(students) < total, "has_previous": offset > 0}), 200
    except Exception:
        logger.exception("Error during student search.")
        return jsonify({"error": "Student search is temporarily unavailable. Retry loading."}), 500



def _profile_directory_filters():
    """Keep a small, role-scoped filter context; never accept a return URL."""
    if not current_user_has_permission("view_student_directory"):
        return {}
    raw = request.args.get("directory", "")
    if len(raw) > 2000:
        return {}
    try:
        pairs = parse_qsl(raw, max_num_fields=20)
        if len({key for key, _ in pairs}) != len(pairs):
            return {}
        values = dict(pairs)
        filters = {}
        if values.get("q"):
            if len(values["q"]) > 120:
                return {}
            filters["q"] = values["q"]
        for key in ("program_id", "class_id"):
            if key == "class_id" and not current_user_has_permission("manage_rosters"):
                continue
            if values.get(key):
                identity = _positive_record_id(values[key])
                if identity is None:
                    return {}
                filters[key] = str(identity)
        allowed = {
            "year_level": VALID_YEAR_LEVELS,
            "biometric_status": ("ready", "consent_missing", "embedding_missing", "needs_attention"),
        }
        if current_user_has_permission("manage_rosters"):
            allowed["roster_status"] = ("enrolled", "unenrolled")
        if current_user_has_permission("view_attendance_reports"):
            allowed["attendance_status"] = ("seen", "never")
        if current_user_has_permission("manage_student_status"):
            allowed["student_status"] = ("active", "inactive", "all")
        for key, choices in allowed.items():
            if values.get(key):
                if values[key] not in choices:
                    return {}
                filters[key] = values[key]
        if values.get("offset"):
            offset = values["offset"]
            if not offset.isascii() or not offset.isdecimal() or len(offset) > 16:
                return {}
            offset = int(offset)
            if offset > 2**53 - 1:
                return {}
            if offset:
                filters["offset"] = str(offset)
        return filters
    except ValueError:
        return {}


@students_bp.route("/students/<int:student_id>")
@login_required
@permission_required("view_student_profiles", "view_own_student_profiles")
def student_profile(student_id):
    """Render the Student Profile page."""
    actor_role = session.get("role")
    actor_user_id = session.get("user_id")
    directory_filters = _profile_directory_filters()

    with managed_transaction(commit=False) as conn:
        # Fetch student details
        student = conn.execute(
            """
            SELECT s.*, ap.name AS program_name,
                   (SELECT COUNT(*) FROM embeddings WHERE student_id = s.id) as embedding_count,
                   (SELECT MAX(created_at) FROM embeddings WHERE student_id = s.id) as biometric_updated_at
            FROM students s
            JOIN academic_programs ap ON s.program_id = ap.id
            WHERE s.id = ?
            """,
            (student_id,),
        ).fetchone()

        if not student:
            flash("Student not found.", "error")
            return redirect(url_for(
                "students.student_directory" if current_user_has_permission("view_student_directory")
                else "classes.manage_classes"
            ))
        if student["permanently_removed_at"] is not None:
            abort(404)

        if actor_role == "Teacher":
            teacher_enrollment = conn.execute(
                """
                SELECT 1
                FROM class_enrollments ce
                JOIN classes c ON c.id = ce.class_id
                WHERE ce.student_id = ? AND c.teacher_id = ?
                LIMIT 1
                """,
                (student_id, actor_user_id),
            ).fetchone()
            if not teacher_enrollment:
                abort(403)

        # Fetch enrolled classes
        can_view_enrollments = current_user_has_permission("manage_rosters")
        if actor_role == "Teacher":
            can_view_enrollments = True
            enrolled_classes = conn.execute(
                """
                SELECT c.id, c.class_code, c.course_name, c.days_of_week, c.start_time, c.end_time, c.is_archived, u.username as teacher_name
                FROM class_enrollments ce
                JOIN classes c ON ce.class_id = c.id
                JOIN users u ON c.teacher_id = u.id
                WHERE ce.student_id = ? AND c.teacher_id = ?
                ORDER BY c.is_archived ASC, c.course_name ASC
                """,
                (student_id, actor_user_id),
            ).fetchall()
        elif can_view_enrollments:
            enrolled_classes = conn.execute(
                """
                SELECT c.id, c.class_code, c.course_name, c.days_of_week, c.start_time, c.end_time, c.is_archived, u.username as teacher_name
                FROM class_enrollments ce
                JOIN classes c ON ce.class_id = c.id
                JOIN users u ON c.teacher_id = u.id
                WHERE ce.student_id = ?
                ORDER BY c.is_archived ASC, c.course_name ASC
                """,
                (student_id,),
            ).fetchall()
        else:
            enrolled_classes = []

        # Use the same occurrence/status dataset as reports, without expanding profile access.
        can_view_attendance = current_user_has_permission("view_attendance_reports")
        attendance, history_error, history_total, history_page = [], None, 0, 1
        history_filters = {key: request.args.get('history_' + key, '').strip()
                           for key in ('start_date', 'end_date', 'status')}
        if can_view_attendance:
            from routes.reports import _validated_date_range
            from services.reporting import report_query, display_row, STATUSES
            try:
                history_filters['start_date'], history_filters['end_date'] = _validated_date_range(
                    history_filters['start_date'], history_filters['end_date'], required=False)
                if history_filters['status'] and history_filters['status'] not in STATUSES:
                    raise ValueError('Choose a valid attendance status.')
                history_page = int(request.args.get('history_page', '1'))
                if history_page < 1:
                    raise ValueError('Choose a valid history page.')
                sql, params = report_query({**history_filters, 'student_id': student['student_id']}, actor_user_id, actor_role)
                history_total = conn.execute(sql + 'SELECT COUNT(*) FROM filtered', params).fetchone()[0]
                history_page = min(history_page, max(1, (history_total + 24)//25))
                # A person's history is chronological, unlike the shared class-
                # grouped report. Recorded times are already normalized once;
                # a Pending entry sorts by its exact scheduled occurrence only.
                history_order = "COALESCE(NULLIF(recorded_at,''), session_date || ' ' || scheduled_start_time) DESC, record_id DESC, session_id DESC"
                for row in conn.execute(sql + 'SELECT * FROM filtered ORDER BY ' + history_order + ' LIMIT :limit OFFSET :offset',
                                        {**params, 'limit': 25, 'offset': (history_page-1)*25}):
                    item = display_row(row)
                    item['timestamp'] = item['recorded_at'] or 'Not recorded'
                    item['updated_by'] = item['latest_editor']
                    attendance.append(item)
            except ValueError as exc:
                history_error = str(exc)

        programs = []
        if current_user_has_permission("edit_student_metadata"):
            programs = conn.execute(
                """
                SELECT * FROM academic_programs
                WHERE is_active = 1 OR id = ?
                ORDER BY is_active DESC, name ASC
                """,
                (student["program_id"],),
            ).fetchall()

    return render_template(
        "students/profile.html",
        student=student,
        enrolled_classes=enrolled_classes,
        attendance=attendance,
        history_filters=history_filters, history_error=history_error,
        history_total=history_total, history_page=history_page,
        history_pages=max(1, (history_total+24)//25),
        history_query={**{'history_' + key: value for key, value in history_filters.items()},
                       **({'directory': urlencode(directory_filters)} if directory_filters else {})},
        programs=programs,
        can_view_enrollments=can_view_enrollments,
        can_view_attendance=can_view_attendance,
        directory_return_url=url_for("students.student_directory", **directory_filters),
        profile_reload_url=url_for("students.student_profile", student_id=student_id,
                                   **({"directory": urlencode(directory_filters)} if directory_filters else {})),
    ), 400 if history_error else 200


@students_bp.route("/api/students/<int:student_id>", methods=["PUT"])
@login_required
@permission_required("edit_student_metadata")
def api_update_student(student_id):
    """Update student metadata."""
    data, payload_error = _student_metadata_payload()
    if payload_error:
        return jsonify({"error": payload_error}), 400

    new_student_id = _canonical_student_id(data.get("student_id", ""))
    name = data.get("name", "").strip()
    program_id = data.get("program_id", "")
    year_level = data.get("year_level", "").strip()

    if not new_student_id or not name or not program_id or not year_level:
        return jsonify({"error": "All fields are required."}), 400

    if year_level not in VALID_YEAR_LEVELS:
        return jsonify({"error": "Invalid Year Level."}), 400

    try:
        program_id = int(program_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid Program ID."}), 400

    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _, denied = _student_write_actor(conn, "edit_student_metadata")
            if denied is not None:
                return denied
            existing = conn.execute(
                "SELECT id, program_id, permanently_removed_at FROM students WHERE id = ?",
                (student_id,),
            ).fetchone()
            if not existing:
                return jsonify({"error": "Student not found."}), 404
            if existing["permanently_removed_at"] is not None:
                return jsonify({"error": "Permanently removed students cannot be edited."}), 410
            # Check program existence
            prog = conn.execute(
                "SELECT id, name, is_active FROM academic_programs WHERE id = ?", (program_id,)
            ).fetchone()
            if not prog:
                return jsonify({"error": "Invalid Program ID."}), 400
            if not prog["is_active"] and program_id != existing["program_id"]:
                return jsonify({
                    "error": "Inactive programs may be retained by currently assigned students, but cannot receive new assignments."
                }), 409

            collision = _student_with_canonical_id(conn, new_student_id, exclude_pk=student_id)
            if collision:
                return jsonify({"error": "Student ID already exists."}), 409
            if _pending_student_registration_exists(conn, new_student_id):
                return jsonify({"error": "Student ID conflicts with a pending registration request."}), 409
            
            # Update student
            conn.execute(
                """
                UPDATE students
                SET student_id = ?, name = ?, program_id = ?, year_level = ?
                WHERE id = ?
                """,
                (new_student_id, name, program_id, year_level, student_id)
            )
            log_system_event(
                conn,
                "student_updated",
                actor_user_id=session.get("user_id"),
                details={"student_pk": student_id, "new_student_id": new_student_id}
            )
        return jsonify({"message": "Student updated successfully.", "student": {
            "student_id": new_student_id, "name": name, "program_id": program_id,
            "program_name": prog["name"], "year_level": year_level,
        }}), 200
    except sqlite3.IntegrityError:
        return jsonify({"error": "Student ID already exists."}), 409
    except Exception as e:
        logger.exception("Error updating student.")
        return jsonify({"error": "An internal error occurred."}), 500


@students_bp.route("/api/students/<int:student_id>/status", methods=["PUT"])
@login_required
@permission_required("manage_student_status")
def api_update_student_status(student_id):
    """Deactivate or Reactivate a student."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("is_active"), bool):
        return jsonify({"error": "is_active must be true or false."}), 400
    is_active = data["is_active"]

    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _, denied = _student_write_actor(conn, "manage_student_status")
            if denied is not None:
                return denied
            existing = conn.execute(
                "SELECT id, permanently_removed_at FROM students WHERE id = ?",
                (student_id,),
            ).fetchone()
            if not existing:
                return jsonify({"error": "Student not found."}), 404
            if existing["permanently_removed_at"] is not None:
                return jsonify({"error": "A permanently removed student cannot be reactivated."}), 410
            conn.execute("UPDATE students SET is_active = ? WHERE id = ?", (int(is_active), student_id))
            
            # If deactivated, purge embeddings for privacy compliance
            if not is_active:
                conn.execute("DELETE FROM embeddings WHERE student_id = ?", (student_id,))
                
            log_system_event(
                conn,
                "student_status_changed",
                actor_user_id=session.get("user_id"),
                details={"student_pk": student_id, "is_active": is_active, "purged_embeddings": not is_active}
            )
        action = "reactivated" if is_active else "deactivated (and biometrics purged)"
        return jsonify({"message": f"Student successfully {action}."}), 200
    except Exception as e:
        logger.exception("Error changing student status.")
        return jsonify({"error": "An internal error occurred."}), 500


@students_bp.route("/api/students/<int:student_id>/biometrics", methods=["POST"])
@login_required
@permission_required("manage_student_biometrics")
def api_student_biometrics_reenroll(student_id):
    """Re-enroll student biometrics."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Send a valid face enrollment request with consent and three images."}), 400
    if data.get("consent_granted") is not True:
        return jsonify({"error": "Biometric consent must be explicitly reconfirmed."}), 400

    with managed_transaction(commit=False) as conn:
        existing = conn.execute(
            """
            SELECT id, is_active, consent_granted, permanently_removed_at
            FROM students WHERE id = ?
            """,
            (student_id,),
        ).fetchone()
    if not existing:
        return jsonify({"error": "Student not found."}), 404
    if existing["permanently_removed_at"] is not None:
        return jsonify({"error": "Permanently removed students cannot be re-enrolled."}), 410
    if not existing["is_active"]:
        return jsonify({"error": "Inactive students cannot accept biometric enrollment."}), 409

    image_input, image_error = _three_pose_image_input(data)
    if image_error:
        return jsonify({"error": image_error}), 400

    try:
        face_blobs = _extract_three_face_blobs(image_input)

        from services.biometric_security import is_encrypted, encrypt_face_vector
        encrypted_face_blobs = [
            b if is_encrypted(b) else encrypt_face_vector(b)
            for b in face_blobs
        ]

        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _, denied = _student_write_actor(conn, "manage_student_biometrics")
            if denied is not None:
                return denied
            existing = conn.execute(
                "SELECT id, is_active, permanently_removed_at FROM students WHERE id = ?",
                (student_id,),
            ).fetchone()
            if not existing:
                return jsonify({"error": "Student not found."}), 404
            if existing["permanently_removed_at"] is not None:
                return jsonify({"error": "Permanently removed students cannot be re-enrolled."}), 410
            if not existing["is_active"]:
                return jsonify({"error": "Inactive students cannot accept biometric enrollment."}), 409
            # The strict boolean request is the durable consent reconfirmation;
            # persist it before the database guard evaluates new vectors.
            conn.execute("UPDATE students SET consent_granted = 1 WHERE id = ?", (student_id,))
            # Delete old embeddings
            conn.execute("DELETE FROM embeddings WHERE student_id = ?", (student_id,))
            # Insert new embedding(s)
            for blob in encrypted_face_blobs:
                conn.execute(
                    "INSERT INTO embeddings (student_id, face_vector) VALUES (?, ?)",
                    (student_id, blob)
                )
            log_system_event(
                conn,
                "student_biometrics_reenrolled",
                actor_user_id=session.get("user_id"),
                details={"student_pk": student_id, "vector_count": len(encrypted_face_blobs)}
            )
        return jsonify({"message": "Student biometrics updated successfully."}), 200
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        logger.exception("Error during student biometric re-enrollment.")
        return jsonify({"error": "An internal server error occurred."}), 500


@students_bp.route("/api/students/<int:student_id>", methods=["DELETE"])
@login_required
@permission_required("permanently_remove_students")
def api_permanently_remove_student(student_id):
    """Irreversibly remove a student from active use without deleting history."""
    actor_user_id = session.get("user_id")
    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            _, denied = _student_write_actor(conn, "permanently_remove_students")
            if denied is not None:
                return denied
            student = conn.execute(
                "SELECT id, student_id, permanently_removed_at FROM students WHERE id = ?",
                (student_id,),
            ).fetchone()
            if not student:
                return jsonify({"error": "Student not found."}), 404
            if student["permanently_removed_at"] is not None:
                return jsonify({"error": "This student has already been permanently removed."}), 409

            attendance_count = conn.execute(
                "SELECT COUNT(*) FROM attendance_records WHERE student_id = ?",
                (student_id,),
            ).fetchone()[0]
            preserved_roster_reference_count = conn.execute(
                "SELECT COUNT(*) FROM class_enrollments WHERE student_id = ?",
                (student_id,),
            ).fetchone()[0]
            embedding_count = conn.execute(
                "SELECT COUNT(*) FROM embeddings WHERE student_id = ?",
                (student_id,),
            ).fetchone()[0]

            conn.execute("DELETE FROM embeddings WHERE student_id = ?", (student_id,))
            conn.execute(
                """
                UPDATE students
                SET is_active = 0,
                    consent_granted = 0,
                    permanently_removed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (student_id,),
            )
            log_system_event(
                conn,
                "student_permanently_removed",
                actor_user_id=actor_user_id,
                details={
                    "student_pk": student_id,
                    "student_id": student["student_id"],
                    "purged_embedding_count": embedding_count,
                    "preserved_roster_reference_count": preserved_roster_reference_count,
                    "preserved_attendance_count": attendance_count,
                },
            )

        return jsonify({
            "message": "Student permanently removed from active use. Historical attendance was preserved."
        }), 200
    except Exception:
        logger.exception("Error permanently removing student %s.", student_id)
        return jsonify({"error": "An internal server error occurred."}), 500
