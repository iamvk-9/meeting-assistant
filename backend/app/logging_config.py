"""Logging setup: console plus a rotating file (uploads, STT/AI failures, API errors)."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(log_dir: Path, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if getattr(root, "_meeting_assistant_configured", False):
        return

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(log_dir / "app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        )
    except OSError:  # read-only filesystem etc.: keep console logging only
        pass

    for handler in handlers:
        handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(handler)
    root.setLevel(level)
    root._meeting_assistant_configured = True  # type: ignore[attr-defined]
