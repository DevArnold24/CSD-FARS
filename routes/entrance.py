"""Occurrence-bound Attendance Display routes for local CSD LAN monitors."""

from flask import Response, jsonify, make_response, render_template, url_for

from core.streaming import generate_mjpeg_stream
from database.db_manager import managed_transaction
from routes.blueprints import entrance_bp
from services.attendance_display import build_display_payload, resolve_display_grant, display_capture_allowed


@entrance_bp.route("/display/session/<grant_token>", methods=["GET"])
def attendance_display(grant_token):
    """Render a display whose access is bound to one exact attendance occurrence."""
    with managed_transaction(commit=True) as conn:
        occurrence = resolve_display_grant(conn, grant_token)
    response = make_response(render_template(
        "display/entrance.html",
        grant_token=grant_token,
        display_title=(occurrence["room_name"] if occurrence else "Attendance Display"),
        status_url=url_for(
            "entrance.api_attendance_display_status",
            grant_token=grant_token,
        ),
    ))
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@entrance_bp.route("/api/display/session/<grant_token>/status", methods=["GET"])
def api_attendance_display_status(grant_token):
    with managed_transaction(commit=True) as conn:
        payload = build_display_payload(conn, grant_token)
    if payload.get("has_camera"):
        from services.stream_capacity import viewing_availability
        from routes.classes import _session_publication_status
        sequence, age = _session_publication_status(payload['class_id'])
        payload.update(viewing_capacity=viewing_availability(), publication_sequence=sequence, current_frame_age_ms=age)
        payload["video_url"] = url_for(
            "entrance.api_attendance_display_video",
            grant_token=grant_token,
        )
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    return response


@entrance_bp.route("/api/display/session/<grant_token>/video", methods=["GET"])
def api_attendance_display_video(grant_token):
    with managed_transaction(commit=True) as conn:
        occurrence = resolve_display_grant(conn, grant_token)
        capture_allowed = display_capture_allowed(conn, occurrence)
    if not capture_allowed:
        return Response("Attendance Display is inactive.", status=404)
    if not occurrence["camera_id"]:
        return Response("No active camera is assigned to this attendance occurrence.", status=404)
    attendance_session_id = occurrence["attendance_session_id"]
    class_id = occurrence["class_id"]

    def grant_is_active():
        with managed_transaction(commit=True) as conn:
            current = resolve_display_grant(conn, grant_token)
            capture_allowed = display_capture_allowed(conn, current)
        return bool(
            capture_allowed and
            current
            and current["attendance_session_id"] == attendance_session_id
            and current["class_id"] == class_id
        )

    response = Response(
        generate_mjpeg_stream(class_id, access_check=grant_is_active),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    return response


# Compatibility-only surfaces. They intentionally expose no authorization
# ceremony, active session metadata, roster, or video. Existing bookmarks fail
# safely at Standby while Teachers use per-occurrence links from the session UI.
@entrance_bp.route("/display/room/<room_code>", methods=["GET"])
def entrance_display(room_code):
    return render_template(
        "display/entrance.html",
        grant_token="",
        display_title=room_code,
        status_url="",
    )


@entrance_bp.route("/display/room/<room_code>/authorize", methods=["POST"])
def authorize_display(room_code):
    return jsonify({"error": "Room-scoped kiosk authorization is retired."}), 410


@entrance_bp.route("/display/room/<room_code>/logout", methods=["POST"])
def logout_display(room_code):
    return jsonify({"error": "Room-scoped kiosk authorization is retired."}), 410


@entrance_bp.route("/api/display/room/<room_code>/status", methods=["GET"])
def api_entrance_status(room_code):
    return jsonify(
        {
            "is_active": False,
            "authorized": False,
            "message": "Use the Teacher-generated Attendance Display link.",
        }
    )


@entrance_bp.route("/api/display/room/<room_code>/video", methods=["GET"])
def api_entrance_video(room_code):
    return Response("Room-scoped display access is retired.", status=410)
