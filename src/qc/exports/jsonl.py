"""JSONL writer with precision-safe scalar serialization (SPEC 10.1).

One record per line, UTF-8, JSON ``null`` preserved, stable field order
when a schema order is provided. Decimals are emitted as JSON numbers
only when they round-trip exactly; otherwise they are emitted as strings
(lossless) and the manifest records the fallback.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any


def json_scalar(value: Any) -> Any:
    """Convert a decoded value to something ``json.dumps`` can emit safely."""
    if isinstance(value, Decimal):
        # exact integer -> JSON number
        if value == value.to_integral_value():
            return int(value)
        # otherwise: keep as number only if float round-trips exactly
        as_float = float(value)
        if Decimal(str(as_float)) == value:
            return as_float
        return str(value)  # lossless fallback
    if isinstance(value, (bytes, bytearray)):
        # binary is not JSON-representable: base64 text, marked by the schema
        import base64

        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, set):
        return sorted(json_scalar(v) for v in value)
    if isinstance(value, dict):
        return {str(k): json_scalar(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_scalar(v) for v in value]
    return value


def to_json_line(row: dict[str, Any], *, field_order: list[str] | None = None) -> str:
    """Serialize one row with optional stable field ordering."""
    if field_order:
        ordered = {k: json_scalar(row[k]) for k in field_order if k in row}
        for k, v in row.items():
            if k not in ordered:
                ordered[k] = json_scalar(v)
    else:
        ordered = {k: json_scalar(v) for k, v in row.items()}
    return json.dumps(ordered, ensure_ascii=False)


def write_jsonl(
    path: Path,
    rows: Iterable[dict[str, Any]],
    *,
    field_order: list[str] | None = None,
) -> int:
    """Write records, one per line; returns the count written."""
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(to_json_line(row, field_order=field_order) + "\n")
            count += 1
    return count


__all__ = ["json_scalar", "to_json_line", "write_jsonl"]
