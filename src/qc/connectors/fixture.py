"""JSONL and CSV fixture connectors (M1, internal testing only).

These connectors read local files as if they were datasets so the whole
profiling/sanitization/generation pipeline can run without a live
database. They are *not* advertised as user-facing sources.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from qc.connectors.base import (
    BaseConnector,
    ConnectorCapabilities,
    ProfileQuery,
    ProfileQueryResult,
    SampleRequest,
)
from qc.domain.enums import DatasetKind, LogicalType, Measurement
from qc.domain.errors import NotFoundError, SourceError, UnsupportedError
from qc.domain.models import (
    ColumnSchema,
    ConnectionTest,
    DatasetRef,
    DatasetSchema,
    EphemeralRow,
    SizeEstimate,
    SourceConfig,
)

_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _coerce(value: str) -> Any:
    """Best-effort JSON-ish coercion for CSV cells; keeps falsy distinctions."""
    if value == "":
        return ""  # empty string stays empty string (not None)
    if value == "null":
        return None
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if re.fullmatch(r"[+-]?\d+", value):
        try:
            return int(value)
        except ValueError:
            return value
    if re.fullmatch(r"[+-]?\d*\.\d+", value):
        try:
            return float(value)
        except ValueError:
            return value
    return value


class FixtureConnector(BaseConnector):
    """Reads ``*.jsonl`` and ``*.csv`` files under a configured root dir."""

    name = "fixture"
    capabilities = ConnectorCapabilities(
        metadata=True,
        aggregates=True,
        sampling=True,
        cancellation=True,
        writes=False,
        named=("metadata", "aggregates", "sampling", "cancellation"),
    )

    def __init__(self, root: Path | str) -> None:
        super().__init__()
        self.root = Path(root).expanduser().resolve()
        self._cache: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------- helpers

    def _resolve(self, dataset: DatasetRef) -> Path:
        """Resolve dataset name to a file under root; rejects traversal."""
        name = dataset.locator or dataset.qualified_name
        path = Path(name) if name.startswith("/") else (self.root / name).resolve()
        if not str(path).startswith(str(self.root)):
            raise SourceError("fixture path escapes configured root")
        if not path.is_file():
            raise NotFoundError(f"fixture dataset not found: {path.name}")
        return path

    def _load(self, path: Path) -> list[dict[str, Any]]:
        key = str(path)
        if key in self._cache:
            return self._cache[key]
        if path.suffix == ".jsonl":
            rows: list[dict[str, Any]] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise SourceError("jsonl fixture rows must be objects")
                rows.append(obj)
        elif path.suffix == ".csv":
            rows = []
            with path.open(encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for raw in reader:
                    rows.append({k: _coerce(v) for k, v in raw.items() if k is not None})
        else:
            raise UnsupportedError(f"unsupported fixture format: {path.suffix}")
        self._cache[key] = rows
        return rows

    @staticmethod
    def _columns(rows: list[dict[str, Any]]) -> list[str]:
        seen: dict[str, None] = {}
        for row in rows:
            for k in row:
                seen.setdefault(k)
        return list(seen)

    # ---------------------------------------------------------- DataConnector

    async def test_connection(self, config: SourceConfig) -> ConnectionTest:
        ok = self.root.is_dir()
        return ConnectionTest(
            ok=ok,
            message="fixture root found" if ok else "fixture root missing",
            capabilities=sorted(self.capabilities.named),
            error_code=None if ok else "SOURCE_UNAVAILABLE",
        )

    async def list_datasets(self, config: SourceConfig) -> list[DatasetRef]:
        if not self.root.is_dir():
            return []
        refs = []
        for p in sorted(self.root.rglob("*")):
            if p.suffix in (".jsonl", ".csv") and p.is_file():
                rel = p.relative_to(self.root).as_posix()
                refs.append(
                    DatasetRef(
                        source_id=config.id,
                        namespace=None,
                        name=rel,
                        kind=DatasetKind.TABLE,
                        locator=rel,
                    )
                )
        return refs

    async def get_schema(self, config: SourceConfig, dataset: DatasetRef) -> DatasetSchema:
        path = self._resolve(dataset)
        rows = self._load(path)
        names = self._columns(rows)
        fingerprint = _fingerprint(path, names, len(rows))
        columns = []
        for i, name in enumerate(names):
            logical = _infer_column_type(rows, name)
            columns.append(
                ColumnSchema(
                    name=name,
                    ordinal=i,
                    source_type=path.suffix.lstrip("."),
                    logical_type=logical,
                    nullable_declared=True,
                )
            )
        return DatasetSchema(
            dataset=dataset,
            source_fingerprint=fingerprint,
            columns=columns,
            row_estimate=len(rows),
            row_estimate_method=Measurement.EXACT,
            provider_metadata={"format": path.suffix.lstrip("."), "path": path.name},
        )

    async def estimate_size(self, config: SourceConfig, dataset: DatasetRef) -> SizeEstimate:
        path = self._resolve(dataset)
        rows = self._load(path)
        return SizeEstimate(
            row_estimate=len(rows),
            byte_estimate=path.stat().st_size,
            method=Measurement.EXACT,
            note="fixture file fully loaded",
        )

    async def execute_profile_query(
        self, config: SourceConfig, query: ProfileQuery
    ) -> ProfileQueryResult:
        path = self._resolve(query.dataset)
        rows = self._load(path)
        predicate = query.params.get("predicate")
        if predicate:
            from qc.domain.predicate import matches_row

            rows = [r for r in rows if matches_row(r, predicate)]
        kind = query.kind

        if kind == "row_count":
            return ProfileQueryResult(
                kind=kind, data={"row_count": len(rows)}, rows_returned=1, estimated=False
            )
        col: str = self._require_col(query.params.get("column"), rows)
        if kind == "null_count":
            null_counts = {
                "null_count": sum(1 for r in rows if r.get(col) is None),
                "empty_string_count": sum(1 for r in rows if r.get(col) == ""),
                "row_count": len(rows),
            }
            return ProfileQueryResult(kind=kind, data=null_counts, rows_returned=1)
        if kind == "distinct":
            vals = [r.get(col) for r in rows]
            # SQL COUNT(DISTINCT) semantics: NULLs excluded from the count
            return ProfileQueryResult(
                kind=kind,
                data={
                    "distinct_count": len({json_key(v) for v in vals if v is not None}),
                    "null_count": sum(1 for v in vals if v is None),
                    "row_count": len(vals),
                },
                rows_returned=1,
            )
        if kind == "min_max":
            nums: list[float] = []
            dates: list[str] = []
            for r in rows:
                v = r.get(col)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    nums.append(float(v))
                elif isinstance(v, str):
                    # numeric strings are common in JSONL fixtures; parse
                    # decimal-safe enough for min/max evidence
                    try:
                        nums.append(float(v))
                    except ValueError:
                        if re.match(r"^\d{4}-\d{2}-\d{2}", v):
                            dates.append(v)
            dates.sort()
            data: dict[str, Any] = {"row_count": len(rows)}
            if nums:
                lo, hi = min(nums), max(nums)
                data["min"] = str(int(lo)) if lo == int(lo) else str(lo)
                data["max"] = str(int(hi)) if hi == int(hi) else str(hi)
            if dates:
                data["min_str"] = dates[0]
                data["max_str"] = dates[-1]
            return ProfileQueryResult(kind=kind, data=data, rows_returned=1)
        if kind == "top_k":
            k = int(query.params.get("k", 10))
            # SQL GROUP BY semantics: NULLs form their own group and are
            # marked as a falsy variant; other values are returned raw so
            # downstream generation can sample them directly.
            cat_counts: dict[str, int] = {}
            falsy_keys: set[str] = set()
            for r in rows:
                v = r.get(col)
                if v is None:
                    key = json_key(None)
                elif isinstance(v, bool):
                    key = json_key(v)
                elif isinstance(v, (int, float)):
                    key = str(v)
                else:
                    key = str(v)
                if str(v) is None or v is None or isinstance(v, bool):
                    falsy_keys.add(key)
                cat_counts[key] = cat_counts.get(key, 0) + 1
            top = sorted(cat_counts.items(), key=lambda kv: -kv[1])[:k]
            return ProfileQueryResult(
                kind=kind,
                data={
                    "categories": [
                        {"key": k2, "count": c, "falsy": k2 in falsy_keys} for k2, c in top
                    ],
                    "distinct_count": len(cat_counts),
                },
                rows_returned=len(top),
            )
        if kind == "length_stats":
            lengths = [len(str(r.get(col))) for r in rows if r.get(col) is not None]
            if not lengths:
                return ProfileQueryResult(kind=kind, data={"row_count": len(rows)}, rows_returned=1)
            lengths.sort()
            data = {
                "min": lengths[0],
                "max": lengths[-1],
                "p50": lengths[len(lengths) // 2],
                "p90": lengths[int(len(lengths) * 0.9) - 1] if len(lengths) > 1 else lengths[0],
                "row_count": len(rows),
            }
            return ProfileQueryResult(kind=kind, data=data, rows_returned=1)
        if kind == "full_scan_values":
            # fixture connector can do full scans cheaply, but only under
            # an explicit full-scan permission flag from the planner
            return ProfileQueryResult(
                kind=kind,
                data={"values": [r.get(col) for r in rows]},
                rows_returned=len(rows),
            )
        raise UnsupportedError(f"fixture connector cannot run query kind '{kind}'")

    async def sample_rows(
        self, config: SourceConfig, request: SampleRequest
    ) -> AsyncIterator[EphemeralRow]:
        path = self._resolve(request.dataset)
        rows = self._load(path)
        if request.predicate:
            from qc.domain.predicate import matches_row

            rows = [r for r in rows if matches_row(r, request.predicate)]
        cols = request.columns or self._columns(rows)
        start = request.offset
        window = rows[start : start + request.limit]
        for i, row in enumerate(window):
            if request.cancel is not None and request.cancel.cancelled:
                request.cancel.raise_if_cancelled()
            yield EphemeralRow(
                values={c: row.get(c) for c in cols},
                row_number=start + i + 1,
            )
            # yield control so cancellation can interleave
            await asyncio.sleep(0)

    async def close(self) -> None:
        self._cache.clear()
        await super().close()

    @staticmethod
    def _require_col(col: Any, rows: list[dict[str, Any]]) -> str:
        if not isinstance(col, str) or not col:
            raise UnsupportedError("query kind requires 'column' param")
        if rows and col not in rows[0]:
            raise NotFoundError(f"column not found in fixture: {col}")
        return col


def json_key(v: Any) -> str:
    """Stable hashing for distinct-count with mixed types."""
    if v is None:
        return "\x00null"
    if isinstance(v, bool):
        return f"\x00bool:{v}"
    return f"\x00{type(v).__name__}:{v}"


def _fingerprint(path: Path, columns: list[str], rows: int) -> str:
    h = hashlib.sha256()
    h.update(path.name.encode())
    h.update("|".join(columns).encode())
    h.update(str(rows).encode())
    return f"fix_{h.hexdigest()[:24]}"


def _infer_column_type(rows: list[dict[str, Any]], name: str) -> LogicalType:
    from qc.profiling.type_inference import infer_logical_type

    values = [r.get(name) for r in rows]
    result = infer_logical_type(values)
    return result.logical_type
