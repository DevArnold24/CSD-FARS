from flask import abort, jsonify, request, session, render_template, flash, redirect, url_for
from werkzeug.security import generate_password_hash
from werkzeug.exceptions import HTTPException

from core import get_logger
from core.utils import login_required
from services.archive import create_purge_archive
from services.audit import log_system_event
from security.rbac import has_permission, permission_required
from security.session_auth import authenticated_session_user
from database.db_manager import get_db_connection, managed_transaction
from routes.blueprints import admin_bp
from security.passwords import validate_password_complexity
import secrets
import sqlite3
import config

logger = get_logger("admin")
def _authorize_admin_write(conn, permission="manage_users", *, html=False):
    """Recheck the signed credential and current role under the write lock."""
    actor = authenticated_session_user(conn, session)
    if not actor or actor["requires_password_change"]:
        session.clear()
        if html:
            flash("Your login session expired. Sign in again before creating an account.", "error")
            return redirect(url_for("auth.login"))
        return jsonify({"error":"Your login session expired.", "session_expired":True,
                        "login_url":url_for("auth.login")}), 401
    if not has_permission(actor["role"], permission):
        if html:
            abort(403)
        return jsonify({"error":"Forbidden"}), 403
    return None


@admin_bp.route("/api/admin/purge", methods=["POST"])
@login_required
@permission_required("run_purge")
def api_admin_purge():
    """API endpoint to securely wipe all attendance, logs, and biometrics."""
    if not config.ENABLE_MAINTENANCE_PURGE:
        abort(404)
    data = request.get_json()
    if not isinstance(data, dict) or data.get("confirm") != "PURGE":
        return jsonify({"error": "Invalid confirmation payload."}), 400
        
    try:
        with managed_transaction(commit=True) as conn:
            # Reserve writes before checking sessions and taking the archive.
            # Separate archive readers can still read the last committed state,
            # but no attendance or roster write can escape that snapshot and
            # then be deleted by the same purge.
            conn.execute("BEGIN IMMEDIATE")
            denied = _authorize_admin_write(conn, "run_purge")
            if denied is not None:
                return denied
            if (conn.execute("SELECT 1 FROM attendance_sessions WHERE status='Open' LIMIT 1").fetchone()
                    or conn.execute("SELECT 1 FROM classes WHERE is_active=1 LIMIT 1").fetchone()):
                return jsonify({"error": "Stop all running classes and close open attendance sessions before maintenance. No archive or purge was started."}), 409
            user_id = session.get("user_id")
            logger.warning(f"CRITICAL: User {user_id} has initiated a database purge.")

            archive_result = create_purge_archive(user_id)
            log_system_event(
                conn,
                "purge_archive_created",
                user_id,
                {
                    "archive_file": archive_result["archive_file"],
                    "row_counts": archive_result["manifest"].get("row_counts", {}),
                },
            )
        
            conn.execute("DELETE FROM embeddings")
            conn.execute("DELETE FROM attendance_change_audit")
            conn.execute("DELETE FROM audit_logs")
            conn.execute("DELETE FROM attendance_records")
            conn.execute("DELETE FROM attendance_sessions")
            conn.execute("DELETE FROM class_enrollments")
            conn.execute("DELETE FROM class_enrollment_periods")
            conn.execute(
                """
                UPDATE pending_student_registrations
                SET status = 'Rejected',
                    reviewed_by_user_id = ?,
                    reviewed_at = CURRENT_TIMESTAMP,
                    review_note = 'Rejected by system purge.',
                    face_vector = NULL
                WHERE status = 'Pending'
                """,
                (user_id,),
            )
            conn.execute("UPDATE pending_student_registrations SET face_vector = NULL WHERE face_vector IS NOT NULL")
            log_system_event(
                conn,
                "purge_completed",
                user_id,
                {"archive_file": archive_result["archive_file"]},
            )
        
            logger.warning(f"CRITICAL: Database purge completed by User {user_id}.")
            return jsonify({
                "message": "System data archived and purged successfully.",
                "archive_file": archive_result["archive_file"],
            }), 200
    except Exception as e:
        logger.exception("Failed to purge database.")
        return jsonify({"error": "Failed to purge database."}), 500
@admin_bp.route("/admin/users", methods=["GET", "POST"])
@login_required
@permission_required("manage_users")
def admin_users():
    """Render user management panel and handle creation of new user accounts.

    Routes:
        GET /admin/users: Fetches and displays a list of all system users.
        POST /admin/users: Validates and provisions a new user with a specified role.

    HTTP Methods:
        GET, POST

    Roles Permitted:
        Dean

    Form Fields (POST):
        username (str): Uniquely identifying username for the new account.
        password (str): Plaintext password to be hashed.
        role (str): System access role ('Dean', 'Teacher', 'IT_Support', 'Office_Admin').

    Database Queries:
        GET:
            - SELECT id, username, role FROM users ORDER BY id DESC
        POST:
            - INSERT INTO users (username, password_hash, role, requires_password_change) VALUES (?, ?, ?, 1)

    Security Boundaries:
        - Strict RBAC: Only a logged-in Dean can access this endpoint.
        - Default Password Reset Force: Newly created accounts are initialized with
          `requires_password_change = 1` to force password reset on initial login.
    """
    create_values = {"staff_id": "", "username": "", "role": "Teacher"}
    create_error = None
    create_error_field = None
    response_status = 200
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        staff_id = request.form.get("staff_id", "").strip()
        if not staff_id and username:
            staff_id = f"STAFF-{username.upper()}"
        password = request.form.get("password", "")
        role = request.form.get("role", "").strip()
        # Keep only non-secret fields in the rendered draft, never in cookies.
        create_values = {"staff_id": staff_id, "username": username, "role": role}
        response_status = 400
        if not staff_id or not username or not password or not role:
            create_error = "All fields are required."
        elif len(staff_id) > 64 or len(username) > 64:
            create_error = "Staff ID and Username must each contain no more than 64 characters."
            create_error_field = "staff_id" if len(staff_id) > 64 else "username"
        elif role not in ["Dean", "Teacher", "IT_Support", "Office_Admin"]:
            create_error = "Invalid role selected."
            create_error_field = "role"
        else:
            valid_password, password_error = validate_password_complexity(password)
            if not valid_password:
                create_error = f"Initial password rejected: {password_error}"
                create_error_field = "password"
            else:
                try:
                    password_hash = generate_password_hash(password)
                    # Let exceptions leave the transaction before handling them:
                    # insertion and auditing must not partially commit on error.
                    with managed_transaction(commit=True) as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        denied = _authorize_admin_write(conn, html=True)
                        if denied is not None:
                            return denied
                        cursor = conn.execute(
                            "INSERT INTO users (staff_id, username, password_hash, role, requires_password_change) VALUES (?, ?, ?, ?, 1)",
                            (staff_id, username, password_hash, role),
                        )
                        log_system_event(
                            conn,
                            "user_created",
                            actor_user_id=session.get("user_id"),
                            details={
                                "created_user_id": cursor.lastrowid,
                                "staff_id": staff_id,
                                "username": username,
                                "role": role,
                                "requires_password_change": True,
                            },
                        )
                except sqlite3.IntegrityError as error:
                    if "UNIQUE constraint failed" in str(error):
                        create_error_field = "staff_id" if "staff_id" in str(error) else "username"
                        create_error = "Staff ID already exists." if create_error_field == "staff_id" else "Username already exists."
                        response_status = 409
                    else:
                        logger.exception("Failed to create user.")
                        create_error = "Unable to create the account. Please try again."
                        response_status = 500
                except HTTPException:
                    raise
                except Exception:
                    logger.exception("Failed to create user.")
                    create_error = "Unable to create the account. Please try again."
                    response_status = 500
                else:
                    flash(f"User '{username}' created successfully.", "success")
                    return redirect(url_for("admin_users"))
    with managed_transaction(commit=False) as conn:
        users = conn.execute("SELECT id, staff_id, username, role, is_active FROM users ORDER BY id DESC").fetchall()
    return render_template(
        "admin/users.html",
        users=users,
        maintenance_purge_enabled=config.ENABLE_MAINTENANCE_PURGE,
        create_values=create_values,
        create_error=create_error,
        create_error_field=create_error_field,
    ), response_status


@admin_bp.route("/api/admin/users/<int:user_id>/edit", methods=["POST"])
@login_required
@permission_required("manage_users")
def api_edit_user(user_id):
    """API endpoint to update user details (Staff ID, Username, Role)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or any(
        not isinstance(data.get(field, ""), str)
        for field in ("staff_id", "username", "role")
    ):
        return jsonify({"status": "error", "message": "Provide Staff ID, Username and Role as text fields."}), 400
    staff_id = data.get("staff_id", "").strip()
    username = data.get("username", "").strip()
    role = data.get("role", "").strip()

    if not staff_id or not username or not role:
        return jsonify({"status": "error", "message": "All fields are required."}), 400
    if len(staff_id) > 64 or len(username) > 64:
        return jsonify({"status":"error","message":"Staff ID and Username must each contain no more than 64 characters."}), 400
    if role not in ["Dean", "Teacher", "IT_Support", "Office_Admin"]:
        return jsonify({"status": "error", "message": "Invalid role selected."}), 400

    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            denied = _authorize_admin_write(conn)
            if denied is not None:
                return denied
            existing = conn.execute(
                "SELECT id, staff_id, username, role, is_active FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if not existing:
                return jsonify({"status": "error", "message": "User not found."}), 404
            if existing["role"] == "Teacher" and role != "Teacher":
                class_count = conn.execute(
                    "SELECT COUNT(*) FROM classes WHERE teacher_id = ?", (user_id,)
                ).fetchone()[0]
                if class_count:
                    return jsonify({
                        "status": "error",
                        "message": (
                            f"Reassign this Teacher's {class_count} class(es) before changing their role. "
                            "The Dean can reassign each class from Edit Class."
                        ),
                    }), 409
            if (
                existing["role"] == "Dean"
                and existing["is_active"]
                and role != "Dean"
            ):
                other_active_deans = conn.execute(
                    """
                    SELECT COUNT(*) FROM users
                    WHERE id != ? AND role = 'Dean' AND is_active = 1
                    """,
                    (user_id,),
                ).fetchone()[0]
                if not other_active_deans:
                    return jsonify({
                        "status": "error",
                        "message": "Assign another active Dean before changing the last active Dean's role.",
                    }), 409
            conn.execute(
                "UPDATE users SET staff_id = ?, username = ?, role = ? WHERE id = ?",
                (staff_id, username, role, user_id)
            )
            if existing["role"] != role:
                conn.execute("""UPDATE attendance_display_grants
                    SET revoked_at=CURRENT_TIMESTAMP, revoked_by_user_id=?, revoke_reason='role_changed'
                    WHERE enabled_by_user_id=? AND revoked_at IS NULL""", (session.get("user_id"), user_id))
            log_system_event(
                conn,
                "user_profile_authorization_changed",
                actor_user_id=session.get("user_id"),
                details={
                    "target_user_id": user_id,
                    "previous": {
                        "staff_id": existing["staff_id"],
                        "username": existing["username"],
                        "role": existing["role"],
                    },
                    "new": {"staff_id": staff_id, "username": username, "role": role},
                },
            )
            logger.info("User ID %d updated by admin (Staff ID=%s, Username=%s, Role=%s)", user_id, staff_id, username, role)
            return jsonify({"status": "success", "message": "User updated successfully."})
    except Exception as e:
        logger.exception("Failed to update user ID %d", user_id)
        if "UNIQUE constraint failed" in str(e):
            if "staff_id" in str(e):
                return jsonify({"status": "error", "message": "Staff ID already exists."}), 400
            else:
                return jsonify({"status": "error", "message": "Username already exists."}), 400
        return jsonify({"status": "error", "message": "An error occurred while updating the user."}), 500
@admin_bp.route("/api/admin/users/<int:user_id>/reset_password", methods=["POST"])
@login_required
@permission_required("manage_users")
def api_reset_password(user_id):
    """API endpoint to force reset a user's password to a random temporary value."""
    try:
        with managed_transaction(commit=True) as conn:
            # Serialize the credential mutation and its actor authorization with
            # concurrent resets/deactivation, including a reset of this Dean.
            conn.execute("BEGIN IMMEDIATE")
            actor = authenticated_session_user(conn, session)
            if not actor or actor["requires_password_change"]:
                session.clear()
                return jsonify({"error": "Your login session expired.",
                                "session_expired": True,
                                "login_url": url_for("auth.login")}), 401
            if not has_permission(actor["role"], "manage_users"):
                return jsonify({"error": "Forbidden"}), 403
            target = conn.execute(
                "SELECT id, username FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not target:
                return jsonify({"status": "error", "message": "User not found."}), 404
            temp_password = secrets.token_urlsafe(9)
            password_hash = generate_password_hash(temp_password)
            conn.execute(
                "UPDATE users SET password_hash = ?, requires_password_change = 1 WHERE id = ?",
                (password_hash, user_id)
            )
            # Issued display tokens are separate from browser cookies. Retire
            # those recovery-adjacent grants in the same reset transaction too.
            conn.execute(
                """UPDATE attendance_display_grants
                   SET revoked_at = CURRENT_TIMESTAMP, revoked_by_user_id = ?,
                       revoke_reason = 'credential_reset'
                   WHERE enabled_by_user_id = ? AND revoked_at IS NULL""",
                (actor["id"], user_id),
            )
            log_system_event(
                conn,
                "user_password_reset",
                actor_user_id=session.get("user_id"),
                details={
                    "target_user_id": user_id,
                    "target_username": target["username"],
                    "requires_password_change": True,
                },
            )
            logger.info("Password for user ID %d reset to a temporary value by admin", user_id)
            return jsonify({
                "status": "success",
                "message": "Password has been reset. The user must change it on next login.",
                "temp_password": temp_password
            })
    except Exception as e:
        logger.exception("Failed to reset password for user ID %d", user_id)
        return jsonify({"status": "error", "message": "An error occurred while resetting the password."}), 500
@admin_bp.route("/api/admin/users/<int:user_id>/toggle_status", methods=["POST"])
@login_required
@permission_required("manage_users")
def api_toggle_status(user_id):
    """API endpoint to toggle user active/inactive status. Prevents self-deactivation."""
    desired_status = None
    if request.is_json or request.get_data():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or type(data.get("is_active")) is not bool:
            return jsonify({"status":"error","message":"Choose whether the account should be active using true or false."}), 400
        desired_status = int(data["is_active"])
    if user_id == session.get("user_id") and desired_status != 1:
        return jsonify({"status": "error", "message": "You cannot deactivate your own account."}), 400

    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            denied = _authorize_admin_write(conn)
            if denied is not None:
                return denied
            user = conn.execute(
                "SELECT id, username, role, is_active FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not user:
                return jsonify({"status": "error", "message": "User not found."}), 404
        
            new_status = desired_status if desired_status is not None else (0 if user["is_active"] else 1)
            if new_status == user["is_active"]:
                return jsonify({"status":"success","message":"Account already has the requested status.","is_active":new_status}), 200
            if not new_status and user["role"] == "Teacher":
                active_class_count = conn.execute(
                    """
                    SELECT COUNT(*) FROM classes
                    WHERE teacher_id = ? AND is_archived = 0
                    """,
                    (user_id,),
                ).fetchone()[0]
                if active_class_count:
                    return jsonify({
                        "status": "error",
                        "message": (
                            f"Reassign or archive this Teacher's {active_class_count} active class(es) "
                            "before deactivation."
                        ),
                    }), 409
            if not new_status and user["role"] == "Dean":
                other_active_deans = conn.execute(
                    """
                    SELECT COUNT(*) FROM users
                    WHERE id != ? AND role = 'Dean' AND is_active = 1
                    """,
                    (user_id,),
                ).fetchone()[0]
                if not other_active_deans:
                    return jsonify({
                        "status": "error",
                        "message": "The last active Dean cannot be deactivated.",
                    }), 409
            conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_status, user_id))
            if not new_status:
                conn.execute("""UPDATE attendance_display_grants
                    SET revoked_at=CURRENT_TIMESTAMP, revoked_by_user_id=?, revoke_reason='account_deactivated'
                    WHERE enabled_by_user_id=? AND revoked_at IS NULL""", (session.get("user_id"), user_id))
            log_system_event(
                conn,
                "user_status_changed",
                actor_user_id=session.get("user_id"),
                details={
                    "target_user_id": user_id,
                    "target_username": user["username"],
                    "role": user["role"],
                    "previous_is_active": bool(user["is_active"]),
                    "new_is_active": bool(new_status),
                },
            )
            logger.info("User ID %d status toggled to %d by admin", user_id, new_status)
            return jsonify({
                "status": "success",
                "message": f"User {'activated' if new_status else 'deactivated'} successfully.",
                "is_active": new_status
            })
    except Exception as e:
        logger.exception("Failed to toggle status for user ID %d", user_id)
        return jsonify({"status": "error", "message": "An error occurred while changing user status."}), 500
