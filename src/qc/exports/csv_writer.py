"""CSV writer with null encoding and formula-injection protection (SPEC 10.2)."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Formula-injection prefixes for spreadsheet-facing CSV (SPEC 10.2)
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any, *, protect_formulas: bool = True) -> str:
    """Render a cell: explicit null encoding + optional formula escaping."""
    if value is None:
        return "\\N"  # documented null sentinel (see manifest)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        import json

        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    if protect_formulas and text and text[0] in _FORMULA_PREFIXES:
        return "'" + text
    return text


def write_csv_rows(
    path: Path,
    rows: Iterable[dict[str, Any]],
    *,
    delimiter: str = ",",
    header: bool = True,
    protect_formulas: bool = True,
) -> int:
    """Write rows as UTF-8 CSV; returns row count.

    Manifest must record null encoding (``\\N``) and that formula
    protection was applied.
    """
    row_list = list(rows)
    fieldnames: list[str] = []
    for row in row_list:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(
            fh,
            delimiter=delimiter,
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        if header and fieldnames:
            writer.writerow(fieldnames)
        for row in row_list:
            writer.writerow(
                [csv_safe(row.get(k), protect_formulas=protect_formulas) for k in fieldnames]
            )
            count += 1
    return count
