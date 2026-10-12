"""Spreadsheet normalization shared by Google/Lark adapters (SPEC 6.7).

Spreadsheets have weak schemas; the user configures header row, data
range, skipped columns, locale, formula handling, merged-cell policy,
blank-row termination, and duplicate-header resolution. Automatic
inference preserves the original cell representation alongside the
normalized value, and every falsy variant stays distinguishable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from qc.domain.enums import LogicalType, Measurement
from qc.domain.errors import InvalidArgumentsError, ValidationError
from qc.domain.models import ColumnSchema, DatasetRef, DatasetSchema


@dataclass
class SheetNormalization:
    """User-configurable normalization options (SPEC 6.7)."""

    header_row: int = 0  # 0-based index within the read range
    data_range: str | None = None  # explicit A1 range; None = used range
    skip_columns: list[str] = field(default_factory=list)
    locale: str = "en_US"
    profile_formulas: bool = False  # False = profile displayed/evaluated values
    merged_cells: str = "reject"  # reject | expand | missing
    stop_on_blank_row: bool = False
    duplicate_headers: str = "suffix"  # suffix | error | first
    coerce_types: bool = True

    @classmethod
    def from_options(cls, options: dict[str, str]) -> SheetNormalization:
        """Build from SourceConfig.options strings, with validation."""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs: dict[str, Any] = {}
        for key, value in options.items():
            if key.startswith("sheet_"):
                key = key[6:]
            if key not in known:
                continue
            if key == "header_row":
                kwargs[key] = int(value)
            elif key == "skip_columns":
                kwargs[key] = [c.strip() for c in value.split(",") if c.strip()]
            elif key in ("profile_formulas", "stop_on_blank_row", "coerce_types"):
                kwargs[key] = value.lower() in ("1", "true", "yes")
            else:
                kwargs[key] = value
        cfg = cls(**kwargs)
        if cfg.merged_cells not in ("reject", "expand", "missing"):
            raise InvalidArgumentsError("merged_cells must be reject|expand|missing")
        if cfg.duplicate_headers not in ("suffix", "error", "first"):
            raise InvalidArgumentsError("duplicate_headers must be suffix|error|first")
        if cfg.header_row < 0:
            raise InvalidArgumentsError("header_row must be >= 0")
        return cfg


# Cell kind markers preserved through normalization (SPEC 6.7)
KIND_BLANK = "blank"  # empty cell in an existing row
KIND_MISSING = "missing"  # cell index beyond row length
KIND_FORMULA_EMPTY = "formula_empty"
KIND_FORMULA = "formula"
KIND_VALUE = "value"


@dataclass
class NormalizedCell:
    """Original representation + normalized value (SPEC 6.7)."""

    value: Any
    kind: str = KIND_VALUE
    raw_type: str = "string"  # provider type name
    original: Any = None  # untouched provider representation

    @property
    def is_falsy_variant(self) -> bool:
        return self.kind in (KIND_BLANK, KIND_MISSING, KIND_FORMULA_EMPTY)


def normalize_rows(
    provider_rows: list[list[Any]],
    *,
    config: SheetNormalization,
    provider_types: list[list[str]] | None = None,
    sheet_name: str = "Sheet1",
) -> tuple[list[str], list[dict[str, NormalizedCell]], list[str]]:
    """Turn provider rows into (headers, normalized rows, warnings).

    ``provider_rows`` are raw cell values as the provider returns them.
    ``provider_types`` optionally carries per-cell type names (e.g.
    Google's ``FORMULA_VALUE``).
    """
    warnings: list[str] = []
    if not provider_rows:
        return [], [], ["sheet is empty"]

    rows = provider_rows
    if config.header_row >= len(rows):
        raise ValidationError(f"header_row {config.header_row} outside sheet with {len(rows)} rows")

    raw_headers = [str(h) if h is not None else "" for h in rows[config.header_row]]
    headers = _resolve_headers(raw_headers, config, warnings)
    data = rows[config.header_row + 1 :]

    # blank-row termination
    if config.stop_on_blank_row:
        for i, row in enumerate(data):
            if _row_is_blank(row):
                data = data[:i]
                warnings.append(f"stopped at blank row {i + config.header_row + 2}")
                break

    # provider_types aligns with provider_rows (header row included);
    # slicing happens exactly here — callers must pass full-sheet types.
    types = provider_types[config.header_row + 1 :] if provider_types else None

    normalized: list[dict[str, NormalizedCell]] = []
    for ridx, row in enumerate(data):
        if _row_is_blank(row) and config.stop_on_blank_row:
            continue
        out: dict[str, NormalizedCell] = {}
        for cidx, header in enumerate(headers):
            if header in config.skip_columns:
                continue
            original = row[cidx] if cidx < len(row) else None
            ptype = (
                types[ridx][cidx]
                if types and ridx < len(types) and cidx < len(types[ridx])
                else None
            )
            out[header] = _normalize_cell(original, ptype, config)
        normalized.append(out)

    # merged-cell policy: providers often repeat or blank merged values
    if config.merged_cells == "reject" and _has_merged_artifacts(provider_rows):
        warnings.append(
            "merged cells detected; policy 'reject' means merged spans are "
            "not expanded — values may appear only in the top-left cell"
        )

    kept = [h for h in headers if h not in config.skip_columns]
    return kept, normalized, warnings


def _resolve_headers(raw: list[str], config: SheetNormalization, warnings: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for idx, name in enumerate(raw):
        name = name.strip()
        if not name:
            name = f"column_{idx}"
            warnings.append(f"blank header at ordinal {idx} named {name}")
        if name in seen:
            seen[name] += 1
            if config.duplicate_headers == "error":
                raise ValidationError(f"duplicate header '{name}' in sheet")
            if config.duplicate_headers == "first":
                out.append(f"{name}__dup{idx}")
                warnings.append(f"duplicate header '{name}' kept first occurrence")
                continue
            name = f"{name}_{seen[name]}"  # occurrence-numbered suffix (id_2, id_3)
            warnings.append(f"duplicate header suffixed to '{name}'")
        else:
            seen[name] = 1
        out.append(name)
    return out


def _row_is_blank(row: list[Any]) -> bool:
    return all(v is None or (isinstance(v, str) and not v.strip()) for v in row)


def _has_merged_artifacts(rows: list[list[Any]]) -> bool:
    # heuristic: consecutive identical non-empty strings across a row span
    # are indistinguishable from merged repeats at the value level; the
    # provider's merge metadata is consulted by adapters instead.
    return False


def _normalize_cell(
    original: Any, provider_type: str | None, config: SheetNormalization
) -> NormalizedCell:
    """Preserve falsy distinctions while producing a normalized value."""
    ptype = (provider_type or "").upper()

    if ptype in ("FORMULA",) or (
        isinstance(original, str) and original.startswith("=") and config.profile_formulas
    ):
        # formula cells: profile the formula text only when configured
        return NormalizedCell(
            value=original,
            kind=KIND_FORMULA,
            raw_type=ptype or "formula",
            original=original,
        )
    if ptype == "FORMULA_EMPTY":
        return NormalizedCell(value="", kind=KIND_FORMULA_EMPTY, raw_type=ptype, original=original)

    if original is None:
        return NormalizedCell(value=None, kind=KIND_BLANK, raw_type="blank", original=None)
    if isinstance(original, str):
        if original == "":
            # distinguish "empty string in cell" from "no cell"
            return NormalizedCell(value="", kind=KIND_VALUE, raw_type="string", original=original)
        if not config.coerce_types:
            return NormalizedCell(
                value=original, kind=KIND_VALUE, raw_type="string", original=original
            )
        return NormalizedCell(
            value=_coerce_string(original, config.locale),
            kind=KIND_VALUE,
            raw_type="string",
            original=original,
        )
    # numbers/bools pass through untouched
    return NormalizedCell(
        value=original,
        kind=KIND_VALUE,
        raw_type=type(original).__name__,
        original=original,
    )


def _coerce_string(value: str, locale: str) -> Any:
    """Locale-aware coercion for numbers/booleans only; dates stay strings.

    Dates are intentionally NOT coerced here: ambiguity (03/04/2025) is
    the profile's job (SPEC 7.6), and coercion would destroy evidence.
    """
    stripped = value.strip()
    if stripped in ("TRUE", "true", "FALSE", "false"):
        return stripped.lower() == "true"
    if stripped and stripped.lstrip("+-").replace(".", "", 1).isdigit():
        # Leading zeros are meaningful identifiers (SPEC 7.6): "00123"
        # must never become the integer 123.
        digits = stripped.lstrip("+-")
        if len(digits) > 1 and digits.startswith("0"):
            return value
        try:
            if "." not in stripped:
                return int(stripped)
            return float(stripped)
        except ValueError:
            return value
    # thousands separators for common locales
    if locale.startswith(("de", "fr", "nl")) and "," in stripped:
        candidate = stripped.replace(".", "").replace(",", ".")
        try:
            return float(candidate)
        except ValueError:
            return value
    return value


def schema_from_headers(
    headers: list[str],
    rows: list[dict[str, NormalizedCell]],
    *,
    dataset: DatasetRef,
    fingerprint: str,
    sheet_name: str,
) -> DatasetSchema:
    """Build a DatasetSchema from normalized sheet data (SPEC 6.7)."""
    columns: list[ColumnSchema] = []
    for idx, name in enumerate(headers):
        values = [r[name] for r in rows if name in r]
        logical = _infer_logical(values)
        nullable = any(v.is_falsy_variant or v.value is None for v in values)
        columns.append(
            ColumnSchema(
                name=name,
                ordinal=idx,
                source_type="sheet_cell",
                logical_type=logical,
                nullable_declared=nullable,
            )
        )
    return DatasetSchema(
        dataset=dataset,
        source_fingerprint=fingerprint,
        columns=columns,
        row_estimate=len(rows),
        row_estimate_method=Measurement.EXACT,
        provider_metadata={"sheet": sheet_name},
    )


def _infer_logical(values: list[NormalizedCell]) -> LogicalType:
    non_null = [v.value for v in values if v.value is not None and not v.is_falsy_variant]
    if not non_null:
        return LogicalType.UNSUPPORTED
    from qc.profiling.type_inference import infer_logical_type

    return infer_logical_type(non_null).logical_type


def used_range(rows: list[list[Any]]) -> str:
    """Compute an A1-style used range from raw rows."""
    if not rows:
        return "A1:A1"
    ncols = max((len(r) for r in rows), default=0)
    return f"A1:{_col_letter(ncols)}{len(rows)}"


def _col_letter(n: int) -> str:
    """1 -> A, 27 -> AA."""
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out or "A"
