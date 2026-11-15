import json
import base64
import os
import uuid
import sqlite3
import socket
import time
import ipaddress
import xml.etree.ElementTree as ET
import re
import math
from urllib.parse import quote, urlsplit
from datetime import datetime, timezone

from flask import jsonify, request, session, render_template, flash, redirect, url_for, abort, Response, send_file, current_app as app

from core import get_logger
from core.routing import parse_record_id
from core.utils import *
from security.rbac import has_permission, permission_required
from security.session_auth import credential_matches, authenticated_session_user
from database.db_manager import get_db_connection, managed_transaction
from services.audit import log_system_event
from services.camera_management import (CameraManagementError, require_idle,
    require_available, deletion_blocker_details, removal_blocker, room_available, record_change)
from routes.blueprints import settings_bp, BLUEPRINTS
from services.camera_security import CameraSourceEncryptionError, encrypt_camera_source
from services.camera_identity import CameraIdentityUnavailable, require_unique_local_source
from services.camera_test_diagnostics import camera_test_diagnostic, classify_camera_test
from cryptography.fernet import InvalidToken
from services.camera_discovery import (
    DiscoveryBusy,
    DiscoveryUnavailable,
    InvalidDiscoveryInterface,
    discover_cameras,
    list_discovery_interfaces,
    select_discovery_interface,
)
from core.latest_frame_capture import LatestFrameCapture
from core.capture_config import build_capture_config, serialize_capture_config
from workers.session_manager import CameraBusyError, session_manager

logger = get_logger("settings")

STREAM_AUTHORIZATION_CHECK_INTERVAL_SECONDS = 0.5
ONVIF_DISCOVERY_TIMEOUT_SECONDS = 2.0
ONVIF_DISCOVERY_MAX_UNIQUE_REPLIES = 64
ONVIF_DISCOVERY_ADDRESS = ("239.255.255.250", 3702)
REGISTRATION_CAMERA_PERMISSIONS = (
    "use_registration_camera",
)


def _camera_stream_access_is_current(
    user_id,
    required_permissions=("manage_cameras",),
    camera_id=None,
    *,
    credential_token=None,
    signing_key=None,
):
    """Fail closed when a camera stream viewer loses current DB permission."""
    try:
        with managed_transaction(commit=False) as conn:
            authorization = conn.execute(
                """
                SELECT role, password_hash, is_active, requires_password_change
                FROM users
                WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
            camera_is_active = camera_id is None or conn.execute(
                """
                SELECT id
                FROM camera_devices
                WHERE id = ?
                  AND is_active = 1
                  AND lifecycle_status = 'Ready'
                """,
                (camera_id,),
            ).fetchone() is not None
    except Exception:
        logger.exception("Failed to revalidate camera stream authorization.")
        return False
    return bool(
        authorization
        and credential_matches(user_id, authorization["password_hash"], credential_token, secret_key=signing_key)
        and authorization["is_active"]
        and not authorization["requires_password_change"]
        and any(
            has_permission(authorization["role"], permission)
            for permission in required_permissions
        )
        and camera_is_active
    )

@settings_bp.route("/settings/cameras")
@login_required
@permission_required("manage_cameras")
def manage_cameras():
    """Render camera inventory and setup UI."""
    with managed_transaction(commit=False) as conn:
        cameras = _fetch_cameras_for_management(conn)
        rooms = _fetch_active_rooms(conn)
        room_coverage = _fetch_room_coverage(conn)
    return render_template(
        "settings/cameras.html",
        cameras=[_serialize_camera(camera) for camera in cameras],
        rooms=[_serialize_room(room) for room in rooms],
        room_coverage=room_coverage,
        room_types=ROOM_TYPES,
        camera_lifecycle_statuses=CAMERA_LIFECYCLE_STATUSES,
    )


@settings_bp.route('/api/cameras/capacity-limits', methods=['GET', 'POST'])
@login_required
@permission_required('manage_cameras')
def camera_capacity_limits():
    from services.camera_capacity import load_limits, save_limits, CapacityUnavailable
    try:
        with managed_transaction(commit=request.method == 'POST') as conn:
            if request.method == 'POST':
                limits = save_limits(conn, request.get_json(silent=True), session.get('user_id'))
            else:
                limits = load_limits(conn)
        return jsonify(limits=limits, provisional=True,
                       message='Configured limits control new connections; they do not establish measured hardware capacity. Changes affect new admissions only; existing owners remain reserved until cleanup is confirmed.')
    except CapacityUnavailable as exc:
        return jsonify(error=str(exc)), 400 if request.method == 'POST' else 503


@settings_bp.route('/settings/attendance')
@login_required
@permission_required('manage_attendance_settings')
def attendance_settings_page():
    import config
    return render_template('settings/attendance.html', school_utc_offset=config.INSTITUTION_UTC_OFFSET_MINUTES)


@settings_bp.route('/api/settings/attendance-opening', methods=['GET', 'POST'])
@login_required
@permission_required('manage_attendance_settings')
def attendance_opening_settings():
    from services import attendance_settings as opening
    try:
        with managed_transaction(commit=request.method == 'POST') as conn:
            if request.method == 'POST':
                conn.execute('BEGIN IMMEDIATE')
            actor = authenticated_session_user(conn, session)
            if not actor or actor['requires_password_change']:
                return jsonify(error='Your login session expired. Sign in again.', outcome='not_accepted'), 401
            if actor['role'] != 'Dean':
                return jsonify(error='Only the Dean may change attendance opening.', outcome='not_accepted'), 403
            if request.method == 'POST':
                saved = opening.save(conn, request.get_json(silent=True), actor['id'])
                operation = saved['operation_id']
            else:
                operation = request.args.get('operation_id')
        # Separate readback after commit. A lost response must not imply failure.
        with managed_transaction(commit=False) as conn:
            current = opening.load(conn)
            saved = opening.receipt(conn, operation) if operation else None
            history = [dict(row) for row in conn.execute("""SELECT h.previous_minutes,h.minutes,h.version,h.created_at,
                COALESCE(u.username,'Former account') AS actor_name
                FROM attendance_opening_history h LEFT JOIN users u ON u.id=h.actor
                ORDER BY h.id DESC LIMIT 20""")]
        response = jsonify(settings=current, receipt=saved, history=history,
                           outcome='confirmed' if saved else 'not_confirmed' if operation else 'current')
        response.headers['Cache-Control'] = 'no-store'
        return response
    except opening.SettingsConflict as exc:
        return jsonify(error=str(exc), outcome='not_accepted'), 409
    except opening.SettingsUnavailable:
        return jsonify(error='Attendance opening configuration is unavailable or invalid. Ask the server operator to check the saved configuration. No default was applied; existing sessions are unchanged.', outcome='unknown'), 503
    except ValueError as exc:
        return jsonify(error=str(exc), outcome='not_accepted'), 400
    except (sqlite3.Error, OSError):
        logger.exception('Attendance opening save/readback could not be confirmed')
        return jsonify(error='The saved setting could not be confirmed. Check the saved setting before retrying; existing sessions are unchanged.', outcome='unknown'), 503


def _normalized_name(value):
    return _display_name(value).casefold()


def _display_name(value):
    return " ".join(value.split()) if isinstance(value, str) else ""



def _location_data(allow_form=False):
    data = request.get_json(silent=True) if request.is_json else (request.form if allow_form else None)
    if not isinstance(data, dict):
        raise ValueError("Submit a complete location form.")
    return data


def _location_text(data, key, limit=120, default="", required=False, normalized=False):
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key.replace('_', ' ').capitalize()} must be text.")
    value = _display_name(value) if normalized else value.strip()
    if len(value) > limit or (required and not value):
        raise ValueError(f"{key.replace('_', ' ').capitalize()} must contain {'1' if required else '0'} to {limit} characters.")
    return value


def _location_integer(value, label, *, default=None, minimum=0, maximum=1000000):
    if value is None or value == "":
        return default
    if type(value) is int:
        parsed = value
    elif isinstance(value, str) and len(value.strip()) <= 20 and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
        parsed = int(value.strip())
    else:
        raise ValueError(f"{label} must be a whole number.")
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return parsed


def _location_id(data, key):
    value = parse_record_id(data.get(key))
    if value is None:
        raise ValueError(f"Select a valid {key.removesuffix('_id')}.")
    return value


def _location_desired_status():
    if not request.is_json and not request.get_data():
        return None  # Compatibility for existing empty-body callers.
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get('is_active')) is not bool:
        raise ValueError("Choose whether the location should be active using true or false.")
    return int(data['is_active'])


def _authorize_location_write(conn):
    """Recheck the account after acquiring the location write transaction."""
    actor = authenticated_session_user(conn, session)
    if not actor or actor['requires_password_change']:
        session.clear()
        return jsonify(error='Your login session expired.', session_expired=True,
                       login_url=url_for('auth.login')), 401
    if not has_permission(actor['role'], 'manage_locations'):
        return jsonify(error='Forbidden'), 403
    return None


def _location_has_open_attendance(conn, kind, identity):
    column = {'building':'f.building_id', 'floor':'r.floor_id', 'room':'r.id'}[kind]
    return conn.execute(f"""SELECT 1 FROM classes c
        JOIN rooms r ON r.id = c.room_id JOIN floors f ON f.id = r.floor_id
        WHERE {column} = ? AND (c.is_active = 1 OR EXISTS
            (SELECT 1 FROM attendance_sessions ats WHERE ats.class_id = c.id AND ats.status = 'Open'))
        LIMIT 1""", (identity,)).fetchone() is not None


def _location_has_owned_camera(conn, kind, identity):
    """Check affected active cameras while the caller holds the lifecycle guard."""
    column = {'building': 'f.building_id', 'floor': 'r.floor_id', 'room': 'r.id'}[kind]
    cameras = conn.execute(f"""SELECT cd.id FROM camera_devices cd
        JOIN rooms r ON r.id = cd.room_id JOIN floors f ON f.id = r.floor_id
        WHERE {column} = ? AND cd.is_active = 1""", (identity,)).fetchall()
    return any(session_manager.get_camera_owner(camera['id']) is not None for camera in cameras)


def _retire_location_displays(conn, kind, identity, actor_user_id):
    """Deactivation permanently retires access; reactivation cannot revive links."""
    from services.attendance_display import revoke_class_displays
    column = {'building': 'f.building_id', 'floor': 'r.floor_id', 'room': 'r.id'}[kind]
    classes = conn.execute(f'''SELECT c.id FROM classes c
        JOIN rooms r ON r.id=c.room_id LEFT JOIN floors f ON f.id=r.floor_id
        WHERE {column}=?''', (identity,)).fetchall()
    for row in classes:
        revoke_class_displays(conn, row['id'], actor_user_id, f'{kind}_deactivated')


def _fetch_location_hierarchy(conn):
    buildings = conn.execute(
        """
        SELECT b.*, COUNT(DISTINCT f.id) AS floor_count,
               COUNT(DISTINCT r.id) AS room_count
        FROM buildings b
        LEFT JOIN floors f ON f.building_id = b.id
        LEFT JOIN rooms r ON r.floor_id = f.id
        GROUP BY b.id
        ORDER BY b.name COLLATE NOCASE
        """
    ).fetchall()
    floors = conn.execute(
        """
        SELECT f.*, b.name AS building_name, b.is_active AS building_is_active, COUNT(r.id) AS room_count
        FROM floors f
        JOIN buildings b ON b.id = f.building_id
        LEFT JOIN rooms r ON r.floor_id = f.id
        GROUP BY f.id
        ORDER BY b.name COLLATE NOCASE, f.sort_order, f.name COLLATE NOCASE
        """
    ).fetchall()
    rooms = conn.execute(
        """
        SELECT r.*, f.name AS floor_name, f.sort_order AS floor_sort_order,
               b.id AS building_id, b.name AS building_name,
               cd.id AS active_camera_id, cd.name AS active_camera_name,
               cd.last_test_status, cd.lifecycle_status, cd.last_health_check_at,
               (SELECT COUNT(*) FROM classes c WHERE c.room_id = r.id) AS class_count,
               (SELECT COUNT(*) FROM attendance_sessions ats
                JOIN classes c ON c.id = ats.class_id WHERE c.room_id = r.id) AS session_count
        FROM rooms r
        JOIN floors f ON f.id = r.floor_id
        JOIN buildings b ON b.id = f.building_id
        LEFT JOIN camera_devices cd ON cd.room_id = r.id AND cd.is_active = 1
        ORDER BY b.name COLLATE NOCASE, f.sort_order, f.name COLLATE NOCASE,
                 r.room_code COLLATE NOCASE
        """
    ).fetchall()
    from services.camera_health import describe_stored_camera_health
    room_data = [dict(row) for row in rooms]
    for room in room_data:
        observation = describe_stored_camera_health(room)
        room["camera_health_state"] = observation["label"]
        room["camera_health_detail"] = observation["detail"] if room["active_camera_id"] else "No active camera is assigned."
    return [dict(row) for row in buildings], [dict(row) for row in floors], room_data


@settings_bp.route("/settings/locations")
@login_required
@permission_required("manage_locations")
def manage_locations():
    """Render the Dean-owned institutional location hierarchy."""
    with managed_transaction(commit=False) as conn:
        buildings, floors, rooms = _fetch_location_hierarchy(conn)
    return render_template(
        "settings/locations.html",
        buildings=buildings,
        floors=floors,
        rooms=rooms,
        room_types=ROOM_TYPES,
    )


@settings_bp.route("/api/buildings", methods=["GET"])
@login_required
@permission_required("manage_locations", "manage_cameras")
def api_buildings():
    with managed_transaction(commit=False) as conn:
        buildings, _, _ = _fetch_location_hierarchy(conn)
    return jsonify(buildings), 200


@settings_bp.route("/api/buildings", methods=["POST"])
@login_required
@permission_required("manage_locations")
def api_add_building():
    try:
        name = _location_text(_location_data(), "name", required=True, normalized=True)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            denied = _authorize_location_write(conn)
            if denied is not None:
                return denied
            cursor = conn.execute(
                "INSERT INTO buildings (name, normalized_name, is_active) VALUES (?, ?, 1)",
                (name, _normalized_name(name)),
            )
            log_system_event(conn, "building_created", session.get("user_id"), {"building_id": cursor.lastrowid})
            return jsonify({"message": "Building created.", "id": cursor.lastrowid}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "A building with that normalized name already exists."}), 400


@settings_bp.route("/api/buildings/<int:building_id>", methods=["PUT"])
@login_required
@permission_required("manage_locations")
def api_update_building(building_id):
    try:
        name = _location_text(_location_data(), "name", required=True, normalized=True)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            denied = _authorize_location_write(conn)
            if denied is not None:
                return denied
            existing = conn.execute("SELECT id FROM buildings WHERE id = ?", (building_id,)).fetchone()
            if not existing:
                return jsonify({"error": "Building not found."}), 404
            conn.execute(
                """
                UPDATE buildings SET name = ?, normalized_name = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (name, _normalized_name(name), building_id),
            )
            conn.execute(
                """
                UPDATE rooms SET building = ?, updated_at = CURRENT_TIMESTAMP
                WHERE floor_id IN (SELECT id FROM floors WHERE building_id = ?)
                """,
                (name, building_id),
            )
            return jsonify({"message": "Building updated.", "id": building_id}), 200
    except sqlite3.IntegrityError:
        return jsonify({"error": "A building with that normalized name already exists."}), 400


@settings_bp.route("/api/buildings/<int:building_id>/toggle", methods=["POST"])
@login_required
@permission_required("manage_locations")
def api_toggle_building(building_id):
    try:
        desired_status = _location_desired_status()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    # Keep preview/attendance acquisition outside the check-and-write interval.
    with session_manager.lifecycle_guard(), managed_transaction(commit=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        denied = _authorize_location_write(conn)
        if denied is not None:
            return denied
        building = conn.execute("SELECT is_active FROM buildings WHERE id = ?", (building_id,)).fetchone()
        if not building:
            return jsonify({"error": "Building not found."}), 404
        new_status = desired_status if desired_status is not None else (0 if building["is_active"] else 1)
        if new_status == building["is_active"]:
            return jsonify({"message": "Location already has the requested status.", "is_active": new_status}), 200
        if not new_status and _location_has_open_attendance(conn, "building", building_id):
            return jsonify({"error": "End attendance in this location before deactivating it."}), 409
        if not new_status and _location_has_owned_camera(conn, "building", building_id):
            return jsonify({"error": "A camera in this location is busy. Stop its live session, tuner or preview before deactivating the location.", "code": "camera_busy"}), 409
        conn.execute(
            "UPDATE buildings SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_status, building_id),
        )
        if not new_status:
            _retire_location_displays(conn, 'building', building_id, session.get('user_id'))
            conn.execute(
                """
                UPDATE camera_devices SET is_active = 0, is_default = 0, updated_at = CURRENT_TIMESTAMP
                WHERE room_id IN (
                    SELECT r.id FROM rooms r JOIN floors f ON f.id = r.floor_id
                    WHERE f.building_id = ?
                )
                """,
                (building_id,),
            )
        return jsonify({"message": "Building status updated.", "is_active": new_status}), 200


@settings_bp.route("/api/buildings/<int:building_id>", methods=["DELETE"])
@login_required
@permission_required("manage_locations")
def api_delete_building(building_id):
    with managed_transaction(commit=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        denied = _authorize_location_write(conn)
        if denied is not None:
            return denied
        child_count = conn.execute(
            "SELECT COUNT(*) FROM floors WHERE building_id = ?", (building_id,)
        ).fetchone()[0]
        if child_count:
            return jsonify({"error": "Referenced buildings cannot be deleted; deactivate them instead."}), 400
        cursor = conn.execute("DELETE FROM buildings WHERE id = ?", (building_id,))
        if not cursor.rowcount:
            return jsonify({"error": "Building not found."}), 404
        return jsonify({"message": "Unused building deleted."}), 200


@settings_bp.route("/api/floors", methods=["GET"])
@login_required
@permission_required("manage_locations", "manage_cameras")
def api_floors():
    with managed_transaction(commit=False) as conn:
        _, floors, _ = _fetch_location_hierarchy(conn)
    return jsonify(floors), 200


def _read_floor_payload():
    data = _location_data()
    return {
        "building_id": _location_id(data, "building_id"),
        "name": _location_text(data, "name", required=True, normalized=True),
        "sort_order": _location_integer(data.get("sort_order", 0), "Display order", default=0, minimum=-100000, maximum=100000),
    }


@settings_bp.route("/api/floors", methods=["POST"])
@login_required
@permission_required("manage_locations")
def api_add_floor():
    try:
        payload = _read_floor_payload()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            denied = _authorize_location_write(conn)
            if denied is not None:
                return denied
            building = conn.execute(
                "SELECT id FROM buildings WHERE id = ? AND is_active = 1",
                (int(payload["building_id"]),),
            ).fetchone()
            if not building:
                return jsonify({"error": "Select an active building."}), 400
            cursor = conn.execute(
                """
                INSERT INTO floors
                    (building_id, name, normalized_name, sort_order, is_active)
                VALUES (?, ?, ?, ?, 1)
                """,
                (building["id"], payload["name"], _normalized_name(payload["name"]), payload["sort_order"]),
            )
            return jsonify({"message": "Floor created.", "id": cursor.lastrowid}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "That floor already exists in the selected building."}), 400


@settings_bp.route("/api/floors/<int:floor_id>", methods=["PUT"])
@login_required
@permission_required("manage_locations")
def api_update_floor(floor_id):
    try:
        payload = _read_floor_payload()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            denied = _authorize_location_write(conn)
            if denied is not None:
                return denied
            existing = conn.execute("SELECT building_id FROM floors WHERE id = ?", (floor_id,)).fetchone()
            if not existing:
                return jsonify({"error": "Floor not found."}), 404
            building = conn.execute(
                "SELECT id, name FROM buildings WHERE id = ? AND (is_active = 1 OR id = ?)",
                (payload["building_id"], existing["building_id"]),
            ).fetchone()
            if not building:
                return jsonify({"error": "Select an active building."}), 400
            cursor = conn.execute(
                """
                UPDATE floors
                SET building_id = ?, name = ?, normalized_name = ?, sort_order = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (building["id"], payload["name"], _normalized_name(payload["name"]), payload["sort_order"], floor_id),
            )
            if not cursor.rowcount:
                return jsonify({"error": "Floor not found."}), 404
            conn.execute(
                """
                UPDATE rooms SET building = ?, floor = ?, updated_at = CURRENT_TIMESTAMP
                WHERE floor_id = ?
                """,
                (building["name"], payload["name"], floor_id),
            )
            return jsonify({"message": "Floor updated.", "id": floor_id}), 200
    except sqlite3.IntegrityError:
        return jsonify({"error": "That floor already exists in the selected building."}), 400


@settings_bp.route("/api/floors/<int:floor_id>/toggle", methods=["POST"])
@login_required
@permission_required("manage_locations")
def api_toggle_floor(floor_id):
    try:
        desired_status = _location_desired_status()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    with session_manager.lifecycle_guard(), managed_transaction(commit=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        denied = _authorize_location_write(conn)
        if denied is not None:
            return denied
        floor = conn.execute("SELECT is_active FROM floors WHERE id = ?", (floor_id,)).fetchone()
        if not floor:
            return jsonify({"error": "Floor not found."}), 404
        new_status = desired_status if desired_status is not None else (0 if floor["is_active"] else 1)
        if new_status == floor["is_active"]:
            return jsonify({"message": "Location already has the requested status.", "is_active": new_status}), 200
        if not new_status and _location_has_open_attendance(conn, "floor", floor_id):
            return jsonify({"error": "End attendance in this location before deactivating it."}), 409
        if not new_status and _location_has_owned_camera(conn, "floor", floor_id):
            return jsonify({"error": "A camera in this location is busy. Stop its live session, tuner or preview before deactivating the location.", "code": "camera_busy"}), 409
        if not new_status:
            _retire_location_displays(conn, 'floor', floor_id, session.get('user_id'))
        conn.execute(
            "UPDATE floors SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_status, floor_id),
        )
        if not new_status:
            conn.execute(
                """
                UPDATE camera_devices SET is_active = 0, is_default = 0, updated_at = CURRENT_TIMESTAMP
                WHERE room_id IN (SELECT id FROM rooms WHERE floor_id = ?)
                """,
                (floor_id,),
            )
        return jsonify({"message": "Floor status updated.", "is_active": new_status}), 200


@settings_bp.route("/api/floors/<int:floor_id>", methods=["DELETE"])
@login_required
@permission_required("manage_locations")
def api_delete_floor(floor_id):
    with managed_transaction(commit=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        denied = _authorize_location_write(conn)
        if denied is not None:
            return denied
        room_count = conn.execute("SELECT COUNT(*) FROM rooms WHERE floor_id = ?", (floor_id,)).fetchone()[0]
        if room_count:
            return jsonify({"error": "Referenced floors cannot be deleted; deactivate them instead."}), 400
        cursor = conn.execute("DELETE FROM floors WHERE id = ?", (floor_id,))
        if not cursor.rowcount:
            return jsonify({"error": "Floor not found."}), 404
        return jsonify({"message": "Unused floor deleted."}), 200


@settings_bp.route("/api/rooms/<int:room_id>", methods=["DELETE"])
@login_required
@permission_required("manage_locations")
def api_delete_room(room_id):
    with managed_transaction(commit=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        denied = _authorize_location_write(conn)
        if denied is not None:
            return denied
        class_count = conn.execute("SELECT COUNT(*) FROM classes WHERE room_id = ?", (room_id,)).fetchone()[0]
        camera_count = conn.execute("SELECT COUNT(*) FROM camera_devices WHERE room_id = ?", (room_id,)).fetchone()[0]
        if class_count or camera_count:
            return jsonify({"error": "Referenced rooms cannot be deleted; deactivate them instead."}), 400
        cursor = conn.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        if not cursor.rowcount:
            return jsonify({"error": "Room not found."}), 404
        return jsonify({"message": "Unused room deleted."}), 200


def _read_room_payload():
    data = _location_data(allow_form=True)
    active = data.get("is_active", True)
    if request.is_json and type(active) is not bool:
        raise ValueError("Room availability must use true or false.")
    return {
        "room_code": _location_text(data, "room_code", 80, required=True, normalized=True),
        "room_name": _location_text(data, "room_name", normalized=True),
        "floor_id": _location_id(data, "floor_id"),
        "department": _location_text(data, "department", default="Computer Studies Department") or "Computer Studies Department",
        "room_type": _location_text(data, "room_type", default="Classroom") or "Classroom",
        "capacity": _location_integer(data.get("capacity", ""), "Capacity"),
        "notes": _location_text(data, "notes", 2000),
        "is_active": active if request.is_json else str(active).lower() in ("1", "true", "on", "yes"),
    }


def _validate_room_payload(payload):
    errors = []
    if not payload["room_code"]:
        errors.append("Room code is required.")
    if payload["floor_id"] is None:
        errors.append("Select a valid floor.")
    if payload["room_type"] not in ROOM_TYPES:
        errors.append("Room type is not supported.")
    if payload["capacity"] is not None and payload["capacity"] < 0:
        errors.append("Capacity must be zero or greater.")
    return errors


@settings_bp.route("/api/rooms", methods=["GET"])
@login_required
@permission_required("manage_cameras", "manage_locations")
def api_rooms():
    """Return official active rooms for camera and class setup."""
    with managed_transaction(commit=False) as conn:
        include_inactive = request.args.get("include_inactive") == "1"
        query = """
            SELECT r.*, f.name AS floor_name, f.sort_order AS floor_sort_order,
                   b.id AS building_id, b.name AS building_name
            FROM rooms r
            JOIN floors f ON f.id = r.floor_id
            JOIN buildings b ON b.id = f.building_id
        """
        if not include_inactive:
            query += " WHERE r.is_active = 1 AND f.is_active = 1 AND b.is_active = 1"
        query += " ORDER BY b.name COLLATE NOCASE, f.sort_order, f.name COLLATE NOCASE, r.room_code COLLATE NOCASE"
        rows = conn.execute(query).fetchall()
        return jsonify([_serialize_room(row) for row in rows]), 200
@settings_bp.route("/api/rooms/coverage", methods=["GET"])
@login_required
@permission_required("manage_cameras")
def api_rooms_coverage():
    """Return room coverage metadata for the setup board."""
    with managed_transaction(commit=False) as conn:
        return jsonify(_fetch_room_coverage(conn)), 200
@settings_bp.route("/api/rooms", methods=["POST"])
@login_required
@permission_required("manage_locations")
def api_add_room():
    """Create an official room record."""
    try:
        payload = _read_room_payload()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    errors = _validate_room_payload(payload)
    if errors:
        return jsonify({"error": " ".join(errors)}), 400

    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            denied = _authorize_location_write(conn)
            if denied is not None:
                return denied
            floor = conn.execute(
                """
                SELECT f.id, f.name AS floor_name, b.name AS building_name
                FROM floors f JOIN buildings b ON b.id = f.building_id
                WHERE f.id = ? AND f.is_active = 1 AND b.is_active = 1
                """,
                (int(payload["floor_id"]),),
            ).fetchone()
            if not floor:
                return jsonify({"error": "Select an active floor in an active building."}), 400
            cursor = conn.execute(
                """
                INSERT INTO rooms
                    (floor_id, room_code, room_name, building, floor, department,
                     room_type, capacity, is_active, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    floor["id"],
                    payload["room_code"],
                    payload["room_name"] or f"Room {payload['room_code']}",
                    floor["building_name"],
                    floor["floor_name"],
                    payload["department"],
                    payload["room_type"],
                    payload["capacity"],
                    1 if payload["is_active"] else 0,
                    payload["notes"],
                ),
            )
            room_id = cursor.lastrowid
            log_system_event(conn, "room_created", session.get("user_id"), {"room_code": payload["room_code"]})
            return jsonify({"message": "Room added successfully.", "id": room_id}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Room code already exists."}), 400
    except Exception:
        logger.exception("Failed to add room.")
        return jsonify({"error": "An error occurred while adding the room."}), 500
@settings_bp.route("/api/rooms/<int:room_id>", methods=["PUT"])
@login_required
@permission_required("manage_locations")
def api_update_room(room_id):
    """Update official room metadata."""
    try:
        payload = _read_room_payload()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    errors = _validate_room_payload(payload)
    if errors:
        return jsonify({"error": " ".join(errors)}), 400

    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            denied = _authorize_location_write(conn)
            if denied is not None:
                return denied
            existing = conn.execute("SELECT id, is_active, floor_id FROM rooms WHERE id = ?", (room_id,)).fetchone()
            if not existing:
                return jsonify({"error": "Room not found."}), 404
            floor = conn.execute(
                """
                SELECT f.id, f.name AS floor_name, b.name AS building_name
                FROM floors f JOIN buildings b ON b.id = f.building_id
                WHERE f.id = ? AND ((f.is_active = 1 AND b.is_active = 1) OR f.id = ?)
                """,
                (payload["floor_id"], existing["floor_id"]),
            ).fetchone()
            if not floor:
                return jsonify({"error": "Select an active floor in an active building."}), 400
            conn.execute(
                """
                UPDATE rooms
                SET floor_id = ?,
                    room_code = ?,
                    room_name = ?,
                    building = ?,
                    floor = ?,
                    department = ?,
                    room_type = ?,
                    capacity = ?,
                    is_active = ?,
                    notes = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    floor["id"],
                    payload["room_code"],
                    payload["room_name"] or f"Room {payload['room_code']}",
                    floor["building_name"],
                    floor["floor_name"],
                    payload["department"],
                    payload["room_type"],
                    payload["capacity"],
                    existing["is_active"],
                    payload["notes"],
                    room_id,
                ),
            )
            log_system_event(conn, "room_updated", session.get("user_id"), {"room_id": room_id, "room_code": payload["room_code"]})
            return jsonify({"message": "Room updated successfully.", "id": room_id}), 200
    except sqlite3.IntegrityError:
        return jsonify({"error": "Room code already exists."}), 400
    except Exception:
        logger.exception("Failed to update room.")
        return jsonify({"error": "An error occurred while updating the room."}), 500
@settings_bp.route("/api/rooms/<int:room_id>/toggle", methods=["POST"])
@login_required
@permission_required("manage_locations")
def api_toggle_room(room_id):
    """Activate or deactivate a room when it has no active dependencies."""
    try:
        desired_status = _location_desired_status()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    try:
        with session_manager.lifecycle_guard(), managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            denied = _authorize_location_write(conn)
            if denied is not None:
                return denied
            room = conn.execute("SELECT id, is_active FROM rooms WHERE id = ?", (room_id,)).fetchone()
            if not room:
                return jsonify({"error": "Room not found."}), 404
            new_status = desired_status if desired_status is not None else (0 if room["is_active"] else 1)
            if new_status == room["is_active"]:
                return jsonify({"message": "Location already has the requested status.", "is_active": new_status}), 200
            if not new_status and _location_has_open_attendance(conn, "room", room_id):
                return jsonify({"error": "End attendance in this location before deactivating it."}), 409
            if not new_status and _location_has_owned_camera(conn, "room", room_id):
                return jsonify({"error": "A camera in this location is busy. Stop its live session, tuner or preview before deactivating the location.", "code": "camera_busy"}), 409
            if new_status == 0:
                _retire_location_displays(conn, 'room', room_id, session.get('user_id'))
                conn.execute(
                    """
                    UPDATE camera_devices
                    SET is_active = 0, is_default = 0, updated_at = CURRENT_TIMESTAMP
                    WHERE room_id = ? AND is_active = 1
                    """,
                    (room_id,),
                )
            conn.execute(
                "UPDATE rooms SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_status, room_id),
            )
            log_system_event(conn, "room_status_toggled", session.get("user_id"), {"room_id": room_id, "is_active": new_status})
            return jsonify({"message": "Room status updated.", "is_active": new_status}), 200
    except Exception:
        logger.exception("Failed to toggle room status.")
        return jsonify({"error": "An error occurred while updating room status."}), 500
@settings_bp.route("/api/cameras", methods=["GET"])
@login_required
@permission_required("manage_cameras")
def api_cameras():
    """Return all configured cameras."""
    with managed_transaction(commit=False) as conn:
        cameras = _fetch_cameras_for_management(conn)
        return jsonify([_serialize_camera(camera) for camera in cameras]), 200


def _serialize_registration_camera(camera):
    """Expose only the camera metadata needed by student registration."""
    data = dict(camera)
    return {
        "id": data["id"],
        "name": data["name"],
        "room_number": data.get("room_code") or "Fallback",
        "lifecycle_status": data.get("lifecycle_status") or "Ready",
    }


@settings_bp.route("/api/registration/cameras", methods=["GET"])
@login_required
@permission_required(*REGISTRATION_CAMERA_PERMISSIONS)
def api_registration_cameras():
    """Return operational registration cameras without management metadata."""
    with managed_transaction(commit=False) as conn:
        cameras = [
            camera
            for camera in _get_active_cameras(conn)
            if camera["lifecycle_status"] == "Ready"
        ]
    return jsonify([_serialize_registration_camera(camera) for camera in cameras]), 200

class _OnvifDiscoveryUnavailable(RuntimeError):
    """Raised when WS-Discovery cannot complete its local UDP operation."""


def _onvif_discovery_message():
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing">'
        '<s:Header>'
        '<a:Action s:mustUnderstand="1">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>'
        f'<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>'
        '<a:ReplyTo><a:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address></a:ReplyTo>'
        '<a:To s:mustUnderstand="1">urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>'
        '</s:Header>'
        '<s:Body>'
        '<Probe xmlns="http://schemas.xmlsoap.org/ws/2005/04/discovery"><d:Types xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dp0="http://www.onvif.org/ver10/network/wsdl">dp0:NetworkVideoTransmitter</d:Types></Probe>'
        '</s:Body>'
        '</s:Envelope>'
    )


def _onvif_discovery_interface_address():
    """Use the local IPv4 address in the request URL when it is truly local."""
    host = request.host.split(":", 1)[0].strip("[]")
    try:
        candidate = ipaddress.ip_address(host)
    except ValueError:
        return None
    if (
        candidate.version != 4
        or candidate.is_loopback
        or candidate.is_unspecified
        or candidate.is_multicast
    ):
        return None
    try:
        local_addresses = {
            address[4][0]
            for address in socket.getaddrinfo(
                socket.gethostname(),
                None,
                socket.AF_INET,
                socket.SOCK_DGRAM,
            )
        }
    except OSError:
        return None
    return str(candidate) if str(candidate) in local_addresses else None


def _discover_onvif_cameras(discovery_message, interface_address=None):
    """Send one subnet-scoped WS-Discovery probe and return unique replies."""
    discovered = []
    seen_ips = set()
    sock = None
    operation = "creating socket"
    deadline = time.monotonic() + ONVIF_DISCOVERY_TIMEOUT_SECONDS

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

        # Keep discovery on the directly connected subnet. The operating system
        # selects its usable multicast interface instead of probing address ranges.
        operation = "configuring socket"
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        if interface_address:
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(interface_address),
            )

        operation = "binding socket"
        sock.bind((interface_address or "", 0))
        operation = "sending probe"
        sock.sendto(discovery_message.encode("utf-8"), ONVIF_DISCOVERY_ADDRESS)

        operation = "receiving replies"
        while len(discovered) < ONVIF_DISCOVERY_MAX_UNIQUE_REPLIES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                # Reaching the response deadline with no (or fewer) replies is a
                # successful discovery operation, including the empty result.
                break

            if not isinstance(addr, tuple) or not addr:
                continue
            ip = str(addr[0])
            if not ip or ip in seen_ips:
                continue

            seen_ips.add(ip)
            text = data.decode("utf-8", errors="ignore")
            model_match = re.search(r"hardware/([^/\s]+)", text)
            model = model_match.group(1)[:120] if model_match else "Unknown Camera"

            discovered.append({
                "ip": ip,
                "model": model,
                "name": f"Camera ({model})",
            })
    except OSError as exc:
        # Log only the failed operation. Socket exception text can contain local
        # interface details and should not be returned or copied to the UI.
        logger.warning("ONVIF discovery failed while %s.", operation)
        raise _OnvifDiscoveryUnavailable from exc
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                logger.warning("ONVIF discovery socket close failed.")

    return discovered


@settings_bp.route("/api/cameras/discover", methods=["GET"])
@login_required
@permission_required("manage_cameras")
def api_cameras_discover():
    """Discover ONVIF cameras on the local network using WS-Discovery."""
    try:
        discovered = _discover_onvif_cameras(
            _onvif_discovery_message(),
            _onvif_discovery_interface_address(),
        )
    except _OnvifDiscoveryUnavailable:
        response = jsonify({
            "error": "Camera discovery is temporarily unavailable. Try again.",
        })
        response.headers["Retry-After"] = "5"
        return response, 503
    return jsonify(discovered), 200


@settings_bp.route("/api/cameras/discovery/networks", methods=["GET"])
@login_required
@permission_required("manage_cameras")
def api_camera_discovery_networks():
    """Offer only active IPv4 addresses belonging to this application host."""
    try:
        interfaces = list_discovery_interfaces()
    except DiscoveryUnavailable:
        return jsonify({"error": "The computer's network list is unavailable. Try again."}), 503
    selected = select_discovery_interface(interfaces)
    response = jsonify({
        "interfaces": interfaces,
        "selected_interface": selected["id"] if selected else None,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@settings_bp.route("/api/cameras/discovery", methods=["GET"])
@login_required
@permission_required("manage_cameras")
def api_camera_guided_discovery():
    """Find cameras and describe their network without modifying any device."""
    try:
        result = discover_cameras(request.args.get("interface"))
    except InvalidDiscoveryInterface:
        return jsonify({
            "error": "That network is no longer available. Choose a connected network and try again.",
        }), 400
    except DiscoveryBusy:
        response = jsonify({"error": "A camera search is already running. Wait a few seconds and try again."})
        response.headers["Retry-After"] = "3"
        return response, 409
    except DiscoveryUnavailable:
        response = jsonify({"error": "Camera discovery is temporarily unavailable. Try again."})
        response.headers["Retry-After"] = "5"
        return response, 503
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response


@settings_bp.route("/api/cameras/health", methods=["GET"])
@login_required
@permission_required("manage_cameras")
def api_cameras_health():
    """Return camera health metadata without entering a live session."""
    with managed_transaction(commit=False) as conn:
        cameras = _fetch_cameras_for_management(conn)
        return jsonify([_serialize_camera(camera) for camera in cameras]), 200
def _read_camera_payload(existing=None):
    data = request.get_json(silent=True) if request.is_json else request.form
    if not isinstance(data, dict):
        raise CameraManagementError("Submit a valid camera form.", "camera_input_invalid", 400)
    defaults = dict(existing) if existing is not None else {}
    payload = {
        "name": "", "camera_type": "rtsp", "room_id": "", "room_number": "",
        "placement_notes": "", "lifecycle_status": "Ready",
        "resolution_width": 640, "resolution_height": 480, "target_fps": 30,
        "match_threshold": 0.45, "is_default": False,
    }
    for key in payload:
        payload[key] = data.get(key, defaults.get(key, payload[key]))
    for key in ("source", "network_url", "manual_rtsp_url", "ip", "username", "password", "rtsp_port", "rtsp_path"):
        # Never merge the stored encrypted source into request/UI fields.
        payload[key] = data.get(key, "")
    for key in ("name", "camera_type", "room_id", "room_number", "placement_notes",
                "lifecycle_status", "source", "network_url", "manual_rtsp_url", "ip", "username", "rtsp_port", "rtsp_path"):
        value = payload[key]
        if value is None:
            value = ""
        elif type(value) is int and key in ("room_id", "source", "rtsp_port"):
            value = str(value)
        elif not isinstance(value, str):
            raise CameraManagementError(
                f"{key.replace('_', ' ').capitalize()} must be text.", "camera_input_invalid", 400,
            )
        payload[key] = value.strip()
    # Password whitespace belongs to the credential; URL construction encodes
    # it below without changing the camera's actual password.
    if payload["password"] is None:
        payload["password"] = ""
    elif not isinstance(payload["password"], str):
        raise CameraManagementError("Camera password must be text.", "camera_input_invalid", 400)
    payload["camera_type"] = payload["camera_type"].lower()
    connection_fields = ("source",) if payload["camera_type"] == "local" else (
        "network_url", "manual_rtsp_url", "ip", "username", "password", "source", "rtsp_port", "rtsp_path")
    payload["replace_source"] = existing is None or data.get("replace_connection") is True or any(payload[k] for k in connection_fields)
    if existing is not None and payload["camera_type"] != existing["camera_type"]:
        payload["replace_source"] = True
    return payload


def _normalize_camera_host(host):
    """Accept bare IP/DNS or exactly one bracketed IPv6 address, without I/O."""
    bracketed = "[" in host or "]" in host
    if bracketed:
        if not re.fullmatch(r"\[[^\[\]]+\]", host):
            raise ValueError("Invalid camera host")
        host = host[1:-1]
    # ipaddress permits arbitrary IPv6 scope text; it is not URL validation.
    if "%" in host:
        _, scope = host.split("%", 1)
        if not re.fullmatch(r"[A-Za-z0-9._~-]+", scope):
            raise ValueError("Invalid camera host")
    try:
        parsed = ipaddress.ip_address(host)
    except ValueError:
        if bracketed or not re.fullmatch(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise ValueError("Invalid camera host") from None
        return host
    if (bracketed or "%" in host) and parsed.version != 6:
        raise ValueError("Invalid camera host")
    return str(parsed)


def _valid_camera_url_authority(parsed):
    """urlsplit alone accepts characters after a closing IPv6 bracket."""
    authority = parsed.netloc.rsplit("@", 1)[-1]
    if "[" not in authority and "]" not in authority:
        return True
    match = re.fullmatch(r"(\[[^\[\]]+\])(?::[0-9]+)?", authority)
    if not match:
        return False
    _normalize_camera_host(match.group(1))
    return True


def _preflight_camera_config(source, camera):
    """Use the same escaped-byte contract and options as live capture."""
    serialize_capture_config(build_capture_config(
        source, width=camera["resolution_width"], height=camera["resolution_height"],
        target_fps=camera["target_fps"], open_timeout=3.0, read_timeout=3.0,
    ))


def _validate_camera_payload(payload, existing=None):
    errors = []
    if not payload["name"]:
        errors.append("Camera name is required.")
    if payload["camera_type"] not in {"local", "rtsp"}:
        errors.append("Camera type must be local or rtsp.")
    if payload["replace_source"]:
        if payload["camera_type"] == "local":
            index = payload["source"].lstrip("0") or "0"
            if (not payload["source"].isascii() or not payload["source"].isdigit()
                    or len(index) > 10 or int(index) > 2**31 - 1):
                errors.append("Local camera source must be a non-negative camera index.")
            else:
                payload["source"] = index
        elif payload["camera_type"] == "rtsp":
            if payload["network_url"] or payload["manual_rtsp_url"]:
                # The legacy database type 'rtsp' represents network sources.
                # Keep existing installations compatible without a schema change.
                network_url = payload["network_url"] or payload["manual_rtsp_url"]
                schemes = {"rtsp", "rtsps", "http", "https"} if payload["network_url"] else {"rtsp", "rtsps"}
                try:
                    parsed = urlsplit(network_url)
                    valid = (parsed.scheme.lower() in schemes and parsed.hostname and not parsed.fragment
                             and _valid_camera_url_authority(parsed)
                             and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in network_url))
                    if parsed.port is not None and not 0 < parsed.port <= 65535:
                        valid = False
                except ValueError:
                    valid = False
                if not valid:
                    errors.append("Enter a valid camera stream URL with a hostname and an allowed protocol (RTSP/RTSPS or HTTP/HTTPS for network streams).")
            elif payload["ip"]:
                try:
                    payload["ip"] = _normalize_camera_host(payload["ip"])
                    valid = True
                except ValueError:
                    valid = False
                if not valid:
                    errors.append("Enter a camera IP address or hostname without a URL, port or credentials.")
                port = payload["rtsp_port"] or "554"
                digits = port.lstrip("0") or "0"
                if (not port.isascii() or not port.isdigit() or len(digits) > 5
                        or not 0 < int(digits) <= 65535):
                    errors.append("RTSP port must be a number from 1 to 65535.")
                else:
                    payload["rtsp_port"] = digits
                path = payload["rtsp_path"] or "/Streaming/Channels/101"
                if not path.startswith("/") or path.startswith("//") or "#" in path or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in path):
                    errors.append("RTSP stream path must begin with one slash and contain no spaces or fragment. Use the camera or recorder's channel path.")
            else:
                errors.append("Enter a camera stream URL or an RTSP camera address to replace connection details.")
    if payload["lifecycle_status"] not in CAMERA_LIFECYCLE_STATUSES:
        errors.append("Camera lifecycle status is not supported.")
    for field in ("resolution_width", "resolution_height", "target_fps"):
        value = parse_record_id(payload[field])
        if value is None:
            errors.append(f"{field.replace('_', ' ').title()} must be a positive whole number within the supported range.")
        else:
            payload[field] = value
    try:
        if isinstance(payload["match_threshold"], bool):
            raise ValueError
        payload["match_threshold"] = float(payload["match_threshold"])
        if not math.isfinite(payload["match_threshold"]) or not 0 < payload["match_threshold"] <= 1:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        errors.append("Sensitivity must be a number greater than zero and at most one.")
    default = payload["is_default"]
    if type(default) is bool or (type(default) is int and default in (0, 1)):
        payload["is_default"] = bool(default)
    elif isinstance(default, str) and default.lower() in ("1", "true", "on", "yes", "0", "false", "off", "no", ""):
        payload["is_default"] = default.lower() in ("1", "true", "on", "yes")
    else:
        errors.append("Choose whether this is the default camera using true or false.")
    if payload["room_id"] and parse_record_id(payload["room_id"]) is None:
        errors.append("Select a valid official room.")
    unchanged_legacy_room = (existing is not None and existing["room_id"] is None
                             and str(payload["room_id"] or "") == str(existing["room_id"] or ""))
    if not payload["is_default"] and not payload["room_id"] and not payload["room_number"] and not unchanged_legacy_room:
        errors.append("Select an official room for classroom cameras.")
    # Names/placement edits must not decrypt or rewrite legacy sources. Changed
    # capture options also affect serialized size, so check their new wire form
    # against the unchanged plaintext without re-encryption or a status reset.
    capture_options_changed = existing is not None and any(
        payload[key] != existing[key] for key in ("resolution_width", "resolution_height", "target_fps")
    )
    if not errors and (payload["replace_source"] or capture_options_changed):
        try:
            if payload["replace_source"]:
                raw_source = _raw_camera_source(payload)
                raw_source.encode("utf-8")  # Fail before encryption for malformed Unicode.
                source = int(raw_source) if payload["camera_type"] == "local" else raw_source
            else:
                source = _camera_source_for_capture(existing)
            _preflight_camera_config(source, payload)
        except (CameraSourceEncryptionError, InvalidToken):
            errors.append("The saved camera connection could not be read. Ask support to check the original camera encryption key before changing capture options.")
        except UnicodeError:
            errors.append("Enter valid text for the camera connection details.")
        except ValueError:
            errors.append("Camera connection configuration is too long or invalid. Shorten the stream address or credentials, or ask your installer for a shorter stream address.")
    return errors


def _raw_camera_source(payload):
    """Build once using normalized, validated form values for save/preflight."""
    if payload["camera_type"] == "local":
        raw_source = str(int(payload["source"]))
    elif payload["network_url"]:
        raw_source = payload["network_url"]
    elif payload["manual_rtsp_url"]:
        raw_source = payload["manual_rtsp_url"]
    else:
        user, password = quote(payload["username"], safe=""), quote(payload["password"], safe="")
        host = payload["ip"]
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        credentials = f"{user}:{password}@" if user or password else ""
        port = payload["rtsp_port"] or "554"
        path = payload["rtsp_path"] or "/Streaming/Channels/101"
        raw_source = f"rtsp://{credentials}{host}:{port}{path}"
    return raw_source


def _stored_camera_source(payload, existing=None):
    if existing is not None and not payload["replace_source"]:
        return existing["source"]
    return encrypt_camera_source(payload["camera_type"], _raw_camera_source(payload))


@settings_bp.route("/api/cameras", methods=["POST"])
@login_required
@permission_required("manage_cameras")
def api_add_camera():
    """Create a camera device."""
    try:
        payload = _read_camera_payload()
    except CameraManagementError as exc:
        return _camera_management_error(exc)
    errors = _validate_camera_payload(payload)
    if errors:
        return jsonify({"error": " ".join(errors)}), 400

    try:
        with session_manager.lifecycle_guard(), managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            actor = _camera_mutation_actor(conn)
            resolved_room_id, resolved_room_number = _resolve_room_from_payload(
                conn, payload["room_id"]
            )
            if not resolved_room_id and payload["room_number"]:
                room = conn.execute(
                    "SELECT id, room_code FROM rooms WHERE LOWER(room_code) = LOWER(?) AND is_active = 1",
                    (payload["room_number"],),
                ).fetchone()
                if room and room_available(conn, room["id"]):
                    resolved_room_id, resolved_room_number = room["id"], room["room_code"]
            if payload["is_default"]:
                resolved_room_id = None
                resolved_room_number = "Fallback"
            elif not resolved_room_id:
                return jsonify({"error": "Select an official room for classroom cameras."}), 400

            try:
                stored_source = _stored_camera_source(payload)
                require_unique_local_source(conn, payload["camera_type"], stored_source,
                                            lifecycle_status=payload["lifecycle_status"])
            except CameraIdentityUnavailable as exc:
                return jsonify({"error": str(exc), "code": "local_camera_conflict"}), 409
            except CameraSourceEncryptionError as exc:
                return jsonify({"error": str(exc)}), 500
            if payload['is_default'] and payload['lifecycle_status'] != 'Ready':
                return jsonify({"error": "The default camera must be Ready."}), 400
            if payload["is_default"]:
                for previous in conn.execute("SELECT * FROM camera_devices WHERE is_default=1").fetchall():
                    require_idle(conn, previous, session_manager)
                conn.execute("UPDATE camera_devices SET is_default = 0")
            cursor = conn.execute(
                """
                INSERT INTO camera_devices
                    (name, camera_type, source, room_id, placement_notes, lifecycle_status,
                     resolution_width, resolution_height, target_fps, match_threshold, is_default, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["name"],
                    payload["camera_type"],
                    stored_source,
                    resolved_room_id,
                    payload["placement_notes"],
                    payload["lifecycle_status"],
                    payload["resolution_width"],
                    payload["resolution_height"],
                    payload["target_fps"],
                    payload["match_threshold"],
                    1 if payload["is_default"] else 0,
                    0 if payload["lifecycle_status"] == "Retired" else 1,
                ),
            )
            camera_id = cursor.lastrowid
            record_change(
                conn,
                "camera_created",
                actor["id"],
                {"name": payload["name"]},
            )
            return jsonify({"message": "Camera added successfully.", "id": camera_id}), 201
    except CameraManagementError as exc:
        return _camera_management_error(exc)
    except sqlite3.IntegrityError:
        return jsonify({"error": "Camera name already exists, or the Room already has an active attendance CCTV."}), 400
    except Exception:
        logger.exception("Failed to add camera.")
        return jsonify({"error": "An error occurred while adding the camera."}), 500
@settings_bp.route("/api/cameras/<int:camera_id>", methods=["PUT"])
@login_required
@permission_required("manage_cameras")
def api_update_camera(camera_id):
    """Update camera metadata and source details."""
    try:
        with session_manager.lifecycle_guard():
            with managed_transaction(commit=False) as conn:
                current = conn.execute("SELECT * FROM camera_devices WHERE id = ?", (camera_id,)).fetchone()
            if not current:
                return jsonify({"error": "Camera not found."}), 404
            if _read_camera_payload(current)["replace_source"] and session_manager.get_camera_owner(camera_id) is not None:
                return jsonify({"error": "Camera is busy. Stop its live session, tuner or preview before replacing its connection.", "code": "camera_busy"}), 409
            with managed_transaction(commit=True) as conn:
                # Serialize read/merge/write so a concurrent metadata edit cannot
                # restore an older encrypted source after connection replacement.
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute("SELECT * FROM camera_devices WHERE id = ?", (camera_id,)).fetchone()
                if not existing:
                    return jsonify({"error": "Camera not found."}), 404
                actor = _camera_mutation_actor(conn)
                payload = _read_camera_payload(existing)
                errors = _validate_camera_payload(payload, existing)
                same_room = str(payload['room_id'] or '') == str(existing['room_id'] or '')
                if errors:
                    return jsonify({"error": " ".join(errors)}), 400
                resolved_room_id, _ = _resolve_room_from_payload(
                    conn, payload["room_id"]
                )
                if not resolved_room_id and payload["room_number"]:
                    room = conn.execute(
                        "SELECT id FROM rooms WHERE LOWER(room_code) = LOWER(?) AND is_active = 1",
                        (payload["room_number"],),
                    ).fetchone()
                    if room:
                        resolved_room_id = room["id"]
                try:
                    stored_source = _stored_camera_source(payload, existing)
                    if payload["replace_source"] or payload["lifecycle_status"] != existing["lifecycle_status"]:
                        require_unique_local_source(conn, payload["camera_type"], stored_source,
                                                    exclude_id=camera_id, lifecycle_status=payload["lifecycle_status"])
                except CameraIdentityUnavailable as exc:
                    return jsonify({"error": str(exc), "code": "local_camera_conflict"}), 409
                except CameraSourceEncryptionError as exc:
                    return jsonify({"error": str(exc)}), 500
                new_default = bool(payload["is_default"] or existing["is_default"])
                if new_default and not existing["is_active"]:
                    return jsonify({"error": "Inactive cameras cannot be set as default."}), 400
                if same_room:
                    resolved_room_id = existing['room_id']
                elif not resolved_room_id and not new_default:
                    return jsonify({"error": "Select an official room for classroom cameras."}), 400

                readiness_changed = (payload["lifecycle_status"] == "Ready") != (existing["lifecycle_status"] == "Ready")
                retiring = payload['lifecycle_status'] == 'Retired'
                active = 0 if retiring else existing['is_active']
                if resolved_room_id != existing['room_id'] or readiness_changed or retiring != (existing['lifecycle_status'] == 'Retired'):
                    require_idle(conn, existing, session_manager)
                    require_available(conn, {**dict(existing), 'room_id': resolved_room_id}, active, payload['lifecycle_status'])
                if new_default and not existing['is_default']:
                    require_idle(conn, existing, session_manager)
                    require_available(conn, {**dict(existing), 'is_default': 1, 'room_id': resolved_room_id}, active, payload['lifecycle_status'])
                    for previous in conn.execute("SELECT * FROM camera_devices WHERE is_default=1").fetchall():
                        require_idle(conn, previous, session_manager)
                conn.execute("UPDATE camera_devices SET is_active=? WHERE id=?", (active, camera_id))

                if new_default:
                    conn.execute("UPDATE camera_devices SET is_default = 0 WHERE id != ?", (camera_id,))
                conn.execute(
                    """
                    UPDATE camera_devices
                    SET name = ?,
                        camera_type = ?,
                        source = ?,
                        room_id = ?,
                        placement_notes = ?,
                        lifecycle_status = ?,
                        resolution_width = ?,
                        resolution_height = ?,
                        target_fps = ?,
                        match_threshold = ?,
                        is_default = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        payload["name"],
                        payload["camera_type"],
                        stored_source,
                        resolved_room_id,
                        payload["placement_notes"],
                        payload["lifecycle_status"],
                        payload["resolution_width"],
                        payload["resolution_height"],
                        payload["target_fps"],
                        payload["match_threshold"],
                        1 if new_default else 0,
                        camera_id,
                    ),
                )
                if payload["replace_source"]:
                    conn.execute(
                        """UPDATE camera_devices SET last_test_status = NULL, last_test_at = NULL,
                           last_health_check_at = NULL, last_successful_frame = NULL,
                           last_failure_reason = NULL, failure_count = 0 WHERE id = ?""",
                        (camera_id,),
                    )
                record_change(
                    conn,
                    "camera_updated",
                    actor["id"],
                    {"camera_id": camera_id, "camera_name": payload["name"]},
                )
                return jsonify({"message": "Camera updated successfully.", "id": camera_id}), 200
    except CameraManagementError as exc:
        return _camera_management_error(exc)
    except sqlite3.IntegrityError:
        return jsonify({"error": "Camera name already exists, or the Room already has an active attendance CCTV."}), 400
    except Exception:
        logger.exception("Failed to update camera.")
        return jsonify({"error": "An error occurred while updating the camera."}), 500
def _camera_mutation_actor(conn):
    actor = authenticated_session_user(conn, session)
    if not actor or actor["requires_password_change"]:
        session.clear()
        raise CameraManagementError("Your login session expired. Sign in again.", "session_expired", 401)
    if not has_permission(actor["role"], "manage_cameras"):
        raise CameraManagementError("Forbidden", "forbidden", 403)
    return actor


def _camera_management_error(exc):
    return jsonify({"error": str(exc), "code": exc.code,
                    "session_expired": exc.status == 401,
                    "login_url": url_for("auth.login")}), exc.status


@settings_bp.route("/api/cameras/<int:camera_id>/availability", methods=["POST"])
@login_required
@permission_required("manage_cameras")
def api_camera_availability(camera_id):
    data = request.get_json(silent=True)
    states = {"Ready": (1, "Ready"), "Inactive": (0, "Ready"),
              "Maintenance": (0, "Maintenance"), "Retired": (0, "Retired")}
    if not isinstance(data, dict) or not isinstance(data.get("state"), str) or data['state'] not in states:
        return jsonify({"error": "Choose Ready, Inactive, Maintenance or Retired.", "code": "camera_state_invalid"}), 400
    active, lifecycle = states[data['state']]
    try:
        with session_manager.lifecycle_guard():
            with managed_transaction(commit=True) as conn:
                conn.execute("BEGIN IMMEDIATE")
                actor = _camera_mutation_actor(conn)
                camera = conn.execute("SELECT * FROM camera_devices WHERE id=?", (camera_id,)).fetchone()
                if not camera:
                    return jsonify({"error": "Camera not found. Refresh inventory.", "code": "camera_not_found"}), 404
                if (camera['is_active'], camera['lifecycle_status']) == (active, lifecycle):
                    return jsonify({"message": "Camera already has the requested availability.", "saved": True,
                                    "id": camera_id, "is_active": active, "lifecycle_status": lifecycle})
                require_unique_local_source(conn, camera["camera_type"], camera["source"],
                                            exclude_id=camera_id, lifecycle_status=lifecycle)
                require_idle(conn, camera, session_manager)
                require_available(conn, camera, active, lifecycle)
                conn.execute("UPDATE camera_devices SET is_active=?,lifecycle_status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (active, lifecycle, camera_id))
                record_change(conn, "camera_availability_changed", actor['id'],
                                 {"camera_id": camera_id, "state": data['state']})
                return jsonify({"message": "Camera availability saved. History has been preserved.", "saved": True,
                                "id": camera_id, "is_active": active, "lifecycle_status": lifecycle})
    except CameraIdentityUnavailable as exc:
        return jsonify({"error": str(exc), "code": "local_camera_conflict"}), 409
    except CameraManagementError as exc:
        return _camera_management_error(exc)
    except sqlite3.IntegrityError:
        return jsonify({"error": "This room already has an active camera. Deactivate it before restoring this camera.", "code": "camera_room_conflict"}), 409
    except Exception:
        logger.error("Camera availability could not be confirmed.")
        return jsonify({"error": "The change could not be confirmed. Refresh saved status before retrying or contact IT Support.",
                        "code": "camera_change_failed"}), 500


@settings_bp.route("/api/cameras/<int:camera_id>/removal", methods=["GET"])
@login_required
@permission_required("manage_cameras")
def api_camera_removal_review(camera_id):
    try:
        with session_manager.lifecycle_guard():
            with managed_transaction(commit=False) as conn:
                camera = conn.execute("SELECT * FROM camera_devices WHERE id=?", (camera_id,)).fetchone()
                if not camera:
                    return _camera_removal_response({"error": "Camera no longer exists. Refresh inventory.", "code": "camera_not_found"}, 404)
                details = deletion_blocker_details(conn, camera, session_manager)
                return _camera_removal_response({"id": camera_id, "name": camera['name'],
                    "can_delete": not details, "blockers": [item['reason'] for item in details],
                    "blocker_details": details})
    except Exception:
        logger.error("Camera removal review could not complete.")
        return _camera_removal_response({"error": "Removal could not be checked. No deletion was requested. Retry the review or contact IT Support.",
                                         "code": "camera_removal_review_failed"}, 500)


def _camera_removal_response(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers['Cache-Control'] = 'no-store, private'
    return response


@settings_bp.route("/api/cameras/<int:camera_id>", methods=["DELETE"])
@login_required
@permission_required("manage_cameras")
def api_delete_unused_camera(camera_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('confirm_name'), str):
        return _camera_removal_response({"error": "Confirm the exact camera name before deleting.", "code": "camera_confirmation_required"}, 400)
    try:
        with session_manager.lifecycle_guard():
            with managed_transaction(commit=True) as conn:
                conn.execute("BEGIN IMMEDIATE")
                actor = _camera_mutation_actor(conn)
                camera = conn.execute("SELECT * FROM camera_devices WHERE id=?", (camera_id,)).fetchone()
                if not camera:
                    return _camera_removal_response({"message": "Camera is already absent. Refresh inventory.",
                                                     "deleted": True, "id": camera_id, "already_absent": True})
                if data['confirm_name'] != camera['name']:
                    return _camera_removal_response({"error": "The camera name does not match. Refresh inventory and review it again.", "code": "camera_name_changed"}, 409)
                details = deletion_blocker_details(conn, camera, session_manager)
                if details:
                    return _camera_removal_response({"error": "Camera deletion is blocked. Review the current reasons and next steps.",
                        "code": "camera_delete_blocked", "id": camera_id, "name": camera['name'],
                        "can_delete": False, "blockers": [item['reason'] for item in details], "blocker_details": details}, 409)
                conn.execute("DELETE FROM camera_devices WHERE id=?", (camera_id,))
                record_change(conn, "camera_unused_deleted", actor['id'],
                                 {"camera_id": camera_id, "camera_name": camera['name']})
                return _camera_removal_response({"message": "Unused camera deleted. No attendance history was removed.",
                                                 "deleted": True, "id": camera_id, "already_absent": False})
    except CameraManagementError as exc:
        return _camera_management_error(exc)
    except sqlite3.IntegrityError:
        detail = removal_blocker('related_record')
        return _camera_removal_response({"error": detail['reason'], "code": "camera_delete_blocked",
            "can_delete": False, "blockers": [detail['reason']], "blocker_details": [detail]}, 409)
    except Exception:
        logger.error("Camera deletion could not be confirmed.")
        return _camera_removal_response({"error": "Deletion could not be confirmed. Refresh inventory and review removal again before retrying. If this continues, contact IT Support.",
                                         "code": "camera_delete_failed"}, 500)


@settings_bp.route("/api/cameras/<int:camera_id>/default", methods=["POST"])
@login_required
@permission_required("manage_cameras")
def api_set_default_camera(camera_id):
    """Replace a default deliberately, preserving both cameras and references."""
    try:
        with session_manager.lifecycle_guard():
            with managed_transaction(commit=True) as conn:
                conn.execute("BEGIN IMMEDIATE")
                actor = _camera_mutation_actor(conn)
                camera = conn.execute("SELECT * FROM camera_devices WHERE id=?", (camera_id,)).fetchone()
                if not camera or not camera['is_active'] or camera['lifecycle_status'] != 'Ready':
                    return jsonify({"error": "Choose an active Ready camera as the default.", "code": "camera_default_unavailable"}), 409
                if camera['is_default']:
                    return jsonify({"message": "This is already the default camera.", "saved": True, "id": camera_id, "is_default": True})
                if not room_available(conn, camera['room_id']):
                    return jsonify({"error": "Assign this camera to an active room before making it the default.", "code": "camera_location"}), 409
                for affected in conn.execute("SELECT * FROM camera_devices WHERE is_default=1 OR id=?", (camera_id,)).fetchall():
                    require_idle(conn, affected, session_manager)
                conn.execute("UPDATE camera_devices SET is_default=0 WHERE is_default=1")
                conn.execute("UPDATE camera_devices SET is_default=1,updated_at=CURRENT_TIMESTAMP WHERE id=?", (camera_id,))
                record_change(conn, "camera_default_set", actor['id'], {"camera_id": camera_id})
                return jsonify({"message": "Default camera replaced. Previous camera and history retained.", "saved": True, "id": camera_id, "is_default": True})
    except CameraManagementError as exc:
        return _camera_management_error(exc)
    except Exception:
        logger.error("Default camera change could not be confirmed.")
        return jsonify({"error": "The change could not be confirmed. Refresh saved status before retrying or contact IT Support.",
                        "code": "camera_change_failed"}), 500


@settings_bp.route("/api/cameras/<int:camera_id>/toggle", methods=["POST"])
@login_required
@permission_required("manage_cameras")
def api_toggle_camera(camera_id):
    """Change camera availability without interrupting an owned capture."""
    try:
        desired_status = _location_desired_status()
    except ValueError:
        return jsonify({"error":"Choose whether the camera should be active using true or false."}), 400
    try:
        # Match attendance/preview startup lock order: lifecycle, then database.
        with session_manager.lifecycle_guard():
            with managed_transaction(commit=True) as conn:
                conn.execute("BEGIN IMMEDIATE")
                actor = authenticated_session_user(conn, session)
                if not actor or actor["requires_password_change"]:
                    session.clear()
                    return jsonify({"error":"Your login session expired.", "session_expired":True,
                                    "login_url":url_for("auth.login")}), 401
                if not has_permission(actor["role"], "manage_cameras"):
                    return jsonify({"error":"Forbidden"}), 403
                camera = conn.execute("SELECT * FROM camera_devices WHERE id=?", (camera_id,)).fetchone()
                if not camera:
                    return jsonify({"error":"Camera not found."}), 404
                new_status = desired_status if desired_status is not None else (0 if camera["is_active"] else 1)
                if new_status and camera['lifecycle_status'] != 'Ready':
                    return jsonify({"error": "Use Restore or Change availability to choose Ready before reactivating this camera."}), 409
                if new_status == camera["is_active"]:
                    return jsonify({"message":"Camera already has the requested status.","is_active":new_status}), 200
                if new_status:
                    require_unique_local_source(conn, camera["camera_type"], camera["source"],
                                                exclude_id=camera_id, lifecycle_status=camera["lifecycle_status"])
                require_idle(conn, camera, session_manager)
                require_available(conn, camera, new_status, camera['lifecycle_status'])
                if session_manager.get_camera_owner(camera_id) is not None:
                    return jsonify({"error":"Camera is busy. Stop its live session, tuner or preview before changing availability.","code":"camera_busy"}), 409
                if conn.execute("""SELECT 1 FROM classes c WHERE (c.camera_id=? OR c.room_id=?)
                    AND (c.is_active=1 OR EXISTS (SELECT 1 FROM attendance_sessions a
                        WHERE a.class_id=c.id AND a.status='Open')) LIMIT 1""", (camera_id,camera["room_id"])).fetchone():
                    return jsonify({"error":"End attendance using this camera or room before changing camera availability.","code":"camera_busy"}), 409
                if not new_status and camera["is_default"]:
                    return jsonify({"error":"Default camera cannot be deactivated."}), 400
                if new_status and camera["room_id"] is not None:
                    room = conn.execute("""SELECT 1 FROM rooms r JOIN floors f ON f.id=r.floor_id
                        JOIN buildings b ON b.id=f.building_id
                        WHERE r.id=? AND r.is_active=1 AND f.is_active=1 AND b.is_active=1""", (camera["room_id"],)).fetchone()
                    if not room:
                        return jsonify({"error":"The room, floor and building must be active before reactivating this camera."}), 409
                conn.execute("UPDATE camera_devices SET is_active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (new_status,camera_id))
                record_change(conn,"camera_status_toggled",actor["id"],{"camera_id":camera_id,"is_active":new_status})
                return jsonify({"message":"Camera status updated.","is_active":new_status}), 200
    except CameraIdentityUnavailable as exc:
        return jsonify({"error": str(exc), "code": "local_camera_conflict"}), 409
    except CameraManagementError as exc:
        return _camera_management_error(exc)
    except sqlite3.IntegrityError:
        return jsonify({"error":"The Room already has an active attendance CCTV. Deactivate it before activating this camera."}), 400
    except Exception:
        logger.exception("Failed to toggle camera status.")
        return jsonify({"error":"An error occurred while updating camera status."}), 500

def _busy_camera_response(exc):
    return jsonify({"error": str(exc), "code": "camera_busy"}), 409


def _secondary_capture(camera, *, startup_deadline=None, external_stop_event=None):
    return LatestFrameCapture(
        _camera_source_for_capture(camera),
        width=camera["resolution_width"], height=camera["resolution_height"],
        target_fps=camera["target_fps"], max_retries=1, reconnect_delay=0.25,
        open_timeout=3.0, read_timeout=3.0, stale_timeout=3.0,
        startup_deadline=startup_deadline, external_stop_event=external_stop_event,
    )


@settings_bp.route("/api/cameras/<int:camera_id>/test", methods=["POST"])
@login_required
@permission_required("manage_cameras")
def api_test_camera(camera_id):
    """Test with exclusive bounded acquisition and a short DB update afterward."""
    lease = None
    saving_result = False
    camera_type = "rtsp"

    def failure_response(code, status=400, **extra):
        diagnostic = camera_test_diagnostic(code, camera_type)
        return jsonify({"error": diagnostic["summary"], "code": diagnostic["code"],
                        "diagnostic": diagnostic, **extra}), status

    try:
        with session_manager.lifecycle_guard():
            with managed_transaction(commit=False) as conn:
                camera = conn.execute("SELECT * FROM camera_devices WHERE id = ?", (camera_id,)).fetchone()
            if not camera:
                return jsonify({"error": "Camera not found."}), 404
            camera_type = camera["camera_type"]
            try:
                _preflight_camera_config(_camera_source_for_capture(camera), camera)
            except (CameraSourceEncryptionError, InvalidToken):
                # Preserve the existing protected-source diagnostic path.
                pass
            except ValueError:
                return failure_response("saved_connection_invalid")
            lease = session_manager.acquire_camera(camera_id, "setup test", ttl=10)
        capture = None
        diagnostic_code = None
        # Helper imports, opening and first read all consume this one budget.
        # The independent lease still enforces cleanup when the browser leaves.
        deadline = min(lease.expires_at, time.monotonic() + 7.0)
        try:
            capture = _secondary_capture(camera, startup_deadline=deadline,
                                         external_stop_event=lease.stop_event)
        except (CameraSourceEncryptionError, InvalidToken):
            diagnostic_code = "saved_connection_unreadable"
        frame = None
        started = capture is not None and lease.start(capture)
        if started:
            while not lease.closed and time.monotonic() < deadline:
                captured = capture.get_latest(timeout=min(0.25, max(0.0, deadline - time.monotonic())))
                if captured is not None:
                    frame = captured.frame
                    break
                if capture.terminal_failure:
                    break
        # Read only structured acquisition evidence before stop changes its state.
        # Older injected adapters can omit snapshot; no backend strings escape.
        snapshot = capture.snapshot() if callable(getattr(capture, "snapshot", None)) else {}
        cleanup_ok = lease.close()
        import cv2
        encoded = False
        buffer = None
        if frame is not None and cleanup_ok:
            try:
                encoded, buffer = cv2.imencode(".jpg", frame)
                encoded = bool(encoded and buffer is not None and buffer.size)
            except Exception:
                # Native encoder errors may contain private source details.
                encoded = False
        if not cleanup_ok or diagnostic_code is None:
            diagnostic_code = classify_camera_test(snapshot, started=started,
                frame_received=frame is not None, cleanup_ok=cleanup_ok, encoded=encoded)
        ok = diagnostic_code is None
        status = "ok" if ok else "failed"
        diagnostic = None if ok else camera_test_diagnostic(diagnostic_code, camera_type)
        failure_reason = None if ok else f"[{diagnostic['code']}] {diagnostic['title']}. {diagnostic['summary']}"
        saving_result = True
        with managed_transaction(commit=True) as conn:
            updated = conn.execute(
                """
                UPDATE camera_devices
                SET last_test_status = ?, last_test_at = CURRENT_TIMESTAMP,
                    last_health_check_at = CURRENT_TIMESTAMP,
                    last_successful_frame = CASE WHEN ? = 'ok' THEN CURRENT_TIMESTAMP ELSE last_successful_frame END,
                    failure_count = CASE WHEN ? = 'ok' THEN 0 ELSE failure_count + 1 END,
                    last_failure_reason = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND source = ? AND camera_type = ?
                """, (status, status, status, failure_reason, camera_id, camera["source"], camera["camera_type"]),
            )
            if not updated.rowcount:
                return jsonify({"error": "Camera connection changed during the test. Run the test again."}), 409
            log_system_event(conn, "camera_test", session.get("user_id"),
                             {"camera_id": camera_id, "status": status})
            tested_at = conn.execute('SELECT last_test_at FROM camera_devices WHERE id=?', (camera_id,)).fetchone()[0]
        if not ok:
            return failure_response(diagnostic_code, last_test_status=status)
        return jsonify({"message": "Camera test succeeded.", "last_test_status": status,
                        "tested_at": tested_at + ' UTC' if tested_at else None,
                        "frame": base64.b64encode(buffer).decode("utf-8")}), 200
    except CameraBusyError as exc:
        return failure_response("camera_busy", 409)
    except Exception:
        # Native exceptions can contain source credentials; do not log their text.
        logger.error("Camera test could not complete.")
        return failure_response("result_save_failed" if saving_result else "test_error", 500)
    finally:
        if lease is not None and not lease.cleanup_confirmed:
            lease.close()


def _camera_stream_response(camera, user_id, *, registration=False, preview=False):
    """Reserve before responding; release on disconnect, authorization loss or expiry."""
    camera_id = camera["id"]
    duration = 30.0 if preview else 600.0
    permissions = REGISTRATION_CAMERA_PERMISSIONS if registration else ("manage_cameras",)
    access_camera_id = camera_id if registration else None
    # Capture the verified cookie/key before the generator leaves request context.
    # Re-reading only requires_password_change would let an old feed resume once
    # a different client completed the forced reset between stream checks.
    credential_token = session.get("credential_binding")
    signing_key = app.secret_key

    def access_is_current():
        return _camera_stream_access_is_current(
            user_id, permissions, access_camera_id,
            credential_token=credential_token, signing_key=signing_key,
        )

    if not access_is_current():
        return jsonify({"error": "Camera stream access is no longer available."}), 403
    lease = session_manager.acquire_camera(
        camera_id, "registration stream" if registration else "camera preview", ttl=duration,
    )
    if registration:
        lease.preview_user_id = user_id
        lease.preview_token = request.args.get("t")
    try:
        capture = _secondary_capture(camera)
    except Exception:
        lease.close()
        raise

    def generate():
        import cv2
        sequence = 0
        last_access_check = 0.0
        frame_interval = 1.0 / max(1, min(camera["target_fps"], 12))
        try:
            if not lease.start(capture):
                return
            while not lease.closed:
                now = time.monotonic()
                if now - last_access_check >= STREAM_AUTHORIZATION_CHECK_INTERVAL_SECONDS:
                    if not access_is_current():
                        break
                    last_access_check = now
                captured = capture.get_latest(after_sequence=sequence, timeout=0.25)
                if captured is None:
                    if capture.terminal_failure:
                        break
                    continue
                if lease.closed:
                    break
                sequence = captured.sequence
                frame = captured.frame
                height, width = frame.shape[:2]
                if not preview and width > 640:
                    frame = cv2.resize(frame, (640, int(height * 640 / width)), interpolation=cv2.INTER_AREA)
                encoded, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85 if preview else 65])
                # Encoding can outlive lease expiry or stall independently of
                # capture. Recheck immediately before publishing any JPEG.
                if lease.closed:
                    break
                if time.monotonic() - captured.captured_at_monotonic > 3.0:
                    continue
                if encoded:
                    lease.preview_frame_at = captured.captured_at_monotonic
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
                time.sleep(frame_interval)
        finally:
            lease.close()

    response = Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")
    # Also covers responses closed before their generator ever starts.
    response.call_on_close(lease.close)
    return response


@settings_bp.route("/api/cameras/<int:camera_id>/preview", methods=["GET"])
@login_required
@permission_required("manage_cameras")
def api_camera_preview(camera_id):
    return _camera_management_stream(camera_id, preview=True)


@settings_bp.route("/api/cameras/<int:camera_id>/stream", methods=["GET"])
@login_required
@permission_required("manage_cameras")
def api_camera_stream(camera_id):
    return _camera_management_stream(camera_id)


def _camera_management_stream(camera_id, preview=False):
    try:
        with session_manager.lifecycle_guard():
            with managed_transaction(commit=False) as conn:
                camera = conn.execute("SELECT * FROM camera_devices WHERE id = ?", (camera_id,)).fetchone()
            if not camera:
                return jsonify({"error": "Camera not found."}), 404
            return _camera_stream_response(camera, session.get("user_id"), preview=preview)
    except CameraBusyError as exc:
        return _busy_camera_response(exc)
    except Exception:
        logger.error("Camera preview could not start.")
        return abort(500)


@settings_bp.route("/api/registration/cameras/<int:camera_id>/status", methods=["GET"])
@login_required
@permission_required(*REGISTRATION_CAMERA_PERMISSIONS)
def api_registration_camera_status(camera_id):
    """Check the current preview without opening another camera connection."""
    fresh = session_manager.registration_preview_fresh(
        camera_id, session.get("user_id"), request.args.get("t"),
    )
    busy = session_manager.registration_preview_busy(
        camera_id, session.get("user_id"), request.args.get("t"),
    )
    response = jsonify({"fresh": fresh, "busy": busy})
    response.headers["Cache-Control"] = "no-store"
    return response


@settings_bp.route("/api/registration/cameras/<int:camera_id>/stream", methods=["GET"])
@login_required
@permission_required(*REGISTRATION_CAMERA_PERMISSIONS)
def api_registration_camera_stream(camera_id):
    """Serve an exclusively owned active camera for biometric registration."""
    try:
        with session_manager.lifecycle_guard():
            with managed_transaction(commit=False) as conn:
                camera = conn.execute(
                    "SELECT * FROM camera_devices WHERE id = ? AND is_active = 1 AND lifecycle_status = 'Ready'",
                    (camera_id,),
                ).fetchone()
            if not camera:
                return jsonify({"error": "Ready registration camera not found."}), 404
            return _camera_stream_response(camera, session.get("user_id"), registration=True)
    except CameraBusyError as exc:
        return _busy_camera_response(exc)
    except Exception:
        logger.error("Registration camera stream could not start.")
        return abort(500)


# ---------------------------------------------------------------------------
# Academic Programs Management
# ---------------------------------------------------------------------------

def _authorize_program_write(conn, *, html=False):
    """Recheck the operator after obtaining the program write lock."""
    actor = authenticated_session_user(conn, session)
    if not actor or actor["requires_password_change"]:
        session.clear()
        if html:
            flash("Your login session expired. Sign in again before changing programs.", "error")
            return redirect(url_for("auth.login"))
        return jsonify({"error": "Your login session expired.", "session_expired": True,
                        "login_url": url_for("auth.login")}), 401
    if not has_permission(actor["role"], "manage_programs"):
        if html:
            flash("You no longer have permission to manage academic programs.", "error")
            return redirect(url_for("dashboard_routes.dashboard"))
        return jsonify({"error": "Forbidden"}), 403
    return None


def _render_program_management(*, form_name="", form_error=None, status=200):
    with managed_transaction(commit=False) as conn:
        programs = conn.execute(
            """
            SELECT ap.*, COUNT(s.id) AS student_count,
                   (SELECT COUNT(*) FROM pending_student_registrations pr
                    WHERE pr.program_id = ap.id) AS request_count
            FROM academic_programs ap
            LEFT JOIN students s ON s.program_id = ap.id
            GROUP BY ap.id
            ORDER BY ap.name ASC
            """
        ).fetchall()
    return render_template("settings/programs.html", programs=programs,
                           form_name=form_name, form_error=form_error), status


def _validated_program_name(value):
    if not isinstance(value, str):
        return None, "Program name must be text."
    name = " ".join(value.split())
    if not name:
        return None, "Program name is required."
    if len(name) > 120:
        return None, "Use 120 characters or fewer for the program name."
    return name, None


def _program_name_exists(conn, name, exclude_id=None):
    # Preserve existing names while detecting case/spacing variants consistently.
    # All callers reserve the write transaction before this check.
    normalized = name.casefold()
    return any(row["id"] != exclude_id and " ".join(row["name"].split()).casefold() == normalized
               for row in conn.execute("SELECT id, name FROM academic_programs"))


@settings_bp.route("/settings/programs", methods=["GET", "POST"])
@login_required
@permission_required("manage_programs")
def manage_programs():
    """Render the Academic Programs management UI."""
    if request.method == "POST":
        return api_add_program()
    return _render_program_management()


@settings_bp.route("/api/programs", methods=["POST"])
@login_required
@permission_required("manage_programs")
def api_add_program():
    """API endpoint to add a new academic program."""
    is_json = request.is_json
    if is_json:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"error": "Send the program name in a JSON object."}), 400
        raw_name = data.get("name", "")
    else:
        raw_name = request.form.get("name", "")
    name, error = _validated_program_name(raw_name)

    def reject(message, status=400):
        if is_json:
            return jsonify({"error": message}), status
        return _render_program_management(form_name=raw_name, form_error=message, status=status)

    if error:
        return reject(error)

    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            denied = _authorize_program_write(conn, html=not is_json)
            if denied is not None:
                return denied
            if _program_name_exists(conn, name):
                return reject("Program already exists.")
            conn.execute("INSERT INTO academic_programs (name, is_active) VALUES (?, 1)", (name,))
    except sqlite3.IntegrityError:
        return reject("Program already exists.")
    except Exception:
        logger.exception("Failed to add program.")
        return reject("The program could not be saved. Try again.", 500)
    if is_json:
        return jsonify({"message": f"Program '{name}' added successfully.", "name": name}), 201
    flash(f"Program '{name}' added successfully.", "success")
    return redirect(url_for("manage_programs"))


@settings_bp.route("/api/programs/<int:program_id>", methods=["PUT"])
@login_required
@permission_required("manage_programs")
def api_update_program(program_id):
    """API endpoint to rename an academic program."""
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"error": "Send the program name in a JSON object."}), 400
    name, error = _validated_program_name(data.get("name", ""))
    if error:
        return jsonify({"error": error}), 400
    if not 0 < program_id <= 9223372036854775807:
        return jsonify({"error": "Program not found."}), 404

    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            denied = _authorize_program_write(conn)
            if denied is not None:
                return denied
            prog = conn.execute("SELECT id FROM academic_programs WHERE id = ?", (program_id,)).fetchone()
            if not prog:
                return jsonify({"error": "Program not found."}), 404

            if _program_name_exists(conn, name, exclude_id=program_id):
                return jsonify({"error": "Program already exists."}), 400
            conn.execute("UPDATE academic_programs SET name = ? WHERE id = ?", (name, program_id))
            return jsonify({"message": "Program updated.", "id": program_id, "name": name}), 200
    except sqlite3.IntegrityError:
        return jsonify({"error": "Program already exists."}), 400
    except Exception:
        logger.exception("Failed to update program.")
        return jsonify({"error": "Internal error."}), 500
@settings_bp.route("/api/programs/<int:program_id>", methods=["DELETE"])
@login_required
@permission_required("manage_programs")
def api_delete_program(program_id):
    """API endpoint to delete an unused academic program."""
    if not 0 < program_id <= 9223372036854775807:
        return jsonify({"error": "Program not found."}), 404
    try:
        with managed_transaction(commit=True) as conn:
            # Serialize dependency checks with deletion, including new requests.
            conn.execute("BEGIN IMMEDIATE")
            denied = _authorize_program_write(conn)
            if denied is not None:
                return denied
            prog = conn.execute("SELECT name FROM academic_programs WHERE id = ?", (program_id,)).fetchone()
            if not prog:
                return jsonify({"error": "Program not found."}), 404

            student_count = conn.execute(
                "SELECT COUNT(*) FROM students WHERE program_id = ?",
                (program_id,),
            ).fetchone()[0]
            if student_count:
                return jsonify({
                    "error": "Cannot delete a program assigned to students. Deactivate it instead."
                }), 400

            request_count = conn.execute(
                "SELECT COUNT(*) FROM pending_student_registrations WHERE program_id = ?",
                (program_id,),
            ).fetchone()[0]
            if request_count:
                return jsonify({
                    "error": "Cannot delete a program referenced by registration requests. Deactivate it instead."
                }), 409

            conn.execute("DELETE FROM academic_programs WHERE id = ?", (program_id,))
            return jsonify({"message": f"Program '{prog['name']}' deleted."}), 200
    except sqlite3.IntegrityError:
        return jsonify({"error": "This program is still in use. Deactivate it instead."}), 409
    except Exception:
        logger.exception("Failed to delete program.")
        return jsonify({"error": "Internal error."}), 500
@settings_bp.route("/api/programs/<int:program_id>/toggle", methods=["POST"])
@login_required
@permission_required("manage_programs")
def api_toggle_program(program_id):
    """Toggle program availability without rewriting existing assignments."""
    desired_status = None
    if request.is_json or request.get_data():
        data = request.get_json()
        if not isinstance(data, dict) or type(data.get("is_active")) is not bool:
            return jsonify({"error": "Choose whether the program should be active using true or false."}), 400
        desired_status = int(data["is_active"])
    if not 0 < program_id <= 9223372036854775807:
        return jsonify({"error": "Program not found."}), 404
    try:
        with managed_transaction(commit=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            denied = _authorize_program_write(conn)
            if denied is not None:
                return denied
            prog = conn.execute(
                "SELECT name, is_active FROM academic_programs WHERE id = ?", (program_id,)
            ).fetchone()
            if not prog:
                return jsonify({"error": "Program not found"}), 404

            new_status = desired_status if desired_status is not None else (0 if prog["is_active"] else 1)
            changed = new_status != prog["is_active"]
            if changed:
                conn.execute("UPDATE academic_programs SET is_active = ? WHERE id = ?", (new_status, program_id))
            affected_students = conn.execute(
                "SELECT COUNT(*) FROM students WHERE program_id = ? AND permanently_removed_at IS NULL",
                (program_id,),
            ).fetchone()[0]
            if changed:
                log_system_event(
                    conn,
                    "academic_program_status_changed",
                    actor_user_id=session.get("user_id"),
                    details={
                        "program_id": program_id,
                        "is_active": bool(new_status),
                        "retained_student_assignments": affected_students,
                    },
                )
            message = "Program activated."
            if not new_status:
                message = (
                    f"Program deactivated. {affected_students} existing student assignment(s) "
                    "were retained and remain editable without reassignment."
                )
            return jsonify({
                "message": message,
                "is_active": new_status,
                "retained_student_assignments": affected_students,
            }), 200
    except Exception as e:
        logger.exception("Failed to toggle program status.")
        return jsonify({"error": "Internal error"}), 500
