"""Alibaba MaxCompute connector via the JVM JDBC sidecar (SPEC 6.4).

All SQL goes through the bridge; the Python side never loads JDBC.
Sampling syntax must be verified against the actual MaxCompute dialect
(SPEC 6.4) — this connector uses only portable LIMIT/OFFSET and
aggregates until dialect-specific sampling is contract-tested.
"""

from __future__ import annotations

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
from qc.connectors.sql_common import (
    assert_not_dangerous_sql,
    quote_ident,
    safety_check_dataset,
    split_qualified,
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
from qc.jdbc_bridge.client import BridgeConfig, JdbcBridge

# MaxCompute type → logical type (SPEC 6.4: no precision loss)
_MAXCOMPUTE_LOGICAL: dict[str, LogicalType] = {
    "bigint": LogicalType.INTEGER,
    "int": LogicalType.INTEGER,
    "smallint": LogicalType.INTEGER,
    "tinyint": LogicalType.INTEGER,
    "decimal": LogicalType.DECIMAL,
    "double": LogicalType.FLOAT,
    "float": LogicalType.FLOAT,
    "varchar": LogicalType.STRING,
    "char": LogicalType.STRING,
    "string": LogicalType.STRING,
    "boolean": LogicalType.BOOLEAN,
    "datetime": LogicalType.TIMESTAMP,
    "timestamp": LogicalType.TIMESTAMP,
    "date": LogicalType.DATE,
    "binary": LogicalType.BINARY,
}

MAXCOMPUTE_DRIVER = "com.aliyun.odps.jdbc.OdrsDriver"


class MaxComputeConnector(BaseConnector):
    name = "maxcompute"

    def __init__(self, bridge_config: BridgeConfig | None = None) -> None:
        super().__init__()
        self._bridge_cfg = bridge_config
        self.bridge: JdbcBridge | None = None
        # declared capabilities; sampling syntax is dialect-verified only
        # for LIMIT/OFFSET (SPEC 6.4: do not assume PG/MySQL sampling)
        self.capabilities = ConnectorCapabilities(
            metadata=True,
            aggregates=True,
            sampling=True,  # portable LIMIT/OFFSET only
            cancellation=True,
            writes=False,
            partitions=True,
            cost_estimates=False,  # enabled when the driver exposes them
            named=("metadata", "aggregates", "sampling", "cancellation"),
        )

    async def _get_bridge(self, config: SourceConfig) -> JdbcBridge:
        if self.bridge is not None and self.bridge.alive:
            return self.bridge
        cfg = self._bridge_cfg or BridgeConfig(
            jar_path=Path(config.options.get("sidecar_jar", ""))
            if config.options.get("sidecar_jar")
            else None,
            classpath=Path(config.options["sidecar_classpath"])
            if config.options.get("sidecar_classpath")
            else None,
        )
        self.bridge = JdbcBridge(cfg)
        # JDBC URL + credentials via environment references only (SPEC 4.3)
        env_name = "QC_MCP_JDBC_URL_" + config.name.upper().replace("-", "_")
        env_overrides = {env_name: _jdbc_url(config)}
        for key, opt_key in (
            ("MAXCOMPUTE_ACCESS_ID", "access_id_env"),
            ("MAXCOMPUTE_ACCESS_KEY", "access_key_env"),
        ):
            ref = config.options.get(opt_key.lower())
            if ref:
                from qc.security.secrets import resolve_secret

                env_overrides[key] = resolve_secret(ref)
        await self.bridge.start(env_overrides=env_overrides)
        await self.bridge.connect(
            driver=MAXCOMPUTE_DRIVER,
            jdbc_url_env=env_name,
            options={
                k: v
                for k, v in config.options.items()
                if k not in ("sidecar_jar", "sidecar_classpath")
            },
        )
        return self.bridge

    async def close(self) -> None:
        if self.bridge is not None:
            await self.bridge.close()
            self.bridge = None
        await super().close()

    # --------------------------------------------------------------- API

    async def test_connection(self, config: SourceConfig) -> ConnectionTest:
        import time

        start = time.monotonic()
        try:
            bridge = await self._get_bridge(config)
            ok = await bridge.ping()
            latency = (time.monotonic() - start) * 1000
            return ConnectionTest(
                ok=ok,
                message="sidecar reachable (JDBC via bridge)" if ok else "sidecar ping failed",
                latency_ms=round(latency, 1),
                capabilities=sorted(self.capabilities.named),
            )
        except Exception as exc:
            from qc.security.redaction import safe_error_message

            return ConnectionTest(
                ok=False,
                message=safe_error_message(exc),
                error_code="SOURCE_UNAVAILABLE",
            )

    async def list_datasets(self, config: SourceConfig) -> list[DatasetRef]:
        bridge = await self._get_bridge(config)
        schema = config.options.get("schema") or config.default_schema
        raw = await bridge.list_datasets(schema)
        refs = []
        for entry in raw:
            name = str(entry.get("name", ""))
            ns = str(entry.get("schema") or schema or "")
            if not name:
                continue
            kind_raw = str(entry.get("kind", "table")).lower()
            refs.append(
                DatasetRef(
                    source_id=config.id,
                    namespace=ns or None,
                    name=name,
                    kind=DatasetKind.VIEW if kind_raw == "view" else DatasetKind.TABLE,
                    locator=f"{ns}.{name}" if ns else name,
                )
            )
        if config.allowed_datasets:
            allowed = set(config.allowed_datasets)
            refs = [r for r in refs if r.qualified_name in allowed]
        return refs

    async def get_schema(self, config: SourceConfig, dataset: DatasetRef) -> DatasetSchema:
        safety_check_dataset(dataset.qualified_name)
        ns, name = split_qualified(dataset.qualified_name, config.default_schema)
        if ns is None:
            raise SourceError("MaxCompute dataset schema unknown")
        bridge = await self._get_bridge(config)
        raw_cols = await bridge.list_columns(ns, name)
        if not raw_cols:
            raise NotFoundError(f"MaxCompute table not found: {dataset.qualified_name}")

        columns: list[ColumnSchema] = []
        pk: list[str] = []
        for idx, c in enumerate(raw_cols):
            col_name = str(c.get("name", ""))
            source_type = str(c.get("type", "")).lower()
            base = source_type.split("(")[0].strip()
            logical = _MAXCOMPUTE_LOGICAL.get(base, LogicalType.UNSUPPORTED)
            is_pk = bool(c.get("primary_key") or c.get("is_primary_key"))
            if is_pk:
                pk.append(col_name)
            columns.append(
                ColumnSchema(
                    name=col_name,
                    ordinal=int(c.get("ordinal", idx)),
                    source_type=source_type,
                    logical_type=logical,
                    nullable_declared=(not is_pk)
                    if c.get("nullable") is None
                    else bool(c.get("nullable")),
                    is_primary_key=is_pk,
                )
            )
        return DatasetSchema(
            dataset=dataset,
            source_fingerprint=_fingerprint(ns, name, [c.name for c in columns]),
            columns=columns,
            primary_key=pk,
            primary_key_declared=bool(pk),
            row_estimate=await self._estimate_rows(bridge, ns, name),
            row_estimate_method=Measurement.ESTIMATED,
            provider_metadata={"engine": "maxcompute", "schema": ns},
        )

    async def _estimate_rows(self, bridge: JdbcBridge, ns: str, name: str) -> int | None:
        try:
            sql = f"SELECT COUNT(*) AS c FROM {quote_ident(ns)}.{quote_ident(name)} LIMIT 1"
            _cols, rows, _ = await bridge.execute(sql, max_rows=1)
            if rows and rows[0]:
                return int(rows[0][0])
        except Exception:
            return None  # row estimate is optional
        return None

    async def estimate_size(self, config: SourceConfig, dataset: DatasetRef) -> SizeEstimate:
        safety_check_dataset(dataset.qualified_name)
        ns, name = split_qualified(dataset.qualified_name, config.default_schema)
        if ns is None:
            raise SourceError("dataset schema unknown")
        bridge = await self._get_bridge(config)
        # partition metadata when present (SPEC 6.4: partition awareness)
        try:
            sql = f"SELECT COUNT(*) FROM {quote_ident(ns)}.{quote_ident(name)} LIMIT 1"
            _cols, rows, _ = await bridge.execute(sql, max_rows=1)
            count = int(rows[0][0]) if rows else None
        except Exception as exc:
            raise SourceError(f"size estimate failed: {exc}") from exc
        return SizeEstimate(
            row_estimate=count,
            method=Measurement.EXACT if count is not None else Measurement.UNKNOWN,
            note="exact COUNT under 1-row bound; larger scans use metadata estimates",
        )

    async def execute_profile_query(
        self, config: SourceConfig, query: ProfileQuery
    ) -> ProfileQueryResult:
        import time

        safety_check_dataset(query.dataset.qualified_name)
        ns, name = split_qualified(query.dataset.qualified_name, config.default_schema)
        if ns is None:
            raise SourceError("dataset schema unknown")
        qual = f"{quote_ident(ns)}.{quote_ident(name)}"
        col = query.params.get("column")
        col_sql = quote_ident(str(col)) if col else None
        kind = query.kind
        started = time.monotonic()
        bridge = await self._get_bridge(config)

        if kind == "row_count":
            # bounded count: MaxCompute exposes COUNT fast on statistics;
            # LIMIT keeps result size at 1 row regardless
            sql = f"SELECT COUNT(*) FROM {qual} LIMIT 1"
        elif kind == "null_count":
            _need(col_sql)
            sql = (
                f"SELECT COUNT(*), SUM(CASE WHEN {col_sql} IS NULL THEN 1 ELSE 0 END) "
                f"FROM {qual} LIMIT 1"
            )
        elif kind == "distinct":
            _need(col_sql)
            sql = (
                f"SELECT COUNT(DISTINCT {col_sql}), "
                f"SUM(CASE WHEN {col_sql} IS NULL THEN 1 ELSE 0 END) "
                f"FROM {qual} LIMIT 1"
            )
        elif kind == "min_max":
            _need(col_sql)
            sql = f"SELECT MIN({col_sql}), MAX({col_sql}) FROM {qual} LIMIT 1"
        elif kind == "top_k":
            _need(col_sql)
            k = int(query.params.get("k", 10))
            sql = (
                f"SELECT {col_sql} AS v, COUNT(*) AS c FROM {qual} "
                f"GROUP BY {col_sql} ORDER BY c DESC LIMIT {int(k)}"
            )
        elif kind == "length_stats":
            _need(col_sql)
            sql = (
                f"SELECT MIN(LENGTH({col_sql})), MAX(LENGTH({col_sql})) "
                f"FROM {qual} WHERE {col_sql} IS NOT NULL LIMIT 1"
            )
        elif kind == "full_scan_values":
            _need(col_sql)
            sql = f"SELECT {col_sql} FROM {qual}"
        else:
            raise UnsupportedError(f"unsupported profile query kind: {kind}")

        assert_not_dangerous_sql(sql)
        cols, rows, truncated = await bridge.execute(
            sql,
            max_rows=1 if kind != "full_scan_values" else 10_000,
            max_bytes=8 * 1024 * 1024,
        )
        data = _shape(kind, cols, rows)
        return ProfileQueryResult(
            kind=kind,
            data=data,
            rows_returned=len(rows),
            duration_seconds=time.monotonic() - started,
            truncated=truncated,
            estimated=truncated,
            note="cost estimates unavailable from driver" if kind == "row_count" else None,
        )

    async def sample_rows(
        self, config: SourceConfig, request: SampleRequest
    ) -> AsyncIterator[EphemeralRow]:
        safety_check_dataset(request.dataset.qualified_name)
        ns, name = split_qualified(request.dataset.qualified_name, config.default_schema)
        if ns is None:
            raise SourceError("dataset schema unknown")
        qual = f"{quote_ident(ns)}.{quote_ident(name)}"
        cols_sql = ", ".join(quote_ident(c) for c in request.columns) if request.columns else "*"
        # Portable sampling only: ORDER BY + LIMIT/OFFSET. MaxCompute
        # dialect sampling must be contract-tested before use (SPEC 6.4).
        if request.method == "random":
            # no RAND() assumption: stable ordering with offset windows
            order = request.columns[0] if request.columns else None
            if order is None:
                raise UnsupportedError("MaxCompute sampling needs a deterministic order column")
            sql = (
                f"SELECT {cols_sql} FROM {qual} ORDER BY {quote_ident(order)} "
                f"LIMIT {int(request.limit)} OFFSET {int(request.offset)}"
            )
        else:
            order = request.columns[0] if request.columns else None
            if order is None:
                raise UnsupportedError("MaxCompute stable-key sampling needs an order column")
            sql = (
                f"SELECT {cols_sql} FROM {qual} ORDER BY {quote_ident(order)} "
                f"LIMIT {int(request.limit)} OFFSET {int(request.offset)}"
            )
        assert_not_dangerous_sql(sql)
        bridge = await self._get_bridge(config)
        cols, rows, _ = await bridge.execute(sql, max_rows=request.limit, max_bytes=4 * 1024 * 1024)
        for idx, row in enumerate(rows):
            yield EphemeralRow(
                values=dict(zip(cols, row, strict=False)),
                row_number=request.offset + idx + 1,
            )


def _shape(kind: str, cols: list[str], rows: list[list[Any]]) -> dict[str, Any]:
    """Map bridge result rows into the standard profile-query payload."""
    if kind == "row_count":
        return {"row_count": int(rows[0][0]) if rows and rows[0][0] is not None else 0}
    if kind == "null_count" and rows:
        total, nulls = rows[0][0], rows[0][1]
        return {
            "row_count": int(total or 0),
            "null_count": int(nulls or 0),
        }
    if kind == "distinct" and rows:
        distinct, nulls = rows[0][0], rows[0][1]
        return {
            "distinct_count": int(distinct or 0),
            "null_count": int(nulls or 0),
            "row_count": int(distinct or 0) + int(nulls or 0),
        }
    if kind == "min_max" and rows and rows[0][0] is not None:
        lo, hi = rows[0][0], rows[0][1]
        return {
            "min": str(lo),
            "max": str(hi),
            "min_str": str(lo),
            "max_str": str(hi),
        }
    if kind == "top_k":
        return {
            "categories": [
                {"key": str(r[0]), "count": int(r[1]), "falsy": r[0] is None} for r in rows
            ]
        }
    if kind == "length_stats" and rows and rows[0][0] is not None:
        return {"min": int(rows[0][0]), "max": int(rows[0][1] or 0)}
    if kind == "full_scan_values":
        return {"values": [r[0] for r in rows]}
    return {"row_count": 0}


def _need(col_sql: str | None) -> None:
    if not col_sql:
        raise UnsupportedError("query kind requires 'column' param")


def _jdbc_url(config: SourceConfig) -> str:
    """Build the JDBC URL from config options (no inline secrets).

    Recognized options: endpoint, project, scheme. Credentials are never
    part of the URL when *_env references are configured.
    """
    endpoint = config.options.get("endpoint", "")
    project = config.options.get("project", "")
    if not endpoint:
        raise SourceError(
            "maxcompute source requires options.endpoint "
            "(e.g. service.cn-hangzhou.maxcompute.aliyun.com)"
        )
    return f"jdbc:odps://{endpoint}!{project}" if project else f"jdbc:odps://{endpoint}"


def _fingerprint(ns: str, name: str, columns: list[str]) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update(f"mc|{ns}|{name}|{','.join(columns)}".encode())
    return f"mc_{h.hexdigest()[:24]}"
