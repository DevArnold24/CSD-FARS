"""Central early-opening policy; reads never initialize or repair configuration."""
import sqlite3


class SettingsUnavailable(ValueError):
    pass


class SettingsConflict(ValueError):
    pass


def install_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS attendance_opening_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), minutes INTEGER NOT NULL,
        version INTEGER NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("INSERT OR IGNORE INTO attendance_opening_settings(id,minutes,version) VALUES(1,10,1)")
    conn.execute("""CREATE TABLE IF NOT EXISTS attendance_opening_history (
        id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
        actor INTEGER NOT NULL, previous_minutes INTEGER NOT NULL,
        minutes INTEGER NOT NULL, previous_version INTEGER NOT NULL,
        version INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS attendance_opening_snapshots (
        session_id INTEGER PRIMARY KEY REFERENCES attendance_sessions(id) ON DELETE CASCADE,
        opens_at REAL NOT NULL, minutes INTEGER NOT NULL, policy_version INTEGER NOT NULL)""")


def load(conn=None):
    if conn is None:
        from database.db_manager import managed_transaction
        with managed_transaction(commit=False) as managed:
            return load(managed)
    try:
        row = conn.execute('SELECT minutes,version,updated_at FROM attendance_opening_settings WHERE id=1').fetchone()
        if not row or type(row['minutes']) is not int or not 0 <= row['minutes'] <= 30 or type(row['version']) is not int or row['version'] < 1:
            raise ValueError()
        return dict(row)
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        raise SettingsUnavailable('Attendance opening cannot be verified. Ask the Dean to check Attendance Settings; new attendance cannot start until the setting is available.') from None


def receipt(conn, operation):
    row = conn.execute('SELECT * FROM attendance_opening_history WHERE operation_id=?', (operation,)).fetchone()
    return dict(row) if row else None


def save(conn, payload, actor):
    if not isinstance(payload, dict) or set(payload) != {'minutes', 'version', 'operation_id'}:
        raise ValueError('Review the current setting before saving.')
    minutes, version, operation = (payload[key] for key in ('minutes', 'version', 'operation_id'))
    if type(minutes) is not int or not 0 <= minutes <= 30:
        raise ValueError('Enter a whole number from 0 to 30 minutes.')
    if type(version) is not int or version < 1 or not isinstance(operation, str) or not 8 <= len(operation) <= 80:
        raise ValueError('Review the current setting before saving.')
    previous = receipt(conn, operation)
    if previous:
        if (previous['actor'], previous['minutes'], previous['previous_version']) != (actor, minutes, version):
            raise SettingsConflict('This Save reference belongs to different details. Refresh before saving again.')
        return previous
    old = load(conn)
    if old['version'] != version:
        raise SettingsConflict('The saved setting changed while this page was open. Refresh, review the current value and save again.')
    conn.execute('UPDATE attendance_opening_settings SET minutes=?,version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=1', (minutes,))
    conn.execute("""INSERT INTO attendance_opening_history
        (operation_id,actor,previous_minutes,minutes,previous_version,version) VALUES(?,?,?,?,?,?)""",
        (operation, actor, old['minutes'], minutes, version, version + 1))
    from services.audit import log_system_event
    log_system_event(conn, 'attendance_opening_changed', actor,
                     {'previous_minutes': old['minutes'], 'minutes': minutes, 'version': version + 1, 'operation_id': operation})
    return receipt(conn, operation)


def snapshot(conn, sid):
    # Existing accepted sessions must keep functioning before/without a snapshot.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='attendance_opening_snapshots'").fetchone():
        return None
    row = conn.execute('SELECT * FROM attendance_opening_snapshots WHERE session_id=?', (sid,)).fetchone()
    return dict(row) if row else None
