"""
Database Manager
----------------
Provides initialization, connection management, and context-managed
utilities for the local SQLite database.
"""

import os
import sqlite3
from contextlib import contextmanager

import config
from core import get_logger

logger = get_logger(__name__)



@contextmanager
def managed_transaction(commit=True):
    """
    Context manager for database transactions.

    Args:
        commit (bool): If True, commits changes if no exception occurs.
                       If False, acts as a read-only context manager.
    Yields:
        sqlite3.Connection
    """
    conn = get_db_connection()
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        if commit:
            conn.rollback()
        raise
    finally:
        conn.close()

def get_db_connection():
    """Open a new SQLite connection to the attendance database.

    The connection is configured with:
    - ``row_factory = sqlite3.Row`` for dict-like row access.
    - ``PRAGMA foreign_keys = ON`` to enforce referential integrity.

    Returns:
        A ``sqlite3.Connection`` instance. The caller is responsible for
        closing it (prefer using it as a context manager).
    """
    conn = sqlite3.connect(config.DATABASE_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db():
    """Create the database file and apply the full schema.

    This function is idempotent. It uses ``CREATE TABLE IF NOT EXISTS``
    statements from ``schema.sql``, so running it multiple times against
    an existing database is safe.

    The WAL journal mode is set by the schema script itself.
    """
    db_dir = os.path.dirname(config.DATABASE_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        logger.info("Created database directory: %s", db_dir)

    if not os.path.isfile(config.SCHEMA_PATH):
        logger.error("Schema file not found at: %s", config.SCHEMA_PATH)
        raise FileNotFoundError(
            f"Schema file not found: {config.SCHEMA_PATH}"
        )

    with open(config.SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_sql = f.read()

    conn = sqlite3.connect(config.DATABASE_PATH, timeout=15.0)
    try:
        conn.executescript(schema_sql)
        conn.commit()
        logger.info(
            "Database initialized at: %s (WAL mode enabled)",
            config.DATABASE_PATH,
        )
    except sqlite3.Error:
        logger.exception("Failed to apply database schema")
        raise
    finally:
        conn.close()

    # Verify WAL mode was applied.
    verify_conn = sqlite3.connect(config.DATABASE_PATH, timeout=15.0)
    try:
        journal_mode = verify_conn.execute("PRAGMA journal_mode;").fetchone()[0]
        if journal_mode.lower() != "wal":
            logger.warning(
                "Expected WAL journal mode but got '%s'", journal_mode
            )
        else:
            logger.info("Confirmed journal_mode=WAL")
    finally:
        verify_conn.close()
