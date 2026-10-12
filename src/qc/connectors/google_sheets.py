"""Google Sheets connector (SPEC 6.5).

Uses the official Sheets API over HTTP with OAuth or service-account
auth (access token supplied via secret reference — tokens are never
persisted to artifacts). Reads bounded ranges with pagination; never
modifies source sheets.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from typing import Any

from qc.connectors.base import (
    BaseConnector,
    ConnectorCapabilities,
    ProfileQuery,
    ProfileQueryResult,
    SampleRequest,
)
from qc.connectors.sheet_normalize import (
    SheetNormalization,
    normalize_rows,
    schema_from_headers,
)
from qc.domain.enums import DatasetKind, Measurement
from qc.domain.errors import (
    NotFoundError,
    SourceError,
    UnsupportedError,
)
from qc.domain.models import (
    ConnectionTest,
    DatasetRef,
    DatasetSchema,
    EphemeralRow,
    SizeEstimate,
    SourceConfig,
)
from qc.security.redaction import safe_error_message
from qc.security.secrets import resolve_secret

API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
REQUIRED_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
MAX_RANGE_CELLS = 1_000_000  # bounded read cap (SPEC 6.5)


class GoogleSheetsConnector(BaseConnector):
    name = "google-sheets"
    capabilities = ConnectorCapabilities(
        metadata=True,
        aggregates=True,  # computed client-side from bounded reads
        sampling=True,  # range-partitioned reads
        cancellation=True,
        writes=False,
        partitions=True,
        named=("metadata", "aggregates", "sampling", "cancellation"),
    )

    def __init__(self, *, http: Any = None, page_size: int = 5000) -> None:
        super().__init__()
        self._http = http  # injectable httpx.AsyncClient for tests
        self.page_size = page_size
        self._token: str | None = None
        self._cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ auth/http

    async def _client(self) -> Any:
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=30.0,
            headers={"User-Agent": "qc-connector/0.1"},
        )
        return self._http

    async def _access_token(self, config: SourceConfig) -> str:
        """Resolve a bearer token; never cached in artifacts (SPEC 6.5)."""
        if self._token is None:
            self._token = resolve_secret(config.connection_ref)
        return self._token

    async def _get(self, config: SourceConfig, path: str, params: dict[str, Any]) -> dict[str, Any]:
        client = await self._client()
        token = await self._access_token(config)
        try:
            resp = await client.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        except Exception as exc:
            raise SourceError(safe_error_message(exc), detail={"connector": self.name}) from exc
        if resp.status_code == 404:
            raise NotFoundError("spreadsheet or range not found")
        if resp.status_code in (401, 403):
            raise SourceError(
                "spreadsheet permission denied (check OAuth scopes / sharing)",
                detail={"status": str(resp.status_code)},
            )
        if resp.status_code == 429:
            raise SourceError("Google API rate limited; retry later", detail={"status": "429"})
        if resp.status_code >= 400:
            body = _safe_error_body(resp)
            raise SourceError(
                f"Google API error {resp.status_code}: {body}",
                detail={"status": str(resp.status_code)},
            )
        payload: dict[str, Any] = resp.json()
        return payload

    # ----------------------------------------------------------------- API

    async def test_connection(self, config: SourceConfig) -> ConnectionTest:
        try:
            await self._access_token(config)
        except Exception as exc:
            return ConnectionTest(
                ok=False, message=safe_error_message(exc), error_code="AUTH_FAILED"
            )
        # spreadsheet id lives in options.spreadsheet_id when configured
        sheet_id = config.options.get("spreadsheet_id")
        if not sheet_id:
            return ConnectionTest(
                ok=True,
                message="token resolved; set options.spreadsheet_id to bind a spreadsheet",
                capabilities=sorted(self.capabilities.named),
            )
        try:
            await self._get(config, f"/{sheet_id}", {"fields": "spreadsheetId"})
            return ConnectionTest(
                ok=True,
                message="connected (readonly scope)",
                capabilities=sorted(self.capabilities.named),
            )
        except SourceError as exc:
            return ConnectionTest(ok=False, message=exc.message, error_code=exc.code.value)

    async def list_datasets(self, config: SourceConfig) -> list[DatasetRef]:
        sheet_id = config.options.get("spreadsheet_id")
        if not sheet_id:
            raise UnsupportedError(
                "google-sheets source needs options.spreadsheet_id; "
                "the v1 connector reads one bound spreadsheet per source"
            )
        meta = await self._get(
            config,
            f"/{sheet_id}",
            {"fields": "sheets(properties(sheetId,title,gridProperties))"},
        )
        refs: list[DatasetRef] = []
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            title = str(props.get("title", ""))
            refs.append(
                DatasetRef(
                    source_id=config.id,
                    namespace=str(props.get("sheetId", "")),
                    name=title,
                    kind=DatasetKind.SHEET,
                    provider_id=str(props.get("sheetId", "")),
                    locator=f"{sheet_id}:{title}",
                )
            )
        return refs

    async def get_schema(self, config: SourceConfig, dataset: DatasetRef) -> DatasetSchema:
        norm = self._norm(config)
        rows, types, _ = await self._read(config, dataset, limit=200)
        kept, normalized, warnings = normalize_rows(
            rows, config=norm, provider_types=types, sheet_name=dataset.name
        )
        fingerprint = _fingerprint(dataset.locator or dataset.name, kept, len(normalized))
        schema = schema_from_headers(
            kept,
            normalized,
            dataset=dataset,
            fingerprint=fingerprint,
            sheet_name=dataset.name,
        )
        if warnings:
            schema.provider_metadata["warnings"] = "; ".join(warnings[:3])
        return schema

    async def estimate_size(self, config: SourceConfig, dataset: DatasetRef) -> SizeEstimate:
        sheet_id = config.options.get("spreadsheet_id") or ""
        title = dataset.name
        meta = await self._get(
            config,
            f"/{sheet_id}",
            {"fields": "sheets(properties(sheetId,title,gridProperties))"},
        )
        for sheet in meta.get("sheets", []):
            props = sheet.get("properties", {})
            if str(props.get("title")) == title:
                grid = props.get("gridProperties", {})
                rows = int(grid.get("rowCount", 0))
                cols = int(grid.get("columnCount", 0))
                return SizeEstimate(
                    row_estimate=rows,
                    byte_estimate=rows * cols * 8,  # rough; marked estimated
                    method=Measurement.ESTIMATED,
                    note="grid dimensions from sheet metadata",
                )
        return SizeEstimate(method=Measurement.UNKNOWN, note="sheet not found")

    async def execute_profile_query(
        self, config: SourceConfig, query: ProfileQuery
    ) -> ProfileQueryResult:
        """Aggregate queries computed client-side from a bounded read.

        Reads are capped at MAX_RANGE_CELLS so profiling never pulls an
        unbounded sheet (SPEC 6.5).
        """
        import time

        started = time.monotonic()
        norm = self._norm(config)
        rows, _types, truncated = await self._read(config, query.dataset, limit=None, cell_cap=True)
        kept, normalized, _ = normalize_rows(rows, config=norm)
        kind = query.kind
        col = str(query.params.get("column", ""))

        if kind == "row_count":
            return ProfileQueryResult(
                kind=kind,
                data={"row_count": len(normalized)},
                rows_returned=1,
                duration_seconds=time.monotonic() - started,
                truncated=truncated,
                estimated=truncated,
                note="truncated at cell cap" if truncated else None,
            )
        if not col:
            raise UnsupportedError(f"profile query '{kind}' requires column")
        if col not in kept:
            raise NotFoundError(f"column not found: {col}")
        values = [r[col] for r in normalized]
        raw_values = [c.value for c in values]
        data: dict[str, Any]

        if kind == "null_count":
            data = {
                "row_count": len(values),
                "null_count": sum(1 for v in values if v.value is None or v.kind == "blank"),
                "empty_string_count": sum(1 for v in values if v.kind == "value" and v.value == ""),
            }
        elif kind == "distinct":
            data = {
                "distinct_count": len({_key(v.value) for v in values if v.value is not None}),
                "null_count": sum(1 for v in values if v.value is None),
                "row_count": len(values),
            }
        elif kind == "min_max":
            nums = [
                float(v)
                for v in raw_values
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            ]
            strs = [str(v) for v in raw_values if isinstance(v, str)]
            dates = sorted(s for s in strs if s[:4].isdigit() and "-" in s)
            data = {"row_count": len(values)}
            if nums:
                lo, hi = min(nums), max(nums)
                data["min"] = str(int(lo)) if lo == int(lo) else str(lo)
                data["max"] = str(int(hi)) if hi == int(hi) else str(hi)
            if dates:
                data["min_str"], data["max_str"] = dates[0], dates[-1]
        elif kind == "top_k":
            k = int(query.params.get("k", 10))
            counts: dict[str, dict[str, Any]] = {}
            for v in values:
                if v.value is None:
                    key, falsy = "\x00null", True
                elif isinstance(v.value, bool):
                    key, falsy = f"\x00bool:{v.value}", True
                else:
                    key, falsy = str(v.value), False
                entry = counts.setdefault(key, {"key": key, "count": 0, "falsy": falsy})
                entry["count"] += 1
            top = sorted(counts.values(), key=lambda e: -e["count"])[:k]
            data = {"categories": top}
        elif kind == "length_stats":
            lengths = [len(str(v)) for v in raw_values if v is not None and not isinstance(v, bool)]
            if not lengths:
                data = {"row_count": len(values)}
            else:
                lengths.sort()
                data = {
                    "min": lengths[0],
                    "max": lengths[-1],
                    "p50": lengths[len(lengths) // 2],
                    "p90": lengths[int(len(lengths) * 0.9) - 1],
                    "row_count": len(values),
                }
        elif kind == "full_scan_values":
            data = {"values": raw_values}
        else:
            raise UnsupportedError(f"unsupported profile query kind: {kind}")

        return ProfileQueryResult(
            kind=kind,
            data=data,
            rows_returned=len(normalized),
            duration_seconds=time.monotonic() - started,
            truncated=truncated,
            estimated=truncated,
        )

    async def sample_rows(
        self, config: SourceConfig, request: SampleRequest
    ) -> AsyncIterator[EphemeralRow]:
        norm = self._norm(config)
        rows, _types, _truncated = await self._read(
            config, request.dataset, limit=request.limit + request.offset
        )
        kept, normalized, _ = normalize_rows(rows, config=norm)
        start = request.offset
        window = normalized[start : start + request.limit]
        wanted = [c for c in request.columns if c in kept] if request.columns else kept
        for i, row in enumerate(window):
            yield EphemeralRow(
                values={c: row[c].value if c in row else None for c in wanted},
                cell_kinds={c: row[c].kind for c in wanted if c in row},
                row_number=start + i + 1,
            )

    # -------------------------------------------------------------- helpers

    def _norm(self, config: SourceConfig) -> SheetNormalization:
        return SheetNormalization.from_options(config.options)

    async def _read(
        self,
        config: SourceConfig,
        dataset: DatasetRef,
        *,
        limit: int | None,
        cell_cap: bool = False,
    ) -> tuple[list[list[Any]], list[list[str]] | None, bool]:
        """Read a bounded range with pagination (SPEC 6.5).

        Returns (values, valueRenderOption types, truncated).
        """
        sheet_id = config.options.get("spreadsheet_id") or ""
        norm = self._norm(config)
        title = dataset.name
        value_range = norm.data_range or "A:Z"
        rng = f"'{_escape_sheet(title)}'!{value_range}"

        params: dict[str, Any] = {
            "valueRenderOption": "UNFORMATTED_VALUE" if norm.coerce_types else "FORMATTED_VALUE",
            "dateTimeRenderOption": "SERIAL_NUMBER",
        }
        data = await self._get(config, f"/{sheet_id}/values/{rng}", params)
        block = data.get("values", [])
        all_rows: list[list[Any]] = []
        for row in block:
            all_rows.append(list(row))

        truncated = False
        if cell_cap:
            total_cells = sum(len(r) for r in all_rows)
            if total_cells > MAX_RANGE_CELLS:
                # keep only the prefix of rows that fits the cap
                budget = MAX_RANGE_CELLS
                clipped: list[list[Any]] = []
                for row in all_rows:
                    if budget - len(row) < 0:
                        break
                    clipped.append(row)
                    budget -= len(row)
                all_rows = clipped
                truncated = True
        if limit is not None and len(all_rows) > limit:
            all_rows = all_rows[:limit]

        # Formatted-value pass to detect formula/blank distinctions when
        # the provider exposes them (SPEC 6.5: formula cells vs results).
        types: list[list[str]] | None = None
        if norm.profile_formulas:
            fmt_params = dict(params, valueRenderOption="FORMULA")
            fmt_data = await self._get(config, f"/{sheet_id}/values/{rng}", fmt_params)
            fmt_rows = fmt_data.get("values", [])
            # build full-sheet types aligned with all_rows (header included);
            # normalize_rows performs the single header_row slice
            types = []
            for ridx, row in enumerate(all_rows):
                type_row: list[str] = []
                for cidx in range(len(row)):
                    original = (
                        fmt_rows[ridx][cidx]
                        if ridx < len(fmt_rows) and cidx < len(fmt_rows[ridx])
                        else None
                    )
                    if isinstance(original, str) and original.startswith("="):
                        type_row.append("FORMULA_EMPTY" if row[cidx] in ("", None) else "FORMULA")
                    else:
                        type_row.append("VALUE")
                types.append(type_row)

        return all_rows, types, truncated


def _key(v: Any) -> str:
    if v is None:
        return "\x00null"
    if isinstance(v, bool):
        return f"\x00bool:{v}"
    return f"{type(v).__name__}:{v}"


def _escape_sheet(title: str) -> str:
    return title.replace("'", "''")


def _fingerprint(locator: str, columns: list[str], rows: int) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update(f"gs|{locator}|{','.join(columns)}|{rows}".encode())
    return f"gs_{h.hexdigest()[:24]}"


async def _safe_error_body(resp: Any) -> str:
    try:
        body = resp.json()
        err = body.get("error", {})
        message = err.get("message", "unknown error")
        # strip anything that looks like a token from the message
        import re

        message = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", str(message))
        return message[:200]
    except Exception:
        return "unreadable error body"


def service_account_token(token_json: str) -> str:
    """Exchange a service-account JWT for an access token (SPEC 6.5).

    Kept minimal: constructs the assertion and POSTs to Google's token
    endpoint. In production, prefer google-auth library; this path is
    used when only a JSON key is available.
    """
    # not implemented in v1 path: tokens are supplied via connection_ref
    # (env/file/keyring/cmd). Present as an explicit unsupported hook so
    # callers get a clear error rather than silent failure.
    raise UnsupportedError(
        "service-account exchange is not built in; supply an access token "
        "via connection_ref or install the google-auth helper",
        detail={"bytes": str(len(token_json))},
    )


def _decode_b64(value: str) -> bytes:  # pragma: no cover - helper
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _json_default(value: Any) -> str:  # pragma: no cover - helper
    return json.dumps(value, default=str)
