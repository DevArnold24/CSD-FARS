"""
Core Package -- Logging Infrastructure
---------------------------------------
Configures a centralized logger for the entire application.
All modules must import `get_logger` from this package instead of
using print() or creating ad-hoc loggers.

Usage:
    from core import get_logger
    logger = get_logger(__name__)
    logger.info("Server started.")
"""

import logging
from logging.handlers import RotatingFileHandler

import config


def get_logger(name=None):
    """Return a configured logger instance.

    On the first call, this function configures the root logger with both
    a console StreamHandler and a rotating file handler writing to the
    path specified by ``config.LOG_FILE``. Subsequent calls return child
    loggers that inherit this configuration.

    Args:
        name: Logger name. Pass ``__name__`` from the calling module to
              create a hierarchical logger (e.g. ``core.ai_pipeline``).
              If None, returns the root application logger.

    Returns:
        A ``logging.Logger`` instance.
    """
    root_logger = logging.getLogger("edge_attendance")

    # Configure the root logger only once (guard against duplicate handlers
    # when get_logger is called from multiple modules).
    if not root_logger.handlers:
        root_logger.setLevel(logging.DEBUG)

        formatter = logging.Formatter(
            fmt=config.LOG_FORMAT,
            datefmt=config.LOG_DATE_FORMAT,
        )

        # Console handler -- INFO and above to stdout.
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

        # Rotating file handler -- DEBUG and above to system.log.
        file_handler = RotatingFileHandler(
            filename=config.LOG_FILE,
            maxBytes=config.LOG_MAX_BYTES,
            backupCount=config.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    if name:
        return root_logger.getChild(name)
    return root_logger
