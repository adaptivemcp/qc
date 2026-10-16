"""Parquet writer (SPEC 10.1) — optional, behind the ``parquet`` extra.

Column types are mapped **explicitly** from the decoded values' Python types
rather than left to pyarrow's inference, so ``Decimal`` keeps its precision
(``decimal128``), ``bytes`` stays binary, and ``date``/``datetime`` become real
temporal types instead of strings — a Parquet reader then gets the same logical
types the profiler already worked out, with no CSV/JSONL re-parsing.

Requires ``pyarrow``: ``pip install 'querycleaner[parquet]'``.

**Known limitation.** Types are mapped from the decoded values, not from the
profile — the export path does not surface a column-type schema yet. When a
policy redacts every value in a column (the common ``strict-local`` case), that
column is all-``None`` and therefore typed ``null``. Wiring the profile's
logical types through the export path would give such columns their real type;
tracked as a follow-up.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any


def _pyarrow() -> Any:
    try:
        import pyarrow as pa
    except ImportError as error:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "Parquet export needs pyarrow. Install it with: pip install 'querycleaner[parquet]'"
        ) from error
    return pa


def _decimal_type(values: Sequence[Decimal], pa: Any) -> Any:
    """``decimal128`` wide enough for every value: precision from the widest
    digit count + scale seen, so no value is truncated."""
    scale = 0
    digits = 1
    for value in values:
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and exponent < 0:
            scale = max(scale, -exponent)
        digits = max(digits, len(value.as_tuple().digits))
    precision = min(38, max(1, digits + scale))
    return pa.decimal128(precision, min(scale, precision))


def _column_type(values: Sequence[Any], pa: Any) -> Any:
    """The Arrow type for a column, from its non-null Python values."""
    kinds = {type(v) for v in values}
    if not kinds:
        return pa.null()
    if kinds <= {bool}:
        return pa.bool_()
    if kinds <= {int}:
        return pa.int64()
    if kinds <= {float, int}:
        return pa.float64()
    if kinds <= {Decimal}:
        return _decimal_type([v for v in values if isinstance(v, Decimal)], pa)
    if kinds <= {datetime}:
        return pa.timestamp("us")
    if kinds <= {date}:
        return pa.date32()
    if kinds <= {time}:
        return pa.time64("us")
    if kinds <= {bytes, bytearray}:
        return pa.binary()
    return pa.string()  # mixed or unsupported -> text (never guess a wrong type)


def rows_to_table(
    rows: Iterable[dict[str, Any]],
    *,
    field_order: list[str] | None = None,
) -> Any:
    """Build a pyarrow Table with explicit per-column types."""
    pa = _pyarrow()
    records = list(rows)
    columns = list(field_order) if field_order else _field_order(records)
    arrays = {}
    for name in columns:
        values = [row.get(name) for row in records]
        present = [v for v in values if v is not None]
        arrow_type = _column_type(present, pa)
        if pa.types.is_string(arrow_type):
            values = [None if v is None else _as_text(v) for v in values]
        arrays[name] = pa.array(values, type=arrow_type)
    # A dict of {name: array} keeps the requested column order (Py 3.7+).
    return pa.table(arrays)


def _field_order(rows: list[dict[str, Any]]) -> list[str]:
    order: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                order.append(key)
    return order


def _as_text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        import base64

        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return str(value)


def write_parquet(
    path: Path,
    rows: Iterable[dict[str, Any]],
    *,
    field_order: list[str] | None = None,
) -> int:
    """Write rows to Parquet; returns the row count written."""
    import pyarrow.parquet as pq

    table = rows_to_table(rows, field_order=field_order)
    pq.write_table(table, path)
    return int(table.num_rows)


__all__ = ["rows_to_table", "write_parquet"]
