"""Safe, role-scoped presentation of operational audit events."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone


TECHNICAL_PREFIXES = (
    "attendance_display_",
    "backup_",
    "building_",
    "camera_",
    "diagnostic_",
    "purge_",
    "recognition_",
    "room_",
    "sandbox_",
    "system_",
    "worker_",
)
HISTORY_EVENT_TYPES = (
    ("attendance_display", "Display"), ("backup", "Backups"),
    ("building", "Buildings"), ("camera", "Cameras"),
    ("diagnostic", "Diagnostics"), ("purge", "Maintenance"),
    ("recognition", "Recognition"), ("room", "Rooms"),
    ("sandbox", "Recognition tests"), ("system", "System"), ("worker", "Workers"),
)


def history_event_types(it_view=False):
    return HISTORY_EVENT_TYPES if it_view else (*HISTORY_EVENT_TYPES, ("administration", "Other administration"))


def _history_cursor(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 19:
        raise ValueError("This history page is unavailable. View latest events to continue.")
    parsed = int(value)
    if not 0 < parsed <= 2**63 - 1:
        raise ValueError("This history page is unavailable. View latest events to continue.")
    return parsed


def load_technical_event_page(conn, *, event_type="", before=None, after=None, it_view=False):
    """Scope in SQL before the page limit, with stable navigation by audit ID."""
    if event_type and event_type not in dict(history_event_types(it_view)):
        raise ValueError("Choose an available event type or view latest events.")
    before, after = _history_cursor(before), _history_cursor(after)
    if before is not None and after is not None:
        raise ValueError("This history page is unavailable. View latest events to continue.")
    # GLOB treats underscores literally and matches the presentation allowlist's
    # case-sensitive startswith checks. SQL LIKE would widen both properties.
    technical = "(" + " OR ".join("sal.action GLOB ?" for _ in TECHNICAL_PREFIXES) + ")"
    clauses, params = ["1"], []
    if it_view or event_type == "administration":
        clauses.append(("NOT " if event_type == "administration" else "") + technical)
        params.extend(prefix + "*" for prefix in TECHNICAL_PREFIXES)
    if event_type and event_type != "administration":
        clauses.append("sal.action GLOB ?")
        params.append(event_type + "_*")
    scope = " AND ".join(clauses)
    page_clause, page_params = "", []
    if before is not None or after is not None:
        page_clause = " AND sal.id " + ("<" if before is not None else ">") + " ?"
        page_params = [before if before is not None else after]
    order = "ASC" if after is not None else "DESC"
    rows = conn.execute(
        f"""SELECT sal.id, sal.action, sal.details, sal.created_at, u.username AS actor
            FROM system_audit_logs sal LEFT JOIN users u ON u.id = sal.actor_user_id
            WHERE {scope}{page_clause} ORDER BY sal.id {order} LIMIT 50""",
        [*params, *page_params],
    ).fetchall()
    if after is not None:
        rows.reverse()
    newer = older = None
    if rows:
        if conn.execute(f"SELECT 1 FROM system_audit_logs sal WHERE {scope} AND sal.id > ? LIMIT 1", [*params, rows[0]["id"]]).fetchone():
            newer = rows[0]["id"]
        if conn.execute(f"SELECT 1 FROM system_audit_logs sal WHERE {scope} AND sal.id < ? LIMIT 1", [*params, rows[-1]["id"]]).fetchone():
            older = rows[-1]["id"]
    return rows, newer, older


SAFE_DETAIL_KEYS = {
    "camera_id",
    "camera_name",
    "device_id",
    "device_name",
    "room_code",
    "status",
    "is_active",
    "failure_count",
    "reconnect_attempts",
    "worker_mode",
}
REASON_KEYS = ("failure_reason", "error", "exception", "reason", "message")
SENSITIVE_KEY = re.compile(
    r"student|learner|person|face|biometric|embedding|rtsp|source|url|password|passwd|credential|secret|token|key",
    re.IGNORECASE,
)
URL_VALUE = re.compile(
    r"(?i)\b(?:rtsp|rtsps|http|https|ftp|ftps|ws|wss)://[^\s<>'\"]+"
)
BEARER_VALUE = re.compile(
    r"(?i)\b(?P<label>bearer)(?P<separator>\s+)(?P<value>[a-z0-9._~+/=-]+)"
)
ASSIGNED_SECRET = re.compile(
    r"(?i)\b(?P<label>password|passwd|pwd|credentials?|secret|"
    r"(?:access|session|refresh|auth(?:entication)?)[\s_-]*token|token|"
    r"api[\s_-]*key|(?:encryption|decryption|fernet|private|signing|master|"
    r"biometric|camera[\s_-]*source|backup[\s_-]*archive)[\s_-]*key)"
    r"(?P<context>\s+for\s+[a-z0-9_.@-]+)?"
    r"(?P<separator>\s*(?::|=|\bis\b|\bare\b|\bwas\b|\bwere\b|"
    r"\bvalue(?:\s+is)?\b)\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
LOOSE_SECRET = re.compile(
    r"(?i)\b(?P<label>password|passwd|pwd|credentials?|secret|token|"
    r"api[\s_-]*key|(?:encryption|decryption|fernet|private|signing|master|"
    r"biometric|camera[\s_-]*source|backup[\s_-]*archive)[\s_-]*key)"
    r"(?P<separator>\s+)"
    r"(?P<value>(?!(?:for|is|are|was|were|value|\[redacted)\b)[^\s,;]+)"
)
STUDENT_IDENTIFIER = re.compile(
    r"(?i)\b(?P<label>(?:student|learner|person)(?:[_\s-]*(?:id|identifier)))"
    r"(?P<separator>\s*[:=#-]?\s*)(?P<value>[a-z0-9@._-]+)"
)
STUDENT_PERSON_IDENTIFIER = re.compile(
    r"\b(?P<label>(?i:student|learner|person))"
    r"(?P<person_separator>\s+)"
    r"(?P<person>[A-Z][A-Za-z'â€™-]+(?:\s+[A-Z][A-Za-z'â€™-]+){0,3}?)"
    r"(?P<identifier_label>\s+(?i:ID|identifier))"
    r"(?P<identifier_separator>\s*[:=#-]?\s*)"
    r"(?P<identifier>[A-Za-z0-9@._-]+)"
)
LABELED_PERSON = re.compile(
    r"\b(?P<label>(?i:student|learner|person)(?:\s+(?i:name|named))?)"
    r"(?P<separator>\s*(?::|=|(?i:is|was))?\s+)"
    r"(?P<value>[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){0,3})"
)
RECOGNITION_PERSON = re.compile(
    r"\b(?P<label>(?i:recognition|identity)\s+"
    r"(?i:rejected|matched|identified|recognized|detected))"
    r"(?P<separator>\s+)"
    r"(?P<value>[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){0,3})"
)
CONTEXTUAL_PERSON_IDENTIFIER = re.compile(
    r"(?P<person>\[redacted person\])"
    r"(?P<opening>\s*\(\s*)"
    r"(?P<value>[A-Za-z0-9][A-Za-z0-9@._-]*)"
    r"(?P<closing>\s*\))"
)
BIOMETRIC_VALUE = re.compile(
    r"\b(?P<label>(?i:face[\s_-]*embedding(?:\s+(?:digest|hash|vector)(?=\s|:|=))?|"
    r"biometric(?:[\s_-]+(?:embedding|vector|template|digest|hash|data)(?=\s|:|=))?|"
    r"embedding(?:\s+(?:template|digest|hash|vector)(?=\s|:|=))?|"
    r"face[\s_-]*(?:digest|hash|vector|template)(?=\s|:|=)|vector))"
    r"(?P<separator>\s*(?::|=|(?i:is)|(?i:was))?\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
EMAIL_ADDRESS = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)

FAILURE_KEYS = (
    "failure_reason",
    "last_failure_reason",
    "error",
    "error_message",
    "exception",
    "exception_message",
)
SUCCESS_KEYS = ("success", "ok", "is_success", "successful", "passed")
FAILURE_STATES = {
    "error",
    "exception",
    "fail",
    "failed",
    "failure",
    "false",
    "0",
    "no",
    "offline",
    "unhealthy",
    "unsuccessful",
}
WARNING_STATES = {"degraded", "inactive", "reconnecting", "retrying", "warning"}
FAILURE_ACTION = re.compile(
    r"(?:^|_)(?:error|exception|fail|failed|failure|unsuccessful)(?:_|$)",
    re.IGNORECASE,
)
WARNING_ACTION = re.compile(
    r"(?:^|_)(?:degraded|inactive|reconnect|reconnecting|retry|warning)(?:_|$)",
    re.IGNORECASE,
)


def _details_object(raw_details):
    if isinstance(raw_details, dict):
        return raw_details
    try:
        value = json.loads(raw_details or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _redacted_match(match, replacement="[redacted]"):
    context = match.groupdict().get("context") or ""
    return "".join(
        (match.group("label"), context, match.group("separator"), replacement)
    )


def _redacted_student_person_match(match):
    return "".join((
        match.group("label"),
        match.group("person_separator"),
        "[redacted person]",
        match.group("identifier_label"),
        match.group("identifier_separator"),
        "[redacted student id]",
    ))


def _redacted_contextual_identifier_match(match):
    return "".join((
        match.group("person"),
        match.group("opening"),
        "[redacted student id]",
        match.group("closing"),
    ))


def _safe_text(value):
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = URL_VALUE.sub("[redacted URL]", text)
    text = BEARER_VALUE.sub(lambda match: _redacted_match(match), text)
    text = ASSIGNED_SECRET.sub(lambda match: _redacted_match(match), text)
    text = LOOSE_SECRET.sub(lambda match: _redacted_match(match), text)
    text = STUDENT_IDENTIFIER.sub(
        lambda match: _redacted_match(match, "[redacted identifier]"), text
    )
    text = STUDENT_PERSON_IDENTIFIER.sub(_redacted_student_person_match, text)
    text = LABELED_PERSON.sub(
        lambda match: _redacted_match(match, "[redacted person]"), text
    )
    text = RECOGNITION_PERSON.sub(
        lambda match: _redacted_match(match, "[redacted person]"), text
    )
    text = CONTEXTUAL_PERSON_IDENTIFIER.sub(
        _redacted_contextual_identifier_match, text
    )
    text = BIOMETRIC_VALUE.sub(
        lambda match: _redacted_match(match, "[redacted biometric value]"), text
    )
    text = EMAIL_ADDRESS.sub("[redacted identifier]", text)
    return text[:240]


def _authorized_details(details):
    """Keep operational context while recursively removing high-risk values."""

    def sanitize(key, value):
        if SENSITIVE_KEY.search(str(key)):
            return "[redacted]"
        if isinstance(value, dict):
            return {str(child): sanitize(child, item) for child, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [sanitize(key, item) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return _safe_text(value)

    sanitized = {str(key): sanitize(key, value) for key, value in details.items()}
    return json.dumps(sanitized, sort_keys=True)


def _local_timestamp(raw_value, offset_minutes):
    raw = str(raw_value or "").strip()
    parsed = None
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    if parsed is None:
        return "Unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local_zone = timezone(timedelta(minutes=offset_minutes))
    return parsed.astimezone(local_zone).isoformat(sep=" ", timespec="seconds")


def _category(action):
    if action.startswith("camera_"):
        return "Camera"
    if action.startswith(("recognition_", "sandbox_")):
        return "Recognition"
    if action.startswith("backup_"):
        return "Backup"
    if action.startswith("purge_"):
        return "Data maintenance"
    if action.startswith(("room_", "building_")):
        return "Location"
    if action.startswith("attendance_display_"):
        return "Display"
    return "System"


def _has_value(value):
    return value not in (None, "", [], {}, ())


def _structured_false(value):
    if value is False or value == 0:
        return True
    return str(value).strip().lower() in {"false", "no", "0", "failed", "failure"}


def _severity(action, details):
    """Derive severity from structured state rather than prose keywords."""
    if any(_has_value(details.get(key)) for key in FAILURE_KEYS):
        return "Error"
    if any(key in details and _structured_false(details[key]) for key in SUCCESS_KEYS):
        return "Error"

    state = str(
        details.get("status", details.get("outcome", details.get("result", "")))
    ).strip().lower()
    if state in FAILURE_STATES or FAILURE_ACTION.search(action):
        return "Error"

    if state in WARNING_STATES or WARNING_ACTION.search(action):
        return "Warning"
    if details.get("is_active") is False:
        return "Warning"
    if any(
        isinstance(details.get(key), (int, float)) and details[key] > 0
        for key in ("failure_count", "reconnect_attempts")
    ):
        return "Warning"
    return "Info"


def prepare_technical_events(rows, offset_minutes, it_view=False):
    """Return display-ready events, filtering IT Support to operational categories."""
    events = []
    for row in rows:
        action = str(row["action"] or "system_event")
        if it_view and not action.startswith(TECHNICAL_PREFIXES):
            continue
        details = _details_object(row["details"])
        safe = {
            key: _safe_text(value)
            for key, value in details.items()
            if key in SAFE_DETAIL_KEYS and not SENSITIVE_KEY.search(key)
        }
        reason = next(
            (_safe_text(details[key]) for key in REASON_KEYS if details.get(key)),
            "",
        )
        camera_id = safe.get("camera_id") or safe.get("device_id")
        device = safe.get("camera_name") or safe.get("device_name")
        if not device and camera_id:
            device = f"Camera #{camera_id}"
        if not device:
            device = safe.get("room_code", "System")
        events.append({
            "action": action,
            "event": action.replace("_", " ").title(),
            "category": _category(action),
            "severity": _severity(action, details),
            "device": device,
            "reason": reason or "--",
            "actor": row["actor"] or "System",
            "local_time": _local_timestamp(row["created_at"], offset_minutes),
            "safe_details": json.dumps(safe, sort_keys=True) if safe else "{}",
            "authorized_details": _authorized_details(details),
        })
    return events
