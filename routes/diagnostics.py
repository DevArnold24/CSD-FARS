import os
import psutil
import json
import math
import stat
import time
from pathlib import Path
from flask import flash, redirect, render_template, jsonify, request, Response, send_from_directory, session, url_for, current_app
from flask_wtf.csrf import generate_csrf

from core import get_logger
from security.rbac import current_user_has_permission, has_permission, permission_required
from security.session_auth import credential_matches
from core.utils import login_required, _camera_coverage_summary
from database.db_manager import get_db_connection, managed_transaction
from workers.session_manager import session_manager
from routes.blueprints import diagnostics_bp
from core.streaming import generate_mjpeg_stream
from services.recognition_settings import load_settings, normal_match_threshold, save_settings, validate_settings
from services import backup
from services.audit import log_system_event
from services.technical_history import prepare_technical_events, history_event_types, load_technical_event_page
from services.camera_health import camera_health_summary
from services.system_capacity import system_capacity_snapshot
import config

logger = get_logger("diagnostics")


@diagnostics_bp.route("/api/diagnostics/stream-capacity")
@login_required
@permission_required("view_diagnostics")
def stream_capacity_status():
    """Expose counters, never camera sources, for deployment load acceptance."""
    from services.stream_capacity import stream_capacity_snapshot
    return jsonify(stream_capacity_snapshot())


def _diagnostic_stream_access_check(user_id):
    """Return a fail-closed authorization check for a long-lived sandbox feed."""
    binding = session.get("credential_binding")
    signing_key = current_app.secret_key

    def access_is_current():
        try:
            with managed_transaction(commit=False) as conn:
                authorization = conn.execute(
                    "SELECT role, password_hash, is_active, requires_password_change FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()
        except Exception:
            logger.exception("Failed to revalidate diagnostic stream authorization.")
            return False
        return bool(
            authorization
            and credential_matches(user_id, authorization["password_hash"], binding, secret_key=signing_key)
            and authorization["is_active"]
            and not authorization["requires_password_change"]
            and has_permission(authorization["role"], "run_recognition_diagnostics")
            and session_manager.is_sandbox_running()
        )

    return access_is_current

@diagnostics_bp.route("/diagnostics")
@login_required
@permission_required("view_diagnostics")
def diagnostics_index():
    """Render the System Diagnostics dashboard."""
    return render_template("diagnostics/index.html")


def _safe_backup_path(name):
    if not name or Path(name).name != name or not name.endswith(".enc"):
        return None
    try:
        root = Path(backup.BACKUP_DIR).resolve()
        candidate = (root / name).resolve()
        if candidate.parent != root or not candidate.is_file():
            return None
    except (OSError, ValueError, RuntimeError):
        return None
    return candidate


def _backup_inventory():
    """An unavailable archive directory must not hide technical history."""
    archives = []
    error = None
    unavailable = "Backup list could not be loaded completely. Refresh or ask the server operator to check backup storage."
    try:
        for path in Path(backup.BACKUP_DIR).iterdir():
            if not path.name.endswith(".enc"):
                continue
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                # A separately removed archive is simply absent from this view.
                continue
            except OSError:
                error = unavailable
                continue
            if (not stat.S_ISREG(metadata.st_mode)
                    or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                continue
            archives.append({"name": path.name, "size": metadata.st_size, "modified": metadata.st_mtime})
    except FileNotFoundError:
        pass  # No backup directory is expected before the first backup.
    except OSError:
        error = unavailable
    archives.sort(key=lambda item: (item["modified"], item["name"]), reverse=True)
    return archives, error


@diagnostics_bp.route("/system/operations")
@login_required
@permission_required("view_technical_history", "manage_backups")
def system_operations():
    can_manage_backups = current_user_has_permission("manage_backups")
    it_view = not can_manage_backups
    event_type = request.args.get("event_type", "")
    history_error = None
    newer = older = None
    with managed_transaction(commit=False) as conn:
        try:
            event_rows, newer, older = load_technical_event_page(
                conn, event_type=event_type, before=request.args.get("before"),
                after=request.args.get("after"), it_view=it_view,
            )
        except ValueError as error:
            event_rows = []
            history_error = str(error)
    events = prepare_technical_events(
        event_rows,
        config.INSTITUTION_UTC_OFFSET_MINUTES,
        it_view=it_view,
    )
    archives = []
    backup_error = None
    if can_manage_backups:
        archives, backup_error = _backup_inventory()
    return render_template(
        "diagnostics/operations.html",
        events=events,
        archives=archives,
        backup_error=backup_error,
        can_manage_backups=can_manage_backups,
        event_types=history_event_types(it_view), event_type=event_type,
        history_error=history_error, history_newer=newer, history_older=older,
        history_filtered=bool(event_type or request.args.get("before") or request.args.get("after")),
    ), 400 if history_error else 200


def _record_backup_event(action, archive_path):
    """History is secondary to the completed archive operation."""
    try:
        with managed_transaction(commit=True) as conn:
            log_system_event(conn, action, session.get("user_id"), {
                "archive_file": Path(archive_path).name,
            })
    except Exception:
        logger.exception("Backup operation completed, but its history entry could not be recorded.")
        return False
    return True


@diagnostics_bp.route("/system/backups", methods=["POST"])
@login_required
@permission_required("manage_backups")
def create_system_backup():
    try:
        archive_path = backup.create_sqlite_backup("manual")
    except Exception:
        logger.exception("Failed to create encrypted system backup.")
        flash("Backup creation failed. Check backup encryption configuration and system logs.", "error")
    else:
        if not _record_backup_event("backup_created", archive_path):
            flash("The backup was created, but its history entry could not be recorded. You can verify or download the archive below.", "warning")
        flash("Encrypted backup created and integrity snapshot completed.", "success")
    return redirect(url_for("diagnostics.system_operations"), code=303)


@diagnostics_bp.route("/system/backups/<name>/verify", methods=["POST"])
@login_required
@permission_required("manage_backups")
def verify_system_backup(name):
    archive_path = _safe_backup_path(name)
    if archive_path is None:
        return jsonify({"error": "Backup not found."}), 404
    verified = backup.verify_sqlite_backup(str(archive_path))
    recorded = _record_backup_event(
        "backup_verified" if verified else "backup_verification_failed", archive_path,
    )
    warning = {} if recorded else {
        "warning": "The verification history entry could not be recorded.",
    }
    if not verified:
        return jsonify({"error": "Backup failed integrity or authentication verification.", **warning}), 409
    return jsonify({"message": "Backup integrity and authentication verified.", **warning})


@diagnostics_bp.route("/system/backups/<name>/download")
@login_required
@permission_required("manage_backups")
def download_system_backup(name):
    archive_path = _safe_backup_path(name)
    if archive_path is None:
        return jsonify({"error": "Backup not found."}), 404
    return send_from_directory(backup.BACKUP_DIR, archive_path.name, as_attachment=True)


@diagnostics_bp.route("/api/diagnostics/stats")
@login_required
@permission_required("view_diagnostics")
def api_diagnostics_stats():
    """Return live health metrics without exposing raw application logs."""
    cpu_percent = psutil.cpu_percent(interval=0.1)
    ram_percent = psutil.virtual_memory().percent
    
    # Raw log lines can contain credentials, student identifiers, or internal
    # network details. Authorized operators use the allowlisted/redacted
    # Technical History instead.
    logs = []
        
    fps = 0.0
    resolution = "N/A"
    first_session = session_manager.first_session_context()
    if first_session:
        fps = first_session.get("fps", 0.0)
        resolution = first_session.get("resolution", "N/A")
        camera_name = first_session.get("camera_name", "N/A")
        camera_room = first_session.get("camera_room", "N/A")
        pipeline_latency = first_session.get("pipeline_latency", {})
        last_match_confidence = first_session.get("last_match_confidence")
        published_capture_at = first_session.get("published_capture_timestamp_monotonic")
        try:
            frame_age_ms = (time.monotonic() - float(published_capture_at)) * 1000.0
            current_frame_age_ms = round(frame_age_ms, 2) if math.isfinite(frame_age_ms) and frame_age_ms >= 0 else None
        except (TypeError, ValueError):
            current_frame_age_ms = None
        camera_freshness = {
            **session_manager.get_preview_stats(context=first_session),
            "capture_fps": first_session.get("capture_fps", 0.0),
            "recognition_fps": first_session.get("fps", 0.0),
            "publication_fps": first_session.get("publication_fps", 0.0),
            "capture_sequence": first_session.get("capture_sequence", 0),
            "capture_timestamp_monotonic": first_session.get("capture_timestamp_monotonic"),
            "processed_sequence": first_session.get("processed_sequence", 0),
            "publication_sequence": first_session.get("publication_sequence", 0),
            "publication_timestamp_monotonic": first_session.get("publication_timestamp_monotonic"),
            "skipped_frames": first_session.get("skipped_frames", 0),
            "current_frame_age_ms": current_frame_age_ms,
            "read_failures": first_session.get("read_failures", 0),
            "capture_state": first_session.get("capture_state", "unknown"),
            "open_failures": first_session.get("open_failures", 0),
            "reconnect_attempts": first_session.get("reconnect_attempts", 0),
            "consecutive_failures": first_session.get("consecutive_failures", 0),
            "last_failure": first_session.get("last_failure"),
        }
    else:
        camera_name = "N/A"
        camera_room = "N/A"
        pipeline_latency = {}
        last_match_confidence = None
        camera_freshness = {}
        
    with managed_transaction(commit=False) as conn:
        camera_coverage = _camera_coverage_summary(conn)
    health = camera_health_summary()
    counts = health.get("counts") or {}
    camera_coverage.update(
        healthy_cameras=counts.get("healthy"), failed_cameras=counts.get("attention"),
        stale_cameras=counts.get("stale"), not_recently_tested=counts.get("not_checked"),
    )
    response = jsonify({
        "cpu": cpu_percent,
        "ram": ram_percent,
        "capacity": system_capacity_snapshot(cpu_percent, ram_percent),
        "logs": logs,
        "logs_restricted": True,
        "camera_active": len(session_manager.active_sessions) > 0,
        "camera_health": health,
        "fps": fps,
        "resolution": resolution,
        "camera_name": camera_name,
        "camera_room": camera_room,
        "worker_mode": first_session.get("worker_mode") if first_session else "N/A",
        "pipeline_latency": pipeline_latency,
        "camera_freshness": camera_freshness,
        "last_match_confidence": last_match_confidence,
        "camera_coverage": camera_coverage,
    })
    response.headers["Cache-Control"] = "no-store"
    return response, 200


# ---------------------------------------------------------------------------
# Recognition Sandbox Endpoints (Milestone 2)
# ---------------------------------------------------------------------------

def _normal_camera_match_limits(cameras, settings):
    limits = {}
    for camera in cameras:
        try:
            threshold = normal_match_threshold(settings["MATCH_THRESHOLD"], camera["match_threshold"])
        except ValueError:
            threshold = None
        limits[str(camera["id"])] = threshold
    return limits


@diagnostics_bp.route("/diagnostics/recognition-sandbox")
@login_required
@permission_required("run_recognition_diagnostics")
def recognition_sandbox_dashboard():
    """Render the Recognition Sandbox diagnostic dashboard."""
    active_cameras = []
    try:
        with managed_transaction(commit=False) as conn:
            rows = conn.execute(
                "SELECT id, name, room_id, resolution_width, resolution_height, target_fps, match_threshold FROM camera_devices WHERE is_active = 1 AND lifecycle_status != 'Retired' ORDER BY name"
            ).fetchall()
            active_cameras = [dict(r) for r in rows]
    except Exception as e:
        logger.error("Error fetching active cameras for sandbox dashboard: %s", e)

    config_dict = load_settings()
    camera_match_limits = _normal_camera_match_limits(active_cameras, config_dict)
    for camera in active_cameras:
        camera["normal_match_threshold"] = camera_match_limits[str(camera["id"])]
    with session_manager.lifecycle_guard():
        sandbox_context = session_manager.get_sandbox_context()
        sandbox_running = sandbox_context is not None
        sandbox_camera_id = sandbox_context.get("camera_id") if sandbox_context else None

    return render_template(
        "diagnostics/sandbox.html",
        active_cameras=active_cameras,
        is_sandbox_running=sandbox_running,
        sandbox_camera_id=sandbox_camera_id,
        face_detection_height=max(1, int(getattr(config, "FACE_DETECTION_HEIGHT", 480))),
        config=config_dict
    )


@diagnostics_bp.route("/api/diagnostics/sandbox/start", methods=["POST"])
@login_required
@permission_required("run_recognition_diagnostics")
def api_sandbox_start():
    """Start the sandbox worker loop with optional camera_id and overrides."""
    raw_body = request.get_data(cache=True)
    if not raw_body.strip():
        data = {}
    elif not request.is_json:
        return jsonify({"success": False, "message": "JSON content is required."}), 415
    else:
        data = request.get_json(silent=True)
    if data is None or not isinstance(data, dict):
        return jsonify({"success": False, "message": "A JSON object is required."}), 400
    camera_id_supplied = "camera_id" in data
    raw_camera_id = data.get("camera_id")
    try:
        overrides, _normal_mode = _parse_tuner_action(data.get("overrides", {}))
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    if camera_id_supplied:
        camera_id_text = str(raw_camera_id)
        if (
            isinstance(raw_camera_id, bool)
            or not camera_id_text.isascii()
            or not camera_id_text.isdigit()
        ):
            return jsonify({"success": False, "message": "camera_id must be a positive integer."}), 400
        camera_id = int(camera_id_text)
        if camera_id <= 0:
            return jsonify({"success": False, "message": "camera_id must be a positive integer."}), 400
    else:
        with managed_transaction(commit=False) as conn:
            default_cam = conn.execute("SELECT id FROM camera_devices WHERE is_active = 1 AND lifecycle_status != 'Retired' LIMIT 1").fetchone()
        if not default_cam:
            return jsonify({"success": False, "message": "No active camera is available for diagnostics."}), 409
        camera_id = default_cam["id"]

    with managed_transaction(commit=False) as conn:
        camera = conn.execute(
            "SELECT id, is_active, match_threshold FROM camera_devices WHERE id = ?",
            (camera_id,),
        ).fetchone()
    if not camera:
        return jsonify({"success": False, "message": "Camera not found."}), 404
    if not camera["is_active"]:
        return jsonify({"success": False, "message": "Inactive cameras cannot run diagnostics."}), 409

    # Use the same lifecycle lock as live-session start so the ownership check
    # and sandbox registration are one atomic operation.  Without this guard,
    # simultaneous live and sandbox requests could both observe a free camera.
    with session_manager.lifecycle_guard():
        # A camera can be retired while this request waits for the lifecycle
        # lock. Re-read admission state before registering the tuner owner.
        with managed_transaction(commit=False) as conn:
            camera = conn.execute("SELECT * FROM camera_devices WHERE id=?", (camera_id,)).fetchone()
        if not camera or not camera['is_active'] or camera['lifecycle_status'] == 'Retired':
            return jsonify({"success": False, "message": "Camera is inactive or retired. Restore it in Camera inventory before starting the tuner."}), 409
        existing_sandbox = session_manager.get_sandbox_context()
        if existing_sandbox and str(existing_sandbox.get("camera_id")) != str(camera_id):
            return jsonify({"success": False,
                            "message": "The tuner is already using another camera. Stop it before switching cameras."}), 409
        camera_owner = session_manager.get_camera_owner(camera_id)
        if camera_owner is not None and camera_owner != "sandbox":
            return jsonify({
                "success": False,
                "message": (f"Camera (ID: {camera_id}) is currently in use by an active live class session (Class ID: {camera_owner}). The recognition tuner cannot be started."
                            if isinstance(camera_owner, int) else
                            "Camera is currently in use. Stop its camera preview, then retry the tuner.")
            }), 400

        if existing_sandbox:
            # Start is idempotent. Changes to a running test belong to the
            # explicit update action, not a repeated Start or a new default.
            context, created = existing_sandbox, False
        else:
            saved = load_settings()
            if "MATCH_THRESHOLD" not in overrides:
                try:
                    saved["MATCH_THRESHOLD"] = normal_match_threshold(
                        saved["MATCH_THRESHOLD"], camera["match_threshold"]
                    )
                except ValueError as exc:
                    return jsonify({"success": False, "message": str(exc)}), 409
            saved.update(overrides)
            context, created = session_manager.start_sandbox(camera_id=camera_id, overrides=saved)
    return jsonify({
        "success": True,
        "created": bool(created),
        "message": "Recognition tuner started." if created else "The recognition tuner is already running.",
        "session": {
            "camera_id": context.get("camera_id", camera_id),
            "started_at": context.get("started_at"),
            "overrides": context.get("overrides", {}),
            "worker_mode": context.get("worker_mode", "process"),
        }
    }), 200


@diagnostics_bp.route("/api/diagnostics/sandbox/stop", methods=["POST"])
@login_required
@permission_required("run_recognition_diagnostics")
def api_sandbox_stop():
    """Stop the sandbox worker loop."""
    stopped = session_manager.stop_sandbox()
    if not stopped:
        return jsonify({"success": False, "stopped": False,
                        "message": "The tuner is still stopping. Its camera remains reserved until cleanup finishes; retry shortly."}), 409
    return jsonify({
        "success": True,
        "message": "Recognition tuner stopped." if stopped else "No recognition tuner is running.",
        "stopped": stopped,
    }), 200


@diagnostics_bp.route("/api/diagnostics/sandbox/feed")
@login_required
@permission_required("run_recognition_diagnostics")
def api_sandbox_feed():
    """Stream MJPEG video output for sandbox session."""
    user_id = session.get("user_id")
    if not session_manager.is_sandbox_running():
        return jsonify({"error": "No recognition tuner is running."}), 409
    return Response(
        generate_mjpeg_stream(
            "sandbox",
            access_check=_diagnostic_stream_access_check(user_id),
            **({"view": "live"} if request.args.get("view") == "live" else {}),
        ),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )


@diagnostics_bp.route("/api/diagnostics/sandbox/stats")
@login_required
@permission_required("run_recognition_diagnostics")
def api_sandbox_stats():
    """Return JSON containing latency breakdowns and match confidence metrics."""
    from services.stream_capacity import stream_capacity_snapshot

    with session_manager.lifecycle_guard():
        stats = session_manager.get_sandbox_stats()
        sandbox_context = session_manager.get_sandbox_context() if stats.get("active") else None
        camera_id = sandbox_context.get("camera_id") if sandbox_context else None
    response = jsonify({
        "success": True,
        "camera_id": camera_id,
        "csrf_token": generate_csrf(),
        "video_capacity": stream_capacity_snapshot(),
        "stats": stats,
        **{key: stats.get(key) for key in (
            "preview_publication_sequence", "preview_current_frame_age_ms", "preview_fps", "preview_resolution", "preview_state",
            "detect_ms", "encode_face_ms", "loop_ms", "rolling_liveness_ms", "match_ms",
            "tracker_init_ms", "tracker_update_ms",
        )},
        "active": stats.get("active", False),
        "detection_ms": stats.get("detection_ms", 0.0),
        "liveness_ms": stats.get("liveness_ms", 0.0),
        "embedding_ms": stats.get("embedding_ms", 0.0),
        "matching_ms": stats.get("matching_ms", 0.0),
        "total_ms": stats.get("total_ms", 0.0),
        "fps": stats.get("fps", 0.0),
        "capture_fps": stats.get("capture_fps", 0.0),
        "publication_fps": stats.get("publication_fps", 0.0),
        "capture_sequence": stats.get("capture_sequence", 0),
        "capture_timestamp_monotonic": stats.get("capture_timestamp_monotonic"),
        "processed_sequence": stats.get("processed_sequence", 0),
        "publication_sequence": stats.get("publication_sequence", 0),
        "publication_timestamp_monotonic": stats.get("publication_timestamp_monotonic"),
        "skipped_frames": stats.get("skipped_frames", 0),
        "current_frame_age_ms": stats.get("current_frame_age_ms", 0.0),
        "read_failures": stats.get("read_failures", 0),
        "capture_state": stats.get("capture_state", "unknown"),
        "open_failures": stats.get("open_failures", 0),
        "reconnect_attempts": stats.get("reconnect_attempts", 0),
        "consecutive_failures": stats.get("consecutive_failures", 0),
        "last_failure": stats.get("last_failure"),
        "best_distance": stats.get("best_distance"),
        "second_distance": stats.get("second_distance"),
        "margin": stats.get("margin"),
        "ratio": stats.get("ratio"),
        "match_info": stats.get("match_info"),
        "match_history": stats.get("match_history", []),
    })
    response.headers["Cache-Control"] = "no-store"
    return response, 200


def _parse_tuner_action(data):
    if not isinstance(data, dict):
        raise ValueError("Recognition settings must be an object.")
    values = dict(data)
    normal_mode = values.pop("use_normal_match_limit", False)
    if not isinstance(normal_mode, bool):
        raise ValueError("use_normal_match_limit must be true or false.")
    if normal_mode and "MATCH_THRESHOLD" in values:
        raise ValueError("Choose the normal camera limit or a test override, not both.")
    return validate_settings(values), normal_mode


def _normal_tuner_limit(conn, camera_id, saved):
    camera = conn.execute("SELECT match_threshold FROM camera_devices WHERE id=? AND is_active=1",
                          (camera_id,)).fetchone()
    if camera is None:
        raise ValueError("The running session's camera is unavailable. Stop it and select an active camera.")
    return normal_match_threshold(saved["MATCH_THRESHOLD"], camera["match_threshold"])


def _validate_and_parse_sandbox_params(data: dict):
    """
    Validates and type-converts incoming sandbox parameter overrides/updates.
    Returns (parsed_dict, error_message).
    """
    try:
        return validate_settings(data), None
    except ValueError as exc:
        return None, str(exc)


@diagnostics_bp.route("/api/diagnostics/sandbox/update_overrides", methods=["POST"])
@login_required
@permission_required("run_recognition_diagnostics")
def api_sandbox_update_overrides():
    """Accept slider/toggle values and push them directly into active worker context overrides."""
    data = request.get_json(silent=True) or {}
    try:
        overrides, normal_mode = _parse_tuner_action(data)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    with session_manager.lifecycle_guard():
        if normal_mode:
            context = session_manager.get_sandbox_context()
            if context is None:
                return jsonify({"success": False, "message": "No recognition tuner is running. Start it before changing settings."}), 400
            try:
                with managed_transaction(commit=False) as conn:
                    overrides["MATCH_THRESHOLD"] = _normal_tuner_limit(conn, context.get("camera_id"), load_settings(conn))
            except ValueError as exc:
                return jsonify({"success": False, "message": str(exc)}), 409
        updated = session_manager.update_sandbox_overrides(overrides)
    if updated is None:
        return jsonify({
            "success": False,
            "message": "No recognition tuner is running. Start it before changing settings."
        }), 400

    return jsonify({
        "success": True,
        "message": "Tuner settings updated.",
        "overrides": updated,
    }), 200


@diagnostics_bp.route("/api/diagnostics/sandbox/save_config", methods=["POST"])
@login_required
@permission_required("manage_recognition_settings")
def api_sandbox_save_config():
    """Persist validated recognition settings in the application database."""
    data = request.get_json(silent=True) or {}
    typed_updates, err_msg = _validate_and_parse_sandbox_params(data)
    if err_msg:
        return jsonify({
            "success": False,
            "message": err_msg
        }), 400

    if not typed_updates:
        return jsonify({
            "success": False,
            "message": "No valid config parameters provided."
        }), 400

    try:
        saved = save_settings(typed_updates, session.get("user_id"))
    except Exception:
        logger.exception("Failed to persist recognition settings.")
        return jsonify({"success": False, "message": "Failed to persist recognition settings."}), 500

    try:
        with managed_transaction(commit=False) as conn:
            cameras = conn.execute(
                "SELECT id, match_threshold FROM camera_devices WHERE is_active = 1"
            ).fetchall()
        camera_match_limits = _normal_camera_match_limits(cameras, saved)
        camera_match_limits_message = (
            "Some cameras have invalid recognition limits. Correct their camera or system settings before starting attendance."
            if any(value is None for value in camera_match_limits.values()) else ""
        )
    except Exception:
        # Persistence already committed. Report unavailable metadata separately
        # so the browser does not keep displaying limits from before this save.
        logger.exception("Recognition defaults saved, but camera limits could not be refreshed.")
        camera_match_limits = None
        camera_match_limits_message = (
            "Recognition defaults were saved, but camera limits could not be refreshed. "
            "Reload this page before checking normal attendance settings."
        )
    return jsonify({
        "success": True,
        "message": "Recognition defaults saved. New attendance sessions also use any stricter camera limit.",
        "config": saved,
        "camera_match_limits": camera_match_limits,
        "camera_match_limits_message": camera_match_limits_message,
    }), 200


@diagnostics_bp.route("/api/diagnostics/sandbox/apply_to_live", methods=["POST"])
@login_required
@permission_required("manage_recognition_settings")
def api_sandbox_apply_to_live():
    """Apply technical sandbox overrides to active live attendance sessions."""
    data = request.get_json(silent=True) or {}
    try:
        overrides, normal_mode = _parse_tuner_action(data)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    applied_count = 0

    with session_manager.lifecycle_guard():
        planned = []
        try:
            with managed_transaction(commit=False) as conn:
                saved = load_settings(conn)
                for class_id, ctx in list(session_manager.active_sessions.items()):
                    if class_id == "sandbox":
                        continue
                    values = dict(overrides)
                    if normal_mode:
                        values["MATCH_THRESHOLD"] = _normal_tuner_limit(conn, ctx.get("camera_id"), saved)
                    planned.append((class_id, values))
        except ValueError as exc:
            # Validate every camera before changing any running session.
            return jsonify({"success": False, "message": str(exc)}), 409
        for class_id, values in planned:
            if session_manager.update_live_session_overrides(class_id, values) is not None:
                applied_count += 1

    msg = "No matching active sessions were updated."
    if applied_count > 0:
        msg = f"Applied settings to {applied_count} active live session(s)."
    msg += " New attendance sessions use saved defaults and any stricter camera limit."

    return jsonify({
        "success": True,
        "message": msg,
    }), 200
