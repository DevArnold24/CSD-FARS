"""Read-only camera observations for the department dashboard.

This summary is deliberately scoped to camera checks. It does not probe devices
or imply that attendance accuracy, backups, or the entire system was validated.
"""

from collections import Counter
import datetime as dt
import math
import time


RECENT_CHECK_SECONDS = 300
FRESH_FRAME_SECONDS = 2


def _check_age(value, now):
    try:
        checked_at = dt.datetime.fromisoformat(str(value))
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=dt.timezone.utc)
        return (now - checked_at).total_seconds()
    except (TypeError, ValueError):
        return None


def _observation(camera, worker, now, monotonic_now):
    if camera.get("lifecycle_status") in {"Maintenance", "Retired"}:
        return "attention"
    if worker is not None:
        if worker.get('capture_unstable') is True:
            return 'attention'
        state = worker.get("capture_state", "unknown")
        if state in {"failed", "reconnecting", "stop_timeout", "cleanup_failed"}:
            return "attention"
        if state == "stale":
            return "stale"
        if state == "running":
            try:
                age = monotonic_now - float(worker.get("capture_timestamp_monotonic"))
            except (TypeError, ValueError):
                age = float("nan")
            if not math.isfinite(age):
                return "not_checked"
            return "healthy" if 0 <= age <= FRESH_FRAME_SECONDS else "stale"
        # An earlier setup test cannot vouch for a worker now starting/stopping.
        return "not_checked"

    status = str(camera.get("last_test_status") or "").strip().lower()
    if status == "failed":
        return "attention"
    if status != "ok":
        return "not_checked"
    age = _check_age(camera.get("last_health_check_at"), now)
    if age is None:
        return "not_checked"
    return "healthy" if 0 <= age <= RECENT_CHECK_SECONDS else "stale"



def describe_stored_camera_health(camera, *, now=None):
    """Describe a saved check at read time; never probe a device or a worker."""
    camera = dict(camera)
    now = now or dt.datetime.now(dt.timezone.utc)
    state = _observation(camera, None, now, 0)
    lifecycle = camera.get("lifecycle_status")
    label = lifecycle if lifecycle in {"Maintenance", "Retired"} else {
        "healthy":"Healthy", "stale":"Stale", "attention":"Failed", "not_checked":"Not tested"
    }[state]
    checked = camera.get("last_health_check_at")
    try:
        stamp = dt.datetime.fromisoformat(str(checked))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        when = stamp.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        check_detail = f"Last saved check: {when}."
    except (TypeError, ValueError):
        check_detail = "No valid check time is available."
    meaning = {
        "Healthy":"Passed within 5 minutes of this refresh.",
        "Stale":"The saved passed check is not current.",
        "Failed":"The latest saved check failed.",
        "Not tested":"No dated passed check is available.",
        "Maintenance":"This camera is marked for maintenance.",
        "Retired":"This camera is retired."
    }[label]
    return {"state":state, "label":label, "detail":f"{meaning} {check_detail}"}

def summarize_camera_health(cameras, workers, *, now=None, monotonic_now=None):
    """Summarize active-camera rows and allowlisted worker observations."""
    now = now or dt.datetime.now(dt.timezone.utc)
    monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
    by_camera = {str(worker.get("camera_id")): worker for worker in workers}
    counts = Counter(
        _observation(camera, by_camera.get(str(camera["id"])), now, monotonic_now)
        for camera in cameras
    )
    total = len(cameras)
    observation_counts = {state: counts[state] for state in ("healthy", "attention", "stale", "not_checked")}
    if not total:
        return {"state": "no_cameras", "label": "No active cameras", "tone": "neutral",
                "counts": observation_counts,
                "detail": "Camera health has not been checked. No cameras are active."}
    if counts["attention"]:
        state, label, tone = "attention", "Needs attention", "warning"
    elif counts["stale"]:
        state, label, tone = "stale", "Stale observations", "warning"
    elif counts["not_checked"]:
        state, label, tone = "not_checked", "Not checked", "neutral"
    else:
        state, label, tone = "healthy", "Checks current", "success"
    detail = (
        f"{counts['healthy']} of {total} active cameras have a fresh frame or a passed check within 5 minutes. "
        f"{counts['attention']} need attention; {counts['stale']} are stale; "
        f"{counts['not_checked']} have no current observation."
    )
    return {"state": state, "label": label, "tone": tone, "detail": detail, "counts": observation_counts}


def camera_health_summary():
    """Use stored checks and worker telemetry; never open or decrypt a source."""
    from database.db_manager import managed_transaction
    from workers.session_manager import session_manager

    try:
        with managed_transaction(commit=False) as conn:
            cameras = [dict(row) for row in conn.execute(
                "SELECT id, lifecycle_status, last_test_status, last_health_check_at "
                "FROM camera_devices WHERE is_active = 1"
            ).fetchall()]
        # Finish database access before acquiring the worker lifecycle guard.
        workers = []
        with session_manager.lifecycle_guard():
            for key in list(session_manager.active_sessions):
                if session_manager.is_running(key):
                    context = session_manager.active_sessions[key]
                    workers.append({name: context.get(name) for name in (
                        "camera_id", "capture_state", "capture_unstable", "capture_timestamp_monotonic",
                    )})
        return summarize_camera_health(cameras, workers)
    except Exception:
        # Unavailable diagnostics are not a successful check; keep the normal
        # dashboard useful and do not expose raw backend exception text.
        return {"state": "unknown", "label": "Unknown", "tone": "neutral",
                "detail": "Camera health information is temporarily unavailable."}
