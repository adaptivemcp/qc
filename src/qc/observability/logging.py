"""Structured logging with mandatory redaction (SPEC 15).

Log records are JSON lines. Every message and string field passes
through :func:`qc.security.redaction.redact_text` before emission when
``redact=True`` (the default).
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any

from qc.security.redaction import redact_text

# Attributes that may carry structured context on a record
_EXTRA_KEYS = (
    "operation_id",
    "source_id",
    "dataset_id",
    "job_id",
    "correlation_id",
    "error_code",
    "policy_version",
    "actor",
    "client_id",
)

# Standard LogRecord attribute names; anything else on the record is context.
_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
}


class RedactingJsonFormatter(logging.Formatter):
    """JSON log lines with redaction applied to message and extras."""

    def __init__(self, *, redact: bool = True) -> None:
        super().__init__()
        self.redact = redact

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _EXTRA_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exception"] = record.exc_info[0].__name__
            if record.exc_text:
                payload["exception_detail"] = record.exc_text
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if self.redact:
            payload["message"] = redact_text(str(payload["message"]))
            if "exception_detail" in payload:
                payload["exception_detail"] = redact_text(str(payload["exception_detail"]))
            for k, v in list(payload.items()):
                if isinstance(v, str):
                    payload[k] = redact_text(v)
        return json.dumps(payload, ensure_ascii=False, default=str)


class SafeLoggerAdapter(logging.LoggerAdapter[logging.Logger]):
    """Adapter that accepts arbitrary kwargs as structured fields."""

    def process(self, msg: str, kwargs: MutableMapping[str, Any]) -> tuple[str, MutableMapping[str, Any]]:
        extra = dict(kwargs.pop("extra", {}) or {})
        fields = kwargs.pop("fields", {}) or {}
        merged = {**extra, **fields}
        if merged:
            # separate known operation keys from arbitrary structured fields
            known = {k: v for k, v in merged.items() if k in _EXTRA_KEYS}
            rest = {k: v for k, v in merged.items() if k not in _EXTRA_KEYS}
            adapter_extra = dict(self.extra) if self.extra else {}
            adapter_extra.update(known)
            if rest:
                adapter_extra["extra_fields"] = rest
            merged = adapter_extra
        kwargs["extra"] = merged
        return msg, kwargs


_configured = False


def setup_logging(*, level: str = "info", json_output: bool = True, redact: bool = True) -> None:
    """Configure the root ``qc`` logger exactly once."""
    global _configured
    logger = logging.getLogger("qc")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if _configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    if json_output:
        handler.setFormatter(RedactingJsonFormatter(redact=redact))
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    logger.addHandler(handler)
    logger.propagate = False
    _configured = True


def get_logger(name: str, **context: Any) -> SafeLoggerAdapter:
    """Get a redacting logger for a module."""
    base = name if name.startswith("qc") else f"qc.{name}"
    return SafeLoggerAdapter(logging.getLogger(base), context)
