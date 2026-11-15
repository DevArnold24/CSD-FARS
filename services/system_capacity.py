"""Read-only capacity warnings; never infer a safe camera count from CPU alone."""

import math
import time

CPU_WARNING = 85
CPU_CRITICAL = 95
RAM_WARNING = 85
RAM_CRITICAL = 95
FRAME_WARNING_MS = 2000


def _number(value, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or (maximum is not None and value > maximum):
        return None
    return round(value, 2)


def _age(timestamp, now):
    stamp = _number(timestamp)
    if stamp is None or timestamp > now:
        return None
    return round((now - timestamp) * 1000, 1)


def summarize_capacity(cpu, ram, configured, workers, previews, streams, *, now=None):
    now = time.monotonic() if now is None else now
    cpu, ram = _number(cpu, 100), _number(ram, 100)
    alerts = []
    severity = 0
    if cpu is None or ram is None:
        alerts.append("Current processor or memory usage is unavailable.")
        severity = 1
    for label, value, warning, critical in (
        ("Processor", cpu, CPU_WARNING, CPU_CRITICAL),
        ("Memory", ram, RAM_WARNING, RAM_CRITICAL),
    ):
        if value is not None and value >= warning:
            severity = max(severity, 2 if value >= critical else 1)
            alerts.append(f"{label} usage is {value:.1f}%. Reduce competing work before adding cameras.")
    cameras = []
    for index, worker in enumerate(workers, 1):
        capture_age = _age(worker.get("capture_timestamp_monotonic"), now)
        result_age = _age(worker.get("published_capture_timestamp_monotonic"), now)
        capture_state = worker.get("capture_state")
        execution = worker.get("execution")
        if execution != "running":
            label, tone = "Worker unverified" if execution != "stopped" else "Worker stopped", "warning"
        elif worker.get('capture_unstable') is True:
            label, tone = 'Unstable capture; waiting for 10 seconds of fresh frames', 'warning'
        elif capture_state in {"failed", "reconnecting", "stop_timeout", "cleanup_failed", "stale"}:
            label, tone = "Camera needs attention", "warning"
        elif capture_state != "running" or capture_age is None or result_age is None:
            label, tone = "Awaiting fresh readings", "neutral"
        elif capture_age > FRAME_WARNING_MS or result_age > FRAME_WARNING_MS:
            label, tone = "Delayed frames", "warning"
        else:
            label, tone = "Frames current", "success"
        cameras.append({
            "name": str(worker.get("camera_name") or f"Camera session {index}"),
            "purpose": worker.get("purpose", "Camera session"),
            "status": label, "tone": tone,
            "capture_fps": _number(worker.get("capture_fps")),
            "recognition_fps": _number(worker.get("fps")),
            "preview_fps": _number(worker.get("preview_fps")),
            "capture_age_ms": capture_age, "result_age_ms": result_age,
            "read_failures": _number(worker.get("read_failures")),
            "reconnect_attempts": _number(worker.get("reconnect_attempts")),
        })
    delayed = sum(row["tone"] == "warning" for row in cameras)
    awaiting = sum(row["tone"] == "neutral" for row in cameras)
    if delayed:
        severity = max(severity, 1)
        alerts.append(f"{delayed} camera session(s) have delayed frames or need attention. Inspect the rows below.")
    if awaiting:
        severity = max(severity, 1)
        alerts.append(f"{awaiting} camera session(s) do not yet have fresh recognition readings.")
    active_streams = _number(streams.get("active_streams"))
    max_streams = _number(streams.get("max_streams"))
    if active_streams is None or max_streams is None:
        severity = max(severity, 1)
        alerts.append("Video viewing limits are unavailable.")
    elif active_streams >= max_streams:
        severity = max(severity, 1)
        alerts.append("Video viewing slots are full. Close an unused camera view before opening another.")
    if severity == 2:
        state, label, tone = "high_load", "Very high system load", "danger"
    elif severity == 1:
        state, label, tone = "attention", "Check capacity before adding cameras", "warning"
    elif not workers and not previews:
        state, label, tone = "idle", "No camera workload measured", "neutral"
    else:
        state, label, tone = "within_guidelines", "Current load within warning levels", "success"
    return {
        "available": True, "state": state, "label": label, "tone": tone, "alerts": alerts,
        "configured_cameras": configured, "recognition_sessions": len(workers),
        "preview_reservations": previews, "cameras": cameras,
        "streams": streams, "cpu_percent": cpu, "ram_percent": ram,
        "validated_camera_limit": None,
        "thresholds": {"cpu_warning": CPU_WARNING, "cpu_critical": CPU_CRITICAL,
                       "ram_warning": RAM_WARNING, "ram_critical": RAM_CRITICAL,
                       "frame_warning_ms": FRAME_WARNING_MS},
    }


def system_capacity_snapshot(cpu, ram):
    from database.db_manager import managed_transaction
    from services.stream_capacity import stream_capacity_snapshot
    from workers.session_manager import session_manager

    try:
        with managed_transaction(commit=False) as conn:
            configured = conn.execute("SELECT COUNT(*) FROM camera_devices WHERE is_active = 1").fetchone()[0]
        # No DB transaction is retained while reading worker/lease observations.
        workers, previews = session_manager.capacity_observations()
        result = summarize_capacity(cpu, ram, configured, workers, previews, stream_capacity_snapshot())
        from services.camera_capacity import load_limits, recognition_reservations
        try:
            with managed_transaction(commit=False) as conn:
                limits = load_limits(conn)
                recognition = len(recognition_reservations(conn))
            secondary = previews + sum(w.get('purpose') == 'Recognition tuner' for w in workers)
            result['admission'] = {'configured': limits, 'recognition_reserved': recognition,
                                   'secondary_reserved': secondary, 'provisional': True}
            if recognition >= limits['recognition'] or secondary >= limits['secondary']:
                result['alerts'].append('A configured camera admission limit is full or paused. Existing owners remain protected until cleanup is confirmed.')
                if result['tone'] != 'danger':
                    result.update(state='attention', label='Camera admission limit reached', tone='warning')
            if any(getattr(lease, 'cleanup_attempts', 0) >= 5 and not lease.cleanup_confirmed
                   for lease in tuple(getattr(session_manager, '_camera_leases', {}).values())):
                result['alerts'].append('Secondary camera cleanup remains unconfirmed after repeated attempts. The camera stays reserved; ask technical support to inspect the server. No force release is available.')
                if result['tone'] != 'danger':
                    result.update(state='attention', label='Camera cleanup needs attention', tone='warning')
        except Exception:
            result['admission'] = {'configured': None, 'provisional': True}
            result['alerts'].append('Configured camera limits are unavailable; new camera work is refused.')
            if result['tone'] != 'danger':
                result.update(state='attention', label='Camera capacity configuration unavailable', tone='warning')
        return result
    except Exception:
        return {"available": False, "label": "Capacity readings unavailable", "tone": "warning"}
