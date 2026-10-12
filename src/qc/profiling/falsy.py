"""Null and falsy-value analysis (SPEC 7.5).

Preserves every distinction the source can express. Raw and normalized
interpretations are both reported; sentinel values are only treated as
missing with evidence or user policy — never by default.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from qc.domain.models import FalsyStats

# String variants that *look* nullish but are not SQL NULL (SPEC 7.5)
_NULL_STRINGS = {"null", "none", "nil"}
_FALSE_STRINGS = {"false", "no", "n"}
_ZERO_STRINGS = {"0", "0.0", "00"}


@dataclass
class FalsyAnalysis:
    """Result of scanning one column's values for falsy variants."""

    stats: FalsyStats = field(default_factory=FalsyStats)
    raw_interpretation: dict[str, int] = field(default_factory=dict)
    normalized_interpretation: dict[str, int] = field(default_factory=dict)
    sentinel_candidates: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def analyze_falsy(
    values: list[Any],
    *,
    cell_kinds: dict[str, int] | None = None,
    row_count: int | None = None,
    exact: bool = True,
) -> FalsyAnalysis:
    """Classify null/falsy variants across observed values.

    Parameters
    ----------
    values:
        Observed column values (None entries are SQL NULL / missing).
    cell_kinds:
        Optional spreadsheet cell-kind counts keyed by kind
        ("blank", "formula_empty", "missing", ...).
    row_count:
        Total rows seen (for measurement designation).
    exact:
        False when values come from a sample (counts are then sample counts).
    """
    out = FalsyAnalysis()
    n = len(values)

    null_count = 0
    empty = 0
    ws = 0
    zero = 0
    false_c = 0
    str_false = 0
    str_zero = 0
    str_null = 0
    nan_c = 0
    sentinels: dict[str, int] = {}

    for v in values:
        if v is None:
            null_count += 1
            continue
        if isinstance(v, str):
            if v == "":
                empty += 1
            elif v.strip() == "":
                ws += 1
            elif v in _FALSE_STRINGS:
                str_false += 1
            elif v in _ZERO_STRINGS:
                str_zero += 1
            elif v.strip().lower() in _NULL_STRINGS:
                str_null += 1
            elif v in _SENTINEL_DISPLAY:
                key = f"str:{v}"
                sentinels[key] = sentinels.get(key, 0) + 1
            continue
        if isinstance(v, bool):
            if v is False:
                false_c += 1
            continue
        if isinstance(v, (int, float)):
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                nan_c += 1
            elif v == 0:
                zero += 1
            elif v in _SENTINEL_NUMBERS:
                key = f"num:{v}"
                sentinels[key] = sentinels.get(key, 0) + 1
            continue
        # other types: no falsy semantics beyond None

    measurement = "exact" if exact else "estimated"
    stats = FalsyStats(
        null_count=null_count,
        null_measurement=measurement,  # type: ignore[arg-type]
        empty_string_count=empty,
        whitespace_only_count=ws,
        zero_count=zero,
        false_count=false_c,
        string_false_count=str_false,
        string_zero_count=str_zero,
        string_null_count=str_null,
        nan_count=nan_c,
        sentinel_candidates={_hash_key(k): c for k, c in sentinels.items()},
    )
    if cell_kinds:
        stats.spreadsheet_blank_count = cell_kinds.get("blank") or cell_kinds.get("missing")
        stats.formula_empty_count = cell_kinds.get("formula_empty")

    out.stats = stats
    out.raw_interpretation = {
        "sql_null": null_count,
        "empty_string": empty,
        "whitespace_only": ws,
        "zero": zero,
        "false": false_c,
        "string_false": str_false,
        "string_zero": str_zero,
        "string_null": str_null,
        "nan_or_inf": nan_c,
        "rows_seen": row_count if row_count is not None else n,
    }
    # Normalized view: collapse to missing/not-missing for aggregate math
    seen = row_count if row_count is not None else n
    normalized_missing = null_count + (cell_kinds or {}).get("missing", 0)
    out.normalized_interpretation = {
        "missing": normalized_missing,
        "present": max(seen - normalized_missing, 0),
        "rows_seen": seen,
    }
    out.sentinel_candidates = sentinels
    if sentinels:
        out.notes.append(
            "sentinel-like values observed; NOT counted as missing "
            "(requires evidence or explicit policy)"
        )
    if str_null or str_false or str_zero:
        out.notes.append("string nullish variants present; kept distinct from SQL NULL")
    if nan_c:
        out.notes.append("NaN/Infinity observed; distinct from NULL")
    return out


_SENTINEL_NUMBERS = {-1, -9999, 9999, 99999, -999}
_SENTINEL_DISPLAY = {"N/A", "n/a", "unknown", "UNKNOWN", "-", "?", "none", "None", "NULL", "null"}


def _hash_key(raw: str) -> str:
    """Stable non-reversible key for sentinel display in artifacts (SPEC 5.4).

    Uses sha256 of the value with a fixed application salt — artifacts
    show only hashed sentinel keys + counts, never raw sample values.
    """
    import hashlib

    return hashlib.sha256(f"qc-sentinel:{raw}".encode()).hexdigest()[:16]
