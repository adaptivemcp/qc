"""Logical type and format inference (SPEC 7.6).

Inference runs independently of declared source type and must retain
ambiguity: ``00123`` stays a string unless evidence supports integer
handling; ``03/04/2025`` records locale ambiguity rather than guessing.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from qc.domain.enums import Confidence, LogicalType

# --------------------------------------------------------------------- patterns

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
# Fictional-safe: loose phone shape, digits with separators, 7..17 digits
PHONE_RE = re.compile(r"^\+?[0-9][0-9\s().-]{5,20}[0-9]$")
URL_RE = re.compile(r"^(https?|ftp)://[^\s/$.?#][^\s]*$", re.IGNORECASE)
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
POSTAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{2,10}$")
INT_RE = re.compile(r"^[+-]?\d+$")
DECIMAL_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+)$")
# Common timestamps
DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S%z",
)
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d", "%d-%m-%Y", "%m-%d-%Y")
AMBIGUOUS_DATE_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-]\d{2,4}$")

# Leading-zero identifiers: keep as string by default (SPEC 7.6)
LEADING_ZERO_RE = re.compile(r"^0\d+$")

# Sentinel candidates (SPEC 7.5): observed, never assumed missing
SENTINEL_STRINGS = {"N/A", "n/a", "unknown", "UNKNOWN", "None", "null", "NULL", "-", "?"}
SENTINEL_NUMBERS = {-1, -9999, 9999, 99999, -999}


@dataclass
class TypeInferenceResult:
    logical_type: LogicalType
    confidence: Confidence
    reasons: list[str] = field(default_factory=list)
    ambiguous: bool = False
    format_ids: dict[str, str] = field(default_factory=dict)
    locale_hint: str | None = None
    notes: list[str] = field(default_factory=list)


def infer_logical_type(
    values: list[Any],
    *,
    declared_type: str | None = None,
    non_null_count: int | None = None,
) -> TypeInferenceResult:
    """Infer a logical type from observed non-null values.

    ``values`` must already exclude SQL NULL (None entries are treated as
    nulls and ignored). Strings are analyzed as-is.
    """
    samples = [v for v in values if v is not None]
    total = non_null_count if non_null_count is not None else len(samples)
    if not samples:
        return TypeInferenceResult(
            LogicalType.UNSUPPORTED,
            Confidence.UNKNOWN,
            reasons=["no non-null values observed"],
        )
    sample_fraction_note: str | None = None
    if total > len(samples):
        sample_fraction_note = f"type evidence covers {len(samples)}/{total} non-null values"

    counts: dict[LogicalType, int] = {}
    notes: list[str] = []
    ambiguous = False
    locale_hint: str | None = None
    leading_zero_seen = 0
    ambiguous_date_seen = 0

    for v in samples:
        t, note, flags = _classify_value(v)
        counts[t] = counts.get(t, 0) + 1
        if note:
            notes.append(note)
        if flags.get("leading_zero"):
            leading_zero_seen += 1
        if flags.get("ambiguous_date"):
            ambiguous_date_seen += 1

    n = len(samples)
    best_type, best_count = max(counts.items(), key=lambda kv: kv[1])
    ratio = best_count / n

    # A dominant type at >=95% with enough evidence is medium; >=99% high.
    if ratio >= 0.99 and n >= 20:
        conf = Confidence.HIGH
    elif ratio >= 0.95 and n >= 10:
        conf = Confidence.MEDIUM
    elif ratio >= 0.8 and n >= 5:
        conf = Confidence.LOW
        ambiguous = True
    else:
        conf = Confidence.UNKNOWN
        ambiguous = True

    reasons = [f"{best_count}/{n} values classify as {best_type.value}"]
    if sample_fraction_note:
        reasons.append(sample_fraction_note)

    # Leading zeros force string/identifier retention (SPEC 7.6)
    if leading_zero_seen and best_type in (LogicalType.INTEGER, LogicalType.DECIMAL):
        best_type = LogicalType.IDENTIFIER if leading_zero_seen == n else LogicalType.STRING
        conf = Confidence.MEDIUM
        ambiguous = True
        reasons.append(f"{leading_zero_seen} values have leading zeros; kept as string")

    # Integer-like with mostly leading zeros => identifier
    if leading_zero_seen and best_type == LogicalType.STRING and leading_zero_seen / n > 0.5:
        best_type = LogicalType.IDENTIFIER
        reasons.append("majority of values are zero-padded numeric: identifier-like")

    # Ambiguous day/month ordering
    if ambiguous_date_seen:
        ambiguous = True
        locale_hint = "dayfirst_ambiguous"
        notes.append(
            f"{ambiguous_date_seen} dates have ambiguous day/month order "
            "(e.g. 03/04/2025); locale required to disambiguate"
        )
        if best_type in (LogicalType.DATE, LogicalType.TIMESTAMP):
            conf = Confidence.LOW

    # Free text vs categorical: long average length + low repetition
    if best_type == LogicalType.STRING:
        strs = [str(v) for v in samples]
        avg_len = sum(len(s) for s in strs) / n
        distinct_ratio = len(set(strs)) / n
        numeric_like = all(INT_RE.match(s) or DECIMAL_RE.match(s) for s in strs)
        if avg_len > 60 and distinct_ratio > 0.9:
            best_type = LogicalType.FREE_TEXT
            reasons.append("long, highly distinct values: free text")
        elif numeric_like:
            # huge integer-like strings stay plain strings (not categorical):
            # they are numeric-shaped but unsafe to parse
            reasons.append("integer-like strings beyond safe numeric range: kept as string")
        elif distinct_ratio <= 0.5 and n >= 10:
            best_type = LogicalType.CATEGORICAL_STRING
            reasons.append("low cardinality: categorical string")

    # UUID / email upgrades from string
    if best_type in (LogicalType.STRING, LogicalType.IDENTIFIER):
        if all(UUID_RE.match(str(v)) for v in samples[: min(n, 50)]):
            best_type = LogicalType.UUID
            conf = Confidence.HIGH if n >= 20 else Confidence.MEDIUM
            reasons.append("all sampled values match UUID shape")
        elif all(EMAIL_RE.match(str(v)) for v in samples[: min(n, 50)]):
            best_type = LogicalType.EMAIL
            conf = Confidence.HIGH if n >= 20 else Confidence.MEDIUM
            reasons.append("all sampled values match email shape")

    # Declared-type corroboration (never overrides strong contrary evidence)
    if declared_type and conf in (Confidence.HIGH, Confidence.MEDIUM):
        reasons.append(f"declared source type: {declared_type}")
    elif declared_type:
        reasons.append(f"declared source type {declared_type} not corroborated")

    return TypeInferenceResult(
        logical_type=best_type,
        confidence=conf,
        reasons=reasons,
        ambiguous=ambiguous,
        locale_hint=locale_hint,
        notes=sorted(set(notes))[:5],
    )


def _classify_value(v: Any) -> tuple[LogicalType, str | None, dict[str, bool]]:
    """Classify a single observed value."""
    flags: dict[str, bool] = {}

    if isinstance(v, bool):
        return LogicalType.BOOLEAN, None, flags
    if isinstance(v, int):
        return LogicalType.INTEGER, None, flags
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return LogicalType.FLOAT, "non-finite float observed", flags
        return LogicalType.FLOAT, None, flags
    if isinstance(v, Decimal):
        return LogicalType.DECIMAL, None, flags
    if isinstance(v, datetime):
        return (
            LogicalType.TIMESTAMP_TZ if v.tzinfo is not None else LogicalType.TIMESTAMP,
            None,
            flags,
        )
    if isinstance(v, date):
        return LogicalType.DATE, None, flags
    if isinstance(v, (bytes, bytearray)):
        return LogicalType.BINARY, None, flags
    if isinstance(v, (list, dict)):
        return LogicalType.JSON, None, flags

    s = str(v)
    if LEADING_ZERO_RE.match(s):
        flags["leading_zero"] = True

    if s == "":
        # empty string is a falsy variant, not evidence of a type
        return LogicalType.STRING, "empty string excluded from type evidence", flags

    if UUID_RE.match(s):
        return LogicalType.UUID, None, flags
    if EMAIL_RE.match(s):
        return LogicalType.EMAIL, None, flags
    if URL_RE.match(s):
        return LogicalType.URL, None, flags
    try:
        ipaddress.ip_address(s)
        return LogicalType.IP_ADDRESS, None, flags
    except ValueError:
        pass

    if COUNTRY_RE.match(s):
        # two-letter codes are weak evidence alone; caller may see categorical
        pass

    # JSON-looking strings
    if s[:1] in "[{" and s[-1:] in "]}":
        try:
            json.loads(s)
            return LogicalType.JSON, None, flags
        except (json.JSONDecodeError, ValueError):
            pass

    if INT_RE.match(s):
        # Guard: huge integers as strings stay strings to avoid precision loss
        if len(s.lstrip("+-")) > 18:
            return (
                LogicalType.STRING,
                "integer-like string beyond safe range; kept as string",
                flags,
            )
        return LogicalType.INTEGER, None, flags
    if DECIMAL_RE.match(s):
        return LogicalType.DECIMAL, None, flags

    m = AMBIGUOUS_DATE_RE.match(s)
    if m:
        flags["ambiguous_date"] = True

    for fmt in DATETIME_FORMATS:
        try:
            datetime.strptime(s, fmt)
            return LogicalType.TIMESTAMP, None, flags
        except ValueError:
            continue
    for fmt in DATE_FORMATS:
        try:
            datetime.strptime(s, fmt)
            return LogicalType.DATE, None, flags
        except ValueError:
            continue

    if PHONE_RE.match(s) and sum(c.isdigit() for c in s) >= 7:
        return LogicalType.PHONE, None, flags
    if s in SENTINEL_STRINGS:
        return LogicalType.STRING, "sentinel-like string observed", flags

    return LogicalType.STRING, None, flags


# ------------------------------------------------------------------ formats

_FORMAT_BUILDERS: list[tuple[str, re.Pattern[str], str]] = []


def _fmt(fid: str, regex: str, desc: str) -> None:
    _FORMAT_BUILDERS.append((fid, re.compile(regex), desc))


_fmt("email", r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", "email address")
_fmt("uuid", r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", "UUID")
_fmt("iso_date", r"\d{4}-\d{2}-\d{2}", "ISO date")
_fmt(
    "iso_datetime",
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?",
    "ISO datetime",
)
_fmt("slash_date", r"\d{1,2}/\d{1,2}/\d{2,4}", "slash date (ambiguous locale)")
_fmt("int", r"[+-]?\d+", "integer")
_fmt("decimal", r"[+-]?\d+\.\d+", "decimal")
_fmt("zero_padded", r"0\d+", "zero-padded identifier")
_fmt("url", r"https?://\S+", "URL")
_fmt("ipv4", r"\b\d{1,3}(\.\d{1,3}){3}\b", "IPv4")
_fmt("phone", r"\+?[\d][\d\s().-]{5,17}\d", "phone-like")
_fmt("alpha_words", r"[A-Za-z]+( [A-Za-z]+)+", "multi-word text")
_fmt("alpha", r"[A-Za-z]+", "letters")
_fmt("alphanum", r"[A-Za-z0-9]+", "alphanumeric")

# Generalization: replace digit runs so patterns are comparable
_DIGIT_RUN = re.compile(r"\d+")


def normalize_pattern(value: str) -> str:
    """Reduce a value to a comparable format skeleton.

    Digit *runs* keep their length (``00123`` → ``99999``) so shapes can
    be regenerated without losing cardinality information.
    """
    out = value
    for _fid, regex, _d in _FORMAT_BUILDERS:
        if regex.fullmatch(out):
            return _DIGIT_RUN.sub(lambda m: "9" * len(m.group()), out).lower()
    return _DIGIT_RUN.sub(lambda m: "9" * len(m.group()), out)[:64].lower()


def detect_format_patterns(
    values: list[str],
    *,
    min_occurrences: int = 3,
    min_coverage: float = 0.05,
) -> list[tuple[str, str, int, float]]:
    """Return [(pattern_id, normalized, count, coverage)] sorted by frequency."""
    non_empty = [v for v in values if v]
    if not non_empty:
        return []
    counts: dict[str, int] = {}
    for v in non_empty:
        key = normalize_pattern(v)
        counts[key] = counts.get(key, 0) + 1
    n = len(non_empty)
    result = []
    for key, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        if count < min_occurrences:
            continue
        cov = count / n
        if cov < min_coverage:
            continue
        result.append((f"fmt_{abs(hash(key)) % 10**8:08d}", key, count, cov))
    return result


def try_parse_decimal(v: Any) -> Decimal | None:
    """Decimal-safe parse used by numeric summaries (no float round-trip)."""
    try:
        if isinstance(v, Decimal):
            return v
        if isinstance(v, float):
            return Decimal(str(v))
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
