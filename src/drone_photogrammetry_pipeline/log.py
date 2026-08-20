"""Logging setup.

Two sinks that never mix. The Rich console carries human-readable CLI output; the JSONL
file carries the machine-readable processing log that a later audit reads. A message
written to the console is presentation and may change freely; a record written to the
JSONL log is data and is part of the run's evidence.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from rich.console import Console

_LOGGER_ROOT = "drone_photogrammetry_pipeline"

_console = Console()
_error_console = Console(stderr=True)

# Attributes present on every LogRecord. Anything else was passed by a caller via
# `extra=` and belongs in the structured output.
#
# These names are also FORBIDDEN as `extra` keys: the standard library raises if `extra`
# would overwrite one, and because that only happens once logging is actually configured, the
# failure hides during isolated tests and appears in production. `name`, `module`, `filename`
# and `args` are the easy ones to reach for by accident — use `task_name`, `block_name` and
# so on instead.
_STANDARD_RECORD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)


class JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_FIELDS and key != "taskName":
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure(level: str = "INFO", jsonl_path: Path | None = None) -> None:
    logger = logging.getLogger(_LOGGER_ROOT)
    logger.setLevel(level.upper())
    logger.propagate = False
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()

    if jsonl_path is not None:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(jsonl_path, encoding="utf-8")
        handler.setFormatter(JsonlFormatter())
        logger.addHandler(handler)
    else:
        logger.addHandler(logging.NullHandler())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{_LOGGER_ROOT}.{name}")


def console() -> Console:
    return _console


def error_console() -> Console:
    return _error_console
