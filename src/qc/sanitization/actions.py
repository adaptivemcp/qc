"""Transformation actions (SPEC 8.4).

keep, mask, redact, generalize, tokenize, synthetic, drop, manual_review.
Tokenization uses keyed HMAC (never a plain hash) so low-entropy
identifiers cannot be reversed by dictionary attack.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

from qc.domain.enums import ColumnAction
from qc.domain.errors import PolicyDeniedError, ValidationError
from qc.domain.models import ColumnPolicy

# In-memory token key: never persisted to artifacts or logs (SPEC 8.4).
# Loaded once per process from the environment or generated ephemerally.
_TOKEN_KEY: bytes | None = None

REDACTED_MARKER = "[REDACTED]"


def _token_key() -> bytes:
    global _TOKEN_KEY
    if _TOKEN_KEY is not None:
        return _TOKEN_KEY
    env = os.environ.get("QC_TOKENIZATION_KEY")
    if env:
        _TOKEN_KEY = (
            bytes.fromhex(env)
            if all(c in "0123456789abcdef" for c in env.lower())
            else env.encode()
        )
    else:
        _TOKEN_KEY = os.urandom(32)
    return _TOKEN_KEY


def reset_token_key(key: bytes | None = None) -> None:
    """Testing hook: set/clear the process tokenization key."""
    global _TOKEN_KEY
    _TOKEN_KEY = key


def stable_token(value: Any) -> str:
    """Keyed HMAC-SHA256 pseudonym, truncated to 32 hex chars (SPEC 8.4)."""
    digest = hmac.new(_token_key(), f"qc-token:{value}".encode(), hashlib.sha256)
    return f"tok_{digest.hexdigest()[:32]}"


def transform_value(value: Any, policy: ColumnPolicy, *, column: str = "") -> Any:
    """Apply one column action to one value.

    Returns ``None`` for redacted/dropped. Raises for actions that need
    a generation pass (``synthetic``) or human input (``manual_review``)
    when encountered in a preview path — callers pre-filter those.
    """
    action = policy.action

    if action == ColumnAction.KEEP:
        return value
    if action == ColumnAction.DROP:
        return None
    if action == ColumnAction.REDACT:
        return REDACTED_MARKER
    if action == ColumnAction.MASK:
        return _mask(value, policy)
    if action == ColumnAction.GENERALIZE:
        return _generalize(value, policy, column=column)
    if action == ColumnAction.TOKENIZE:
        if value is None:
            return None
        return stable_token(value)
    if action == ColumnAction.SYNTHETIC:
        # Preview path: emit a placeholder token so structure is visible
        # without copying source values; real synthesis runs in generate.
        if value is None:
            return None
        return f"synthetic_{stable_token(value)[4:16]}"
    if action == ColumnAction.MANUAL_REVIEW:
        raise PolicyDeniedError(
            f"column '{column}' requires manual review before output",
            detail={"column": column},
        )
    raise ValidationError(f"unknown action {action} for column '{column}'")


def transform_row(value: Any, policy: ColumnPolicy, *, column: str = "") -> Any:
    """Alias used by application layer; same semantics as transform_value."""
    return transform_value(value, policy, column=column)


def _mask(value: Any, policy: ColumnPolicy) -> Any:
    if value is None:
        return None
    s = str(value)
    keep_prefix = max(policy.mask_keep_prefix, 0)
    keep_suffix = max(policy.mask_keep_suffix, 0)
    if keep_prefix + keep_suffix >= len(s):
        # masking would reveal everything: mask middle only
        if len(s) <= 2:
            return policy.mask_char * len(s)
        keep_prefix = min(keep_prefix, 1)
        keep_suffix = min(keep_suffix, 1)
    chars = list(s)
    for i in range(keep_prefix, len(chars) - keep_suffix):
        chars[i] = policy.mask_char
    return "".join(chars)


def _generalize(value: Any, policy: ColumnPolicy, *, column: str) -> Any:
    if value is None:
        return None
    fmt = policy.generalize_format or "redact"
    s = str(value)

    if fmt == "year":
        return s[:4] if len(s) >= 4 and s[:4].isdigit() else REDACTED_MARKER
    if fmt == "month":
        return s[:7] if len(s) >= 7 else s[:4]
    if fmt == "day":
        return s[:10] if len(s) >= 10 else s
    if fmt.startswith("age_band"):
        width = int(fmt.rsplit("_", 1)[-1]) if fmt.rsplit("_", 1)[-1].isdigit() else 10
        try:
            age = int(float(s))
            lo = (age // width) * width
            return f"{lo}-{lo + width - 1}"
        except ValueError:
            return REDACTED_MARKER
    if fmt == "city":
        return REDACTED_MARKER  # generalization needs a lookup; fail closed
    if fmt == "first_digit":
        return s[0] + "*" * max(len(s) - 1, 0)
    return REDACTED_MARKER
