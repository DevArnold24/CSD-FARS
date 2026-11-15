"""Versioned staff acknowledgment; never a credential or student consent."""

# Change this version whenever responsibilities materially change. Both the
# rendered confirmation and account recall are bound to this value.
VERSION = "2026-09-24-v1"


def install_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS login_notice_acknowledgments (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        notice_version TEXT NOT NULL,
        acknowledged_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        remember INTEGER NOT NULL CHECK (remember IN (0, 1)),
        PRIMARY KEY (user_id, notice_version)
    )""")


def accept_or_recall(conn, user_id, form):
    """Called under the login writer lock, only for a verified active account."""
    if form.get("notice_acknowledged") == "1":
        if form.get("notice_version") != VERSION:
            return None
        remember = form.get("remember_notice") == "1"
        conn.execute("""INSERT INTO login_notice_acknowledgments
            (user_id, notice_version, remember) VALUES (?, ?, ?)
            ON CONFLICT(user_id, notice_version) DO UPDATE SET
            acknowledged_at = CURRENT_TIMESTAMP, remember = excluded.remember""",
            (user_id, VERSION, int(remember)))
        return remember
    row = conn.execute("""SELECT remember FROM login_notice_acknowledgments
        WHERE user_id = ? AND notice_version = ?""", (user_id, VERSION)).fetchone()
    return True if row and row["remember"] == 1 else None
