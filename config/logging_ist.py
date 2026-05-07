"""
config/logging_ist.py
─────────────────────
IST-aware logging formatter.

Python's default logging.Formatter uses time.localtime() which reflects
the OS timezone. On UTC servers this produces UTC timestamps in logs.
This formatter overrides formatTime() to always emit Asia/Kolkata (IST) time.

Usage:
    from config.logging_ist import setup_logging
    setup_logging(level=logging.INFO, log_file="logs/bot.log")
"""

import logging
import logging.handlers
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class ISTFormatter(logging.Formatter):
    """Logging formatter that always stamps records in IST (Asia/Kolkata)."""

    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, tz=IST)
        if datefmt:
            return ct.strftime(datefmt)
        # Default: 2026-03-24 21:58:07,205
        t = ct.strftime("%Y-%m-%d %H:%M:%S")
        return f"{t},{record.msecs:03.0f}"


def setup_logging(
    level: int = logging.INFO,
    fmt: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    log_file: str | None = None,
) -> None:
    """
    Configure root logger with IST timestamps and daily log rotation.

    Args:
        level:    Logging level (default INFO)
        fmt:      Log record format string
        log_file: Path to log file. Rotates at midnight; all rotated files are kept
                  (backupCount=0 means no automatic deletion).
    """
    formatter = ISTFormatter(fmt)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        import os
        os.makedirs(os.path.dirname(log_file) if os.path.dirname(log_file) else ".", exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(
            log_file,
            when        = "midnight",
            interval    = 1,
            backupCount = 0,       # 0 = keep all rotated files, never delete
            encoding    = "utf-8",
            utc         = False,
        )
        # Suffix format: watchdog.log.2026-05-06
        fh.suffix = "%Y-%m-%d"
        handlers.append(fh)

    for handler in handlers:
        handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    # Remove any handlers added by basicConfig before this call
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)
