"""Durable system audit helpers."""

import json
import sqlite3
from datetime import datetime, timezone

from core import get_logger
import config

logger = get_logger("audit")


def _write_fallback(action, actor_user_id, details, error_msg):
    """Write a failed audit event to the local fallback log file."""
    try:
        entry = json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": action,
                "actor_user_id": actor_user_id,
                "details": details or {},
                "error": str(error_msg),
            },
            default=str,
        )
        with open(config.AUDIT_FALLBACK_LOG, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception as fallback_err:
        logger.error("Audit fallback log write also failed: %s", fallback_err)


def log_system_event(conn, action, actor_user_id=None, details=None):
    """Write an operational audit event if the target table exists.

    Expected database errors (OperationalError, IntegrityError) are logged as
    warnings and written to the fallback log file so no audit events are lost.
    Unexpected exceptions are re-raised so programming bugs surface immediately.
    """
    payload = json.dumps(details or {}, default=str)
    try:
        conn.execute(
            """
            INSERT INTO system_audit_logs (actor_user_id, action, details)
            VALUES (?, ?, ?)
            """,
            (actor_user_id, action, payload),
        )
    except (sqlite3.OperationalError, sqlite3.IntegrityError) as e:
        logger.warning("Audit log write failed (expected DB error): %s", e)
        _write_fallback(action, actor_user_id, details, e)
    except Exception:
        raise
