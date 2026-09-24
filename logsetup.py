"""Idempotent logging setup for the myitbr pipeline.

All pipeline modules log under the ``myitbr`` namespace:

  - ``myitbr.flex``      — Flex Web Service download (ibkr/flex.py)
  - ``myitbr.parser``    — statement parse / section extraction (ibkr/parser.py)
  - ``myitbr.dividends`` — calendar assembly (data/dividends.py)

``setup_logging()`` attaches two sinks to the ``myitbr`` parent logger:

  1. ``StreamHandler`` on stderr.
  2. ``RotatingFileHandler`` at ``logs/app.log`` under the project root
     (1 MB per file, 3 backups -> at most ~4 MB on disk).

The attach is guarded by a sentinel flag AND a handler check, so Streamlit
reruns (which re-execute app.py in the same process) never duplicate handlers
or log lines. Propagation stays ON so pytest's ``caplog`` (root-level capture)
still sees pipeline records.

Setup failures are swallowed: logging must never break the data pipeline.
Modules only ever call ``logging.getLogger("myitbr.<name>")`` — emitting to an
unconfigured logger is a silent no-op, never an error.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "app.log"

ROOT_LOGGER_NAME = "myitbr"

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 3
_SENTINEL = "_myitbr_configured"


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Attach stderr + rotating-file handlers to the ``myitbr`` logger once.

    Idempotent: safe to call on every Streamlit rerun — repeat calls only
    refresh the level and return the already-configured logger. Returns the
    ``myitbr`` parent logger either way. Never raises.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    try:
        logger.setLevel(level)
        if getattr(logger, _SENTINEL, False) and logger.handlers:
            return logger

        formatter = logging.Formatter(_FORMAT)

        try:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(formatter)
            logger.addHandler(stream)
        except Exception:  # noqa: BLE001 — logging must never crash the app
            pass

        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception:  # noqa: BLE001 — e.g. read-only FS: stderr still works
            pass

        setattr(logger, _SENTINEL, True)
    except Exception:  # noqa: BLE001 — absolute last resort
        pass
    return logger
