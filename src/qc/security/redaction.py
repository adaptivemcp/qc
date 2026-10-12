"""Redaction helpers (SPEC 8.2, 15).

Every string that may be logged, serialized into an error, or returned
to an MCP client passes through :func:`redact_text` first.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Ordered: more specific patterns first.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # URLs with credentials: scheme://user:pass@host
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)[^@\s]+@"), r"\1" + REDACTED + "@"),
    # Bearer tokens (before generic key=value so 'Authorization: Bearer x' fully matches)
    (re.compile(r"(?i)\b(bearer\s+)[a-z0-9._\-]+"), r"\1" + REDACTED),
    # password=..., token=..., etc.
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
            r"authorization|auth|credential|private[_-]?key)(\s*[=:]\s*)([^\s,'\"]+)"
        ),
        r"\1\2" + REDACTED,
    ),
    # AWS-style keys
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), REDACTED),
    # Email addresses (may be personal data in logs)
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), REDACTED),
    # Inline connection strings with query params containing secrets
    (re.compile(r"(?i)([?&](password|token|secret|key)=)[^&\s]+"), r"\1" + REDACTED),
]

_SECRET_MARKER_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|credential)"
)


def redact_text(text: str) -> str:
    """Apply all redaction patterns to free text."""
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact mapping values whose keys look sensitive."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if _SECRET_MARKER_RE.search(key):
            out[key] = REDACTED
        elif isinstance(value, dict):
            out[key] = redact_mapping(value)
        elif isinstance(value, list):
            out[key] = [
                redact_mapping(v)
                if isinstance(v, dict)
                else redact_text(v)
                if isinstance(v, str)
                else v
                for v in value
            ]
        elif isinstance(value, str):
            out[key] = redact_text(value)
        else:
            out[key] = value
    return out


def safe_error_message(exc: BaseException) -> str:
    """Render an exception message with redaction applied.

    Database drivers sometimes embed connection strings or values in
    exception text; nothing may leak unredacted.
    """
    return redact_text(f"{type(exc).__name__}: {exc}")
