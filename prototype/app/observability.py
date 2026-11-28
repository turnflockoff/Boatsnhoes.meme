"""Structured logs (Track D).

The coordinator logs one JSON object per line, for example:

    {"ts": "2026-11-22T21:04:05.123Z", "level": "info", "logger": "bnh.access",
     "msg": "request", "request_id": "9f1c...", "method": "POST",
     "path": "/v1/jobs/next", "status": 200, "duration_ms": 3.2}

Rules that keep logs safe to paste into a bug report:
- The message is a fixed phrase; the data goes in named fields (`extra=`).
- Fields whose *name* is a secret or job content (keys, tokens, codes, prompts, answers,
  signatures) are replaced with "[redacted]" even if a caller passes them by mistake.
- Request lines carry the path only: no query string, no headers, no body.
- Nothing says which jobs are hidden audits or challenges.

Logs are still operator data: request lines show *when* the operator called an operator
endpoint, so don't publish raw logs to workers.

Settings: LOG_FORMAT=json|text (default json), LOG_LEVEL=debug|info|warning|error
(default info). `configure_logging()` is called when uvicorn loads `app.main:app`;
tests and tools that import the app leave global logging alone.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

REDACTED = "[redacted]"

# Field names that must never be written to a log. Matched case-insensitively.
SENSITIVE_FIELDS = frozenset({
    "authorization", "worker_key", "key", "operator_token", "token", "x_operator_token",
    "enrollment_code", "code", "payload", "prompt", "result", "expected_result",
    "signature", "private_key", "secret", "password",
})

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_STANDARD = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}

LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
          "error": logging.ERROR}

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")
_PLAIN_TEXT = re.compile(r"^[A-Za-z0-9._:/@+-]+\Z")


def safe_request_id(value: str | None) -> str | None:
    """A caller-supplied X-Request-ID, or None if it could break a log line or is too long."""
    return value if value and _REQUEST_ID.match(value) else None


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): (REDACTED if str(k).lower() in SENSITIVE_FIELDS else _plain(v))
                for k, v in value.items()}
    return str(value)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, then the record's extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc)
        out: dict[str, Any] = {
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for name, value in vars(record).items():
            if name in _STANDARD or name.startswith("_") or name in out:
                continue
            out[name] = REDACTED if name.lower() in SENSITIVE_FIELDS else _plain(value)
        if record.exc_info and record.exc_info[0] is not None:
            out["error_type"] = record.exc_info[0].__name__
            out["error"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable variant for a terminal: same fields, `key=value` after the message."""

    def format(self, record: logging.LogRecord) -> str:
        data = json.loads(JsonFormatter().format(record))
        head = f"{data.pop('ts')} {data.pop('level').upper():7} {data.pop('logger')}: " \
               f"{data.pop('msg')}"
        error = data.pop("error", None)
        # Quote anything that isn't a plain token, so a value sent by a worker can't
        # contain a newline or spaces that fake a second log line or field.
        tail = " ".join(f"{k}={v if isinstance(v, str) and _PLAIN_TEXT.match(v) else json.dumps(v)}"
                        for k, v in data.items())
        line = f"{head} {tail}".rstrip()
        return f"{line}\n{error}" if error else line


def configure_logging() -> None:
    """Send `bnh.*` logs to stderr in the chosen format. Safe to call more than once."""
    fmt = os.getenv("LOG_FORMAT", "json").strip().lower() or "json"
    if fmt not in ("json", "text"):
        raise ValueError("LOG_FORMAT must be json or text")
    level_name = os.getenv("LOG_LEVEL", "info").strip().lower() or "info"
    if level_name not in LEVELS:
        raise ValueError(f"LOG_LEVEL must be one of {', '.join(LEVELS)}")
    logger = logging.getLogger("bnh")
    logger.handlers[:] = [h for h in logger.handlers if not getattr(h, "_bnh_handler", False)]
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler._bnh_handler = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(LEVELS[level_name])
    logger.propagate = False  # uvicorn's root handler would print every line twice
