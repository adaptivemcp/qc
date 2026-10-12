"""Lark Sheets connector (SPEC 6.6).

Uses the Lark Open Platform Sheets API. App scopes and tenant/user
authorization modes are documented in docs/connectors.md. Provider IDs
stay separate from display names; explicit range allowlists are
supported; source sheets are never modified.
"""

from __future__ import annotations

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
from qc.domain.errors import NotFoundError, SourceError, UnsupportedError
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

LARK_BASE = "https://open.larksuite.com/open-apis"
FEISHU_BASE = "https://open.feishu.cn/open-apis"
# sheets:read scope; exact scope names are deployment-specific (documented)
DEFAULT_SCOPE = "sheets:spreadsheet:readonly"
MAX_RANGE_CELLS = 1_000_000


class LarkSheetsConnector(BaseConnector):
    name = "lark-sheets"
    capabilities = ConnectorCapabilities(
        metadata=True,
        aggregates=True,
        sampling=True,
        cancellation=True,
        writes=False,
        partitions=True,
        named=("metadata", "aggregates", "sampling", "cancellation"),
    )

    def __init__(self, *, http: Any = None, base_url: str | None = None) -> None:
        super().__init__()
        self._http = http
        self.base_url = base_url or LARK_BASE
        self._token: str | None = None
        self._domain: str | None = None  # provider id, not display name

    # -------------------------------------------------------------- transport

    async def _client(self) -> Any:
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=30.0,
            headers={"User-Agent": "qc-connector/0.1"},
        )
        return self._http

    async def _token_value(self, config: SourceConfig) -> str:
        """Tenant/user access token via secret reference (never persisted)."""
        if self._token is None:
            self._token = resolve_secret(config.connection_ref)
        return self._token

    async def _get(
        self, config: SourceConfig, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        client = await self._client()
        token = await self._token_value(config)
        try:
            resp = await client.get(
                path,
                params=params or {},
                headers={"Authorization": f"Bearer {token}"},
            )
        except Exception as exc:
            raise SourceError(safe_error_message(exc), detail={"connector": self.name}) from exc
        return self._handle(resp)

    async def _post(self, config: SourceConfig, path: str, body: dict[str, Any]) -> dict[str, Any]:
        client = await self._client()
        token = await self._token_value(config)
        try:
            resp = await client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
        except Exception as exc:
            raise SourceError(safe_error_message(exc), detail={"connector": self.name}) from exc
        return self._handle(resp)

    def _handle(self, resp: Any) -> dict[str, Any]:
        if resp.status_code in (401, 403):
            raise SourceError(
                "Lark permission denied (check app scopes / tenant authorization)",
                detail={"status": str(resp.status_code)},
            )
        if resp.status_code == 429:
            # Lark rate limits: caller applies backoff (SPEC 6.6)
            raise SourceError("Lark API rate limited", detail={"status": "429"})
        if resp.status_code >= 400:
            raise SourceError(
                f"Lark API error {resp.status_code}", detail={"status": str(resp.status_code)}
            )
        payload = resp.json()
        code = payload.get("code", 0)
        if code != 0:
            # Lark signals business errors in-body with code/msg
            msg = str(payload.get("msg", "unknown error"))[:200]
            if code in (99991663, 99991661):  # token invalid/expired
                raise SourceError("Lark token invalid or expired", detail={"code": str(code)})
            if code == 1254045:
                raise NotFoundError("Lark spreadsheet or sheet not found")
            raise SourceError(f"Lark error {code}: {msg}", detail={"code": str(code)})
        body: dict[str, Any] = payload.get("data", {}) or {}
        return body

    # ------------------------------------------------------------------ API

    async def test_connection(self, config: SourceConfig) -> ConnectionTest:
        try:
            await self._token_value(config)
        except Exception as exc:
            return ConnectionTest(
                ok=False, message=safe_error_message(exc), error_code="AUTH_FAILED"
            )
        spreadsheet_id = config.options.get("spreadsheet_id")
        if not spreadsheet_id:
            return ConnectionTest(
                ok=True,
                message="token resolved; set options.spreadsheet_id to bind a spreadsheet",
                capabilities=sorted(self.capabilities.named),
            )
        try:
            await self._get(
                config,
                f"/sheets/v3/spreadsheets/{spreadsheet_id}",
                {"user_id_type": "open_id"},
            )
            return ConnectionTest(
                ok=True,
                message="connected (readonly)",
                capabilities=sorted(self.capabilities.named),
            )
        except SourceError as exc:
            return ConnectionTest(ok=False, message=exc.message, error_code=exc.code.value)

    async def list_datasets(self, config: SourceConfig) -> list[DatasetRef]:
        spreadsheet_id = config.options.get("spreadsheet_id")
        if not spreadsheet_id:
            raise UnsupportedError(
                "lark-sheets source needs options.spreadsheet_id; "
                "v1 binds one spreadsheet per source"
            )
        data = await self._get(
            config,
            f"/sheets/v3/spreadsheets/{spreadsheet_id}/sheets/query",
            {"user_id_type": "open_id"},
        )
        refs: list[DatasetRef] = []
        for sheet in data.get("sheets", []):
            props = sheet.get("sheet_id") or sheet.get("title")
            title = str(sheet.get("title", ""))
            refs.append(
                DatasetRef(
                    source_id=config.id,
                    # provider id kept separate from display name (SPEC 6.6)
                    namespace=None,
                    name=title,
                    kind=DatasetKind.SHEET,
                    provider_id=str(sheet.get("sheet_id", "")),
                    locator=f"{spreadsheet_id}:{sheet.get('sheet_id', '')}",
                )
            )
            del props
        return refs

    async def get_schema(self, config: SourceConfig, dataset: DatasetRef) -> DatasetSchema:
        norm = self._norm(config)
        rows, _ = await self._read(config, dataset, limit=200)
        kept, normalized, warnings = normalize_rows(rows, config=norm, sheet_name=dataset.name)
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
        spreadsheet_id = config.options.get("spreadsheet_id") or ""
        provider_id = dataset.provider_id or ""
        data = await self._get(
            config,
            f"/sheets/v3/spreadsheets/{spreadsheet_id}/sheets/query",
            {"user_id_type": "open_id"},
        )
        for sheet in data.get("sheets", []):
            if str(sheet.get("sheet_id")) == provider_id:
                grid = sheet.get("grid_properties") or sheet.get("gridProperties") or {}
                rows = int(grid.get("row_count", 0) or 0)
                cols = int(grid.get("column_count", 0) or 0)
                return SizeEstimate(
                    row_estimate=rows,
                    byte_estimate=rows * cols * 8,
                    method=Measurement.ESTIMATED,
                    note="grid properties from Lark sheet metadata",
                )
        return SizeEstimate(method=Measurement.UNKNOWN, note="sheet not found")

    async def execute_profile_query(
        self, config: SourceConfig, query: ProfileQuery
    ) -> ProfileQueryResult:
        import time

        started = time.monotonic()
        norm = self._norm(config)
        rows, truncated = await self._read(config, query.dataset, cell_cap=True)
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
            )
        if not col:
            raise UnsupportedError(f"profile query '{kind}' requires column")
        if col not in kept:
            raise NotFoundError(f"column not found: {col}")
        values = [r[col] for r in normalized]
        raw = [c.value for c in values]
        data: dict[str, Any]

        if kind == "null_count":
            data = {
                "row_count": len(values),
                "null_count": sum(1 for v in values if v.value is None or v.kind == "blank"),
                "empty_string_count": sum(1 for v in values if v.kind == "value" and v.value == ""),
            }
        elif kind == "distinct":
            data = {
                "distinct_count": len({_k(v.value) for v in values if v.value is not None}),
                "null_count": sum(1 for v in values if v.value is None),
                "row_count": len(values),
            }
        elif kind == "min_max":
            nums = [
                float(v) for v in raw if isinstance(v, (int, float)) and not isinstance(v, bool)
            ]
            dates = sorted(
                str(v) for v in raw if isinstance(v, str) and v[:4].isdigit() and "-" in v
            )
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
            data = {"categories": sorted(counts.values(), key=lambda e: -e["count"])[:k]}
        elif kind == "length_stats":
            lengths = [len(str(v)) for v in raw if v is not None and not isinstance(v, bool)]
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
            data = {"values": raw}
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
        rows, _ = await self._read(config, request.dataset, limit=request.limit + request.offset)
        kept, normalized, _ = normalize_rows(rows, config=norm)
        window = normalized[request.offset : request.offset + request.limit]
        wanted = [c for c in request.columns if c in kept] if request.columns else kept
        for i, row in enumerate(window):
            yield EphemeralRow(
                values={c: row[c].value if c in row else None for c in wanted},
                cell_kinds={c: row[c].kind for c in wanted if c in row},
                row_number=request.offset + i + 1,
            )

    # -------------------------------------------------------------- helpers

    def _norm(self, config: SourceConfig) -> SheetNormalization:
        return SheetNormalization.from_options(config.options)

    async def _read(
        self,
        config: SourceConfig,
        dataset: DatasetRef,
        *,
        limit: int | None = None,
        cell_cap: bool = False,
    ) -> tuple[list[list[Any]], bool]:
        """Read a bounded range via the v2 values API with pagination."""
        spreadsheet_id = config.options.get("spreadsheet_id") or ""
        norm = self._norm(config)
        # explicit range allowlist (SPEC 6.6): data_range wins, else A:Z
        value_range = norm.data_range or "A:Z"
        provider_id = dataset.provider_id or dataset.name
        rng = f"{provider_id}!{value_range}"

        data = await self._post(
            config,
            f"/sheets/v2/spreadsheets/{spreadsheet_id}/values_batch_get",
            {"ranges": [rng], "valueRenderOption": "ToString"},
        )
        value_ranges = data.get("value_ranges") or data.get("valueRanges") or []
        block = value_ranges[0].get("values", []) if value_ranges else []
        all_rows = [list(r) for r in block]

        truncated = False
        if cell_cap:
            total = sum(len(r) for r in all_rows)
            if total > MAX_RANGE_CELLS:
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
        return all_rows, truncated


def _k(v: Any) -> str:
    if v is None:
        return "\x00null"
    if isinstance(v, bool):
        return f"\x00bool:{v}"
    return f"{type(v).__name__}:{v}"


def _fingerprint(locator: str, columns: list[str], rows: int) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update(f"lk|{locator}|{','.join(columns)}|{rows}".encode())
    return f"lk_{h.hexdigest()[:24]}"
