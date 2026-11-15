"""
CSD FARS — Facial Attendance Recognition System
----------------------------
Main entry point for the web server. Handles routing, session-based
authentication, RBAC enforcement, and error handling.
"""

import datetime
import functools
import os
import psutil
import csv
import io

from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
    Response,
    send_file,
    abort,
)
from werkzeug.security import check_password_hash, generate_password_hash
from flask_wtf.csrf import CSRFProtect, CSRFError

import config
from core import get_logger
from core.routing import RecordIdConverter
from core.ai_pipeline import extract_embedding
from core.streaming import generate_mjpeg_stream
from database.db_manager import get_db_connection
from routes.blueprints import (
    BLUEPRINTS,
    admin_bp,
    auth_bp,
    classes_bp,
    dashboard_bp,
    diagnostics_bp,
    reports_bp,
    settings_bp,
    students_bp,
)
from core.utils import *
from security.rbac import (
    ROLES,
    current_user_has_permission,
    permission_required,
)
from services.archive import create_purge_archive
from services.stream_capacity import install_stream_capacity
from services.network_monitor import install_network_monitor
from services.audit import log_system_event
from services.attendance_sessions import (
    EDITABLE_ATTENDANCE_STATUSES,
    close_attendance_session,
    create_attendance_session,
    get_latest_attendance_session,
    get_open_attendance_session,
    session_window_for_class,
    upsert_attendance_record,
)
from services.camera_security import (
    CameraSourceEncryptionError,
    decrypt_camera_source,
    encrypt_camera_source,
    mask_camera_source,
)
from workers.session_manager import session_manager
import sqlite3

active_sessions = session_manager.active_sessions

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

# ---------------------------------------------------------------------------
# Application Factory
# ---------------------------------------------------------------------------

logger = get_logger("app")

# Load .env for SECRET_KEY.
load_dotenv(config.ENV_PATH)

app = Flask(__name__)
app.url_map.converters['int'] = RecordIdConverter
app.secret_key = os.environ.get("SECRET_KEY")
csrf = CSRFProtect(app)

install_stream_capacity(app, persisted_limits=True)
install_network_monitor(app)

if not app.secret_key:
    logger.error(
        "SECRET_KEY not found. Open START_HERE.cmd from the same installation folder. "
        "If setup cannot continue, leave the folder unchanged and contact the system owner. "
        "Do not replace or regenerate existing installation keys."
    )
    raise RuntimeError(
        "SECRET_KEY is not set. Open START_HERE.cmd from the same installation folder. "
        "If setup cannot continue, leave the folder unchanged and contact the system owner. "
        "Do not replace or regenerate existing installation keys."
    )


@app.context_processor
def inject_rbac_helpers():
    return {
        "has_permission": current_user_has_permission,
        "roles": ROLES,
        "format_duration": format_duration,
    }


app.jinja_env.filters["format_duration"] = format_duration

def school_local_time(value):
    if not value:
        return "Not recorded"
    try:
        instant = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=datetime.timezone.utc)
        local = instant.astimezone(datetime.timezone(datetime.timedelta(minutes=config.INSTITUTION_UTC_OFFSET_MINUTES)))
        return local.strftime('%d %b %Y, %I:%M %p')
    except (ValueError, TypeError):
        return "Time unavailable"

app.jinja_env.filters["school_local_time"] = school_local_time



# ---------------------------------------------------------------------------
# Blueprint Registration And Legacy Endpoint Aliases
# ---------------------------------------------------------------------------

import routes.auth
import routes.dashboard
import routes.diagnostics
import routes.reports
import routes.admin
import routes.students
import routes.classes
import routes.settings
import routes.setup
import routes.network
import routes.entrance
for blueprint in BLUEPRINTS:
    app.register_blueprint(blueprint)


LEGACY_ENDPOINTS = {
    "index": "auth.index",
    "about": "auth.about",
    "login": "auth.login",
    "logout": "auth.logout",
    "password_reset": "auth.password_reset",
    "dashboard": "dashboard_routes.dashboard",
    "diagnostics_index": "diagnostics.diagnostics_index",
    "api_diagnostics_stats": "diagnostics.api_diagnostics_stats",
    "api_reports_export": "reports.api_reports_export",
    "reports_index": "reports.reports_index",
    "api_admin_purge": "admin.api_admin_purge",
    "admin_users": "admin.admin_users",
    "api_edit_user": "admin.api_edit_user",
    "api_reset_password": "admin.api_reset_password",
    "api_toggle_status": "admin.api_toggle_status",
    "student_registration": "students.student_registration",
    "student_approvals": "students.student_approvals",
    "student_directory": "students.student_directory",
    "api_students_directory": "students.api_students_directory",
    "api_student_enroll": "students.api_student_enroll",
    "api_approve_student_registration": "students.api_approve_student_registration",
    "api_reject_student_registration": "students.api_reject_student_registration",
    "api_students_search": "students.api_students_search",
    "manage_classes": "classes.manage_classes",
    "edit_class": "classes.edit_class",
    "class_enrollments": "classes.class_enrollments",
    "api_class_enrollments": "classes.api_class_enrollments",
    "api_class_enrollments_batch": "classes.api_class_enrollments_batch",
    "class_session": "classes.class_session",
    "api_start_session": "classes.api_start_session",
    "api_stop_session": "classes.api_stop_session",
    "video_feed": "classes.video_feed",
    "api_manual_override": "classes.api_manual_override",
    "api_attendance_status": "classes.api_attendance_status",
    "class_session_history": "classes.class_session_history",
    "class_session_report": "classes.class_session_report",
    "class_session_report_export": "classes.class_session_report_export",
    "api_update_session_attendance": "classes.api_update_session_attendance",
    "manage_cameras": "settings.manage_cameras",
    "api_cameras": "settings.api_cameras",
    "api_cameras_health": "settings.api_cameras_health",
    "api_add_camera": "settings.api_add_camera",
    "api_update_camera": "settings.api_update_camera",
    "api_set_default_camera": "settings.api_set_default_camera",
    "api_toggle_camera": "settings.api_toggle_camera",
    "api_test_camera": "settings.api_test_camera",
    "manage_programs": "settings.manage_programs",
    "api_add_program": "settings.api_add_program",
    "api_toggle_program": "settings.api_toggle_program",
}


def _register_legacy_endpoint_aliases():
    rules_by_endpoint = {}
    for rule in list(app.url_map.iter_rules()):
        rules_by_endpoint.setdefault(rule.endpoint, []).append(rule)

    for legacy_endpoint, blueprint_endpoint in LEGACY_ENDPOINTS.items():
        view_func = app.view_functions[blueprint_endpoint]
        for rule in rules_by_endpoint.get(blueprint_endpoint, []):
            methods = sorted(rule.methods - {"HEAD", "OPTIONS"})
            app.add_url_rule(
                rule.rule,
                endpoint=legacy_endpoint,
                view_func=view_func,
                methods=methods,
            )


_register_legacy_endpoint_aliases()


# ---------------------------------------------------------------------------
# Error Handlers
# ---------------------------------------------------------------------------

def _request_wants_json_error():
    """Identify API requests without turning ordinary browser pages into JSON."""
    if request.path.startswith("/api/") or request.is_json:
        return True
    return (
        request.accept_mimetypes.accept_json
        and not request.accept_mimetypes.accept_html
    )


def _json_error(message, status):
    """Return the stable public envelope used by generic API failures."""
    response = jsonify({"error": message, "status": status})
    response.status_code = status
    return response


@app.errorhandler(400)
def bad_request_error(e):
    """Keep validation details private while preserving browser HTML errors."""
    if _request_wants_json_error():
        return _json_error("Bad Request", 400)
    return e

@app.errorhandler(403)
def forbidden_error(e):
    """Redirect unauthorized users to the dashboard, or return JSON for APIs."""
    if _request_wants_json_error():
        return _json_error("Forbidden", 403)
    flash("You do not have permission to access this page.", "error")
    return redirect(url_for("dashboard"))


@app.errorhandler(404)
def page_not_found(e):
    """Render a user-friendly 404 page."""
    if _request_wants_json_error():
        return _json_error("Not Found", 404)
    return render_template("errors/404.html"), 404


@app.errorhandler(405)
def method_not_allowed_error(e):
    """Return a stable API error while retaining Werkzeug's HTML page."""
    if _request_wants_json_error():
        response = _json_error("Method Not Allowed", 405)
        allow_header = e.get_response().headers.get("Allow")
        if allow_header:
            response.headers["Allow"] = allow_header
        return response
    return e


@app.errorhandler(415)
def unsupported_media_type_error(e):
    """Return a stable API error while retaining Werkzeug's HTML page."""
    if _request_wants_json_error():
        return _json_error("Unsupported Media Type", 415)
    return e


@app.errorhandler(500)
def internal_server_error(e):
    """Render a user-friendly 500 page."""
    logger.exception("Internal server error: %s", e)
    if _request_wants_json_error():
        return _json_error("Internal Server Error", 500)
    return render_template("errors/500.html"), 500


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    """Handle CSRF errors gracefully."""
    logger.warning("CSRF validation failed: %s", e.description)
    if _request_wants_json_error():
        return _json_error("CSRF token missing or expired.", 400)
    # Recover only through a server-defined GET route, never a client Referer
    # or replay of the rejected write. Do not echo submitted fields/credentials.
    recovery_url = url_for("login")
    recovery_label = "Return to sign in"
    if request.url_rule and "GET" in request.url_rule.methods:
        recovery_url = url_for(request.url_rule.endpoint, **(request.view_args or {}))
        recovery_label = "Reload form"
    return render_template(
        "errors/csrf.html",
        expired=e.description == "The CSRF token has expired.",
        recovery_url=recovery_url,
        recovery_label=recovery_label,
    ), 400


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from services.session_lifecycle import start_coordinator
    start_coordinator()
    logger.info(
        "Starting CSD FARS on %s:%s",
        config.FLASK_HOST,
        config.FLASK_PORT,
    )
    app.run(
        host=config.FLASK_HOST,
        port=config.FLASK_PORT,
        debug=config.FLASK_DEBUG,
        use_reloader=False,
    )
