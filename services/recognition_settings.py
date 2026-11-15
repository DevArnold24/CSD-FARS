"""Validated database-backed recognition policy."""

import math

from database.db_manager import managed_transaction


FIELD_MAP = {
    "MATCH_THRESHOLD": ("match_threshold", float, 0.35, 0.55),
    "MATCH_MARGIN": ("match_margin", float, 0.08, 0.20),
    "MATCH_CONFIRMATION_PASSES": ("match_confirmation_passes", int, 2, 5),
    "FRAME_SKIP_INTERVAL": ("frame_skip_interval", int, 1, 10),
    "DOWNSCALE_HEIGHT": ("downscale_height", int, 480, 1080),
    "LIVENESS_TTL_SECONDS": ("liveness_ttl_seconds", float, 5.0, 30.0),
}
BOOLEAN_FIELD_MAP = {
    "REQUIRE_LIVENESS": "require_liveness",
}
TEACHER_SAFE_FIELDS = {
    "FRAME_SKIP_INTERVAL": (int, 1, 5),
    "DOWNSCALE_HEIGHT": (int, 480, 720),
}
DEFAULTS = {
    "MATCH_THRESHOLD": 0.50,
    "MATCH_MARGIN": 0.20,
    "MATCH_CONFIRMATION_PASSES": 2,
    "FRAME_SKIP_INTERVAL": 3,
    "DOWNSCALE_HEIGHT": 720,
    "LIVENESS_TTL_SECONDS": 10.0,
    "REQUIRE_LIVENESS": False,
}


def normal_match_threshold(global_threshold, camera_threshold):
    """Use the stricter saved limit for a new normal attendance session.

    A camera may have a stricter limit than the tuner's editable range. Keep
    that limit intact; explicit validated tuner overrides are a separate action.
    """
    def finite_value(value, message):
        if isinstance(value, bool):
            raise ValueError(message)
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(message) from exc
        if not math.isfinite(number):
            raise ValueError(message)
        return number

    global_error = (
        "Saved recognition defaults have an invalid match threshold. "
        "Ask a Dean to correct the Recognition Tuner defaults before starting attendance."
    )
    camera_error = (
        "This camera has an invalid match threshold. "
        "Ask a Dean to correct its settings in Cameras before starting attendance."
    )
    global_value = finite_value(global_threshold, global_error)
    camera_value = finite_value(camera_threshold, camera_error)
    _column, _caster, minimum, maximum = FIELD_MAP["MATCH_THRESHOLD"]
    if not minimum <= global_value <= maximum:
        raise ValueError(global_error)
    if not 0 < camera_value <= 1:
        raise ValueError(camera_error)
    return min(global_value, camera_value)


def validate_settings(values, teacher_safe=False):
    if not isinstance(values, dict):
        raise ValueError("Recognition settings must be an object.")
    policy = TEACHER_SAFE_FIELDS if teacher_safe else {
        key: (spec[1], spec[2], spec[3]) for key, spec in FIELD_MAP.items()
    }
    allowed = set(policy)
    if not teacher_safe:
        allowed.update(BOOLEAN_FIELD_MAP)
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"Unsupported recognition setting: {sorted(unknown)[0]}")
    parsed = {}
    for key, value in values.items():
        if key in BOOLEAN_FIELD_MAP:
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be true or false.")
            parsed[key] = value
            continue
        caster, minimum, maximum = policy[key]
        if isinstance(value, bool):
            raise ValueError(f"{key} must be numeric.")
        try:
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ValueError(f"{key} must be finite.")
            if caster is int and not numeric.is_integer():
                raise ValueError(f"{key} must be a whole number.")
            converted = caster(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{key} must be numeric.") from exc
        if not minimum <= converted <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}.")
        parsed[key] = converted
    return parsed


def load_settings(conn=None):
    if conn is None:
        with managed_transaction(commit=False) as managed:
            return load_settings(managed)
    try:
        row = conn.execute("SELECT * FROM recognition_settings WHERE id = 1").fetchone()
    except Exception:
        row = None
    result = dict(DEFAULTS)
    if row:
        for public_name, (column, _caster, _minimum, _maximum) in FIELD_MAP.items():
            result[public_name] = row[column]
        if "require_liveness" in row.keys():
            result["REQUIRE_LIVENESS"] = bool(row["require_liveness"])
    return result


def save_settings(values, actor_user_id):
    parsed = validate_settings(values)
    if not parsed:
        raise ValueError("No recognition settings were provided.")
    assignments = []
    params = []
    for public_name, value in parsed.items():
        column = BOOLEAN_FIELD_MAP.get(public_name)
        if column:
            assignments.append(f"{column} = ?")
            params.append(int(value))
        else:
            assignments.append(f"{FIELD_MAP[public_name][0]} = ?")
            params.append(value)
    assignments.extend(["updated_by_user_id = ?", "updated_at = CURRENT_TIMESTAMP"])
    params.extend([actor_user_id, 1])
    with managed_transaction(commit=True) as conn:
        conn.execute("INSERT OR IGNORE INTO recognition_settings (id) VALUES (1)")
        conn.execute(f"UPDATE recognition_settings SET {', '.join(assignments)} WHERE id = ?", params)
        return load_settings(conn)
