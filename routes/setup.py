"""Dean-owned Setup Center; web requests never perform Windows setup."""

from io import BytesIO
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from flask import abort, current_app, flash, redirect, render_template, request, send_file, session, url_for

import config
from core import get_logger
from core.utils import login_required
from database.db_manager import managed_transaction
from routes.blueprints import settings_bp
from security.rbac import permission_required
from services.audit import log_system_event
from services.deployment_setup import PreferenceError, load_preference, save_preference
from services.setup_client_kit import ClientKitUnavailable, build_client_kit_archive


logger = get_logger("setup")
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))


def _install_root():
    # Server-controlled startup context, never a request path or Host header.
    return Path(current_app.config.get("CSD_INSTALL_ROOT", config.BASE_DIR))


def _public_url():
    value = current_app.config.get("CSD_PUBLIC_URL")
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        parsed = urlsplit(value)
        address = ipaddress.IPv4Address(parsed.hostname)
        if (
            parsed.scheme != "https" or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or not any(address in network for network in _PRIVATE_NETWORKS)
        ):
            return None
    except (TypeError, ValueError):
        return None
    return f"https://{address}/"


def _runtime_mode():
    mode = current_app.config.get("CSD_SERVING_MODE")
    return mode if mode in ("local", "lan") else "unmanaged"


def _kit_bytes():
    connection = current_app.config.get("CSD_LAN_KIT_CONNECTION")
    public_url = _public_url()
    if (
        _runtime_mode() != "lan" or not public_url or not isinstance(connection, dict)
        or connection.get("serverUrl") != public_url
    ):
        raise ClientKitUnavailable()
    return build_client_kit_archive(_install_root(), connection)


def _setup_context():
    result = {
        "runtime_mode": _runtime_mode(),
        "public_url": _public_url() if _runtime_mode() == "lan" else None,
        "preferred_mode": None,
        "preference_error": None,
        "client_kit_available": False,
        "client_kit_reason": None,
    }
    try:
        result["preferred_mode"] = load_preference(_install_root())
    except PreferenceError as exc:
        result["preference_error"] = str(exc)
    try:
        _kit_bytes()
        result["client_kit_available"] = True
    except ClientKitUnavailable:
        result["client_kit_reason"] = (
            "The connection kit is not available for this running server. Complete shared-LAN setup "
            "on the server computer; do not use another installation's kit or bypass a certificate warning."
        )
    return result


@settings_bp.after_request
def setup_private_response(response):
    if request.path == "/system/setup" or request.path.startswith("/system/setup/"):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@settings_bp.get("/system/setup")
@login_required
@permission_required("manage_deployment")
def system_setup():
    return render_template("settings/setup.html", setup=_setup_context())


@settings_bp.post("/system/setup/preference")
@login_required
@permission_required("manage_deployment")
def save_deployment_preference():
    if request.content_length is None or request.content_length > 2048:
        abort(413)
    if request.mimetype != "application/x-www-form-urlencoded":
        abort(415)
    if set(request.form) - {"mode", "csrf_token"} or len(request.form.getlist("mode")) != 1:
        abort(400)
    mode = request.form["mode"]
    try:
        save_preference(_install_root(), mode)
    except PreferenceError as exc:
        return render_template(
            "settings/setup.html", setup=_setup_context(),
            setup_selected_mode=mode if mode in ("local", "lan") else None,
            setup_save_error=str(exc),
        ), 400
    try:
        with managed_transaction(commit=True) as conn:
            log_system_event(conn, "deployment_preference_saved", session["user_id"], {
                "mode": mode, "effect": "next_interactive_launch_requires_local_confirmation",
            })
    except Exception:
        # The atomic preference save has already completed. Do not falsely
        # report that it failed, or make the operator repeat an uncertain write.
        logger.exception("Setup preference saved, but its history entry could not be recorded.")
        flash("The choice was saved, but its history entry could not be recorded.", "warning")
    flash("Next-launch choice saved. The running mode has not changed. Finish active sessions, then open START_HERE.cmd on the server computer and confirm the choice.", "success")
    return redirect(url_for("settings.system_setup"), code=303)


@settings_bp.get("/system/setup/client-kit")
@login_required
@permission_required("manage_deployment")
def download_lan_client_kit():
    try:
        archive = _kit_bytes()
    except ClientKitUnavailable:
        flash("The connection kit could not be verified. Complete or repair shared-LAN setup on the server computer first.", "error")
        return render_template("settings/setup.html", setup=_setup_context()), 409
    return send_file(
        BytesIO(archive), mimetype="application/zip", as_attachment=True,
        download_name="CSD-LAN-Client-Kit.zip", max_age=0,
    )
