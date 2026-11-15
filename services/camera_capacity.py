"""Explicit, provisional admission limits; never inferred hardware capacity.

Defaults are deliberately small and reversible. Missing/corrupt persisted limits
refuse new camera work; reductions do not stop existing owners or release leases.
"""
import sqlite3

FIELDS = ('recognition', 'viewers', 'secondary')
DEFAULTS = {'recognition': 2, 'viewers': 4, 'secondary': 2}
SCHEMA = '''CREATE TABLE IF NOT EXISTS camera_capacity_limits (
 id INTEGER PRIMARY KEY CHECK(id=1), recognition INTEGER NOT NULL,
 viewers INTEGER NOT NULL, secondary INTEGER NOT NULL,
 updated_by_user_id INTEGER, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)'''


class CapacityUnavailable(ValueError):
    pass


def install_schema(conn):
    conn.execute(SCHEMA)
    conn.execute('INSERT OR IGNORE INTO camera_capacity_limits(id,recognition,viewers,secondary) VALUES(1,2,4,2)')


def validate(values):
    if not isinstance(values, dict) or set(values) != set(FIELDS):
        raise CapacityUnavailable('Provide all three camera capacity limits.')
    if any(type(values[key]) is not int or not 0 <= values[key] <= 64 for key in FIELDS):
        raise CapacityUnavailable('Each camera capacity limit must be a whole number from 0 to 64. Zero pauses new admissions.')
    return {key: values[key] for key in FIELDS}


def load_limits(conn=None):
    if conn is None:
        from database.db_manager import managed_transaction
        try:
            with managed_transaction(commit=False) as managed:
                return load_limits(managed)
        except (sqlite3.Error, OSError):
            raise CapacityUnavailable('Camera capacity configuration is unavailable. Ask the Dean or IT Support to check it before new camera work.') from None
    try:
        row = conn.execute('SELECT recognition,viewers,secondary FROM camera_capacity_limits WHERE id=1').fetchone()
        return validate(dict(zip(FIELDS, row)))
    except (sqlite3.Error, TypeError, CapacityUnavailable):
        raise CapacityUnavailable('Camera capacity configuration is unavailable. Ask the Dean or IT Support to check the saved limits before new camera work.') from None


def save_limits(conn, values, actor):
    parsed = validate(values)
    # The explicit audited settings action can repair a malformed record, but a
    # missing schema requires the authorized installation migration.
    try:
        conn.execute('INSERT INTO camera_capacity_limits(id,recognition,viewers,secondary,updated_by_user_id) VALUES(1,?,?,?,?) '
                     'ON CONFLICT(id) DO UPDATE SET recognition=excluded.recognition,viewers=excluded.viewers,secondary=excluded.secondary,'
                     'updated_by_user_id=excluded.updated_by_user_id,updated_at=CURRENT_TIMESTAMP',
                     (*[parsed[key] for key in FIELDS], actor))
    except sqlite3.Error:
        raise CapacityUnavailable('Camera capacity settings could not be saved. Ask the server operator to check migration 028; existing work is unchanged.') from None
    from services.audit import log_system_event
    log_system_event(conn, 'camera_capacity_updated', actor, parsed)
    return parsed


def recognition_reservations(conn, exclude_session=None):
    """Count accepted work and unconfirmed cleanup, not just visible workers."""
    rows = conn.execute('''SELECT s.id FROM attendance_sessions s
        LEFT JOIN attendance_lifecycle l ON l.session_id=s.id
        WHERE s.camera_id IS NOT NULL AND s.id<>?
          AND (l.collection IN ('starting','collecting') OR l.cleanup='pending'
               OR (l.session_id IS NULL AND s.status='Open'))''', (exclude_session or -1,)).fetchall()
    return {row[0] for row in rows}


def recognition_refusal(conn, exclude_session=None):
    try:
        limit = load_limits(conn)['recognition']
        used = len(recognition_reservations(conn, exclude_session))
    except (CapacityUnavailable, sqlite3.Error):
        return 'Recognition capacity cannot be verified. Ask the Dean or IT Support to check it; no new camera attendance was accepted.'
    if used >= limit:
        return 'Recognition capacity is full or paused. Existing attendance is protected. Wait for confirmed cleanup or ask the Dean or IT Support to review the configured limit; Start is not queued.'
    return None
