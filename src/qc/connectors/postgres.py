"""PostgreSQL connector (SPEC 6.2).

Read-only, parameterized, budget-aware. Metadata from catalogs /
information_schema; pg_stats only when available (marked as estimates);
TABLESAMPLE documented as block-biased and optional.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from qc.connectors.base import (
    BaseConnector,
    ConnectorCapabilities,
    ProfileQuery,
    ProfileQueryResult,
    SampleRequest,
)
from qc.connectors.pool import AsyncConnectionPool
from qc.connectors.resilience import RetryPolicy, with_retry
from qc.connectors.sql_common import (
    assert_not_dangerous_sql,
    build_where,
    map_db_error,
    quote_ident,
    safety_check_dataset,
    split_qualified,
)
from qc.domain.enums import DatasetKind, LogicalType, Measurement
from qc.domain.errors import CancelledError, NotFoundError, SourceError, UnsupportedError
from qc.domain.models import (
    ColumnSchema,
    ConnectionTest,
    DatasetRef,
    DatasetSchema,
    EphemeralRow,
    ForeignKeyConstraint,
    SizeEstimate,
    SourceConfig,
)
from qc.observability.logging import get_logger
from qc.security.secrets import resolve_secret

# PostgreSQL type → logical type (SPEC 6.2 mapping)
_PG_LOGICAL: dict[str, LogicalType] = {
    "integer": LogicalType.INTEGER,
    "bigint": LogicalType.INTEGER,
    "smallint": LogicalType.INTEGER,
    "numeric": LogicalType.DECIMAL,
    "decimal": LogicalType.DECIMAL,
    "real": LogicalType.FLOAT,
    "double precision": LogicalType.FLOAT,
    "money": LogicalType.CURRENCY,
    "boolean": LogicalType.BOOLEAN,
    "date": LogicalType.DATE,
    "timestamp": LogicalType.TIMESTAMP,
    "timestamp without time zone": LogicalType.TIMESTAMP,
    "timestamptz": LogicalType.TIMESTAMP_TZ,
    "timestamp with time zone": LogicalType.TIMESTAMP_TZ,
    "time": LogicalType.TIME,
    "uuid": LogicalType.UUID,
    "json": LogicalType.JSON,
    "jsonb": LogicalType.JSON,
    "text": LogicalType.STRING,
    "varchar": LogicalType.STRING,
    "character varying": LogicalType.STRING,
    "char": LogicalType.STRING,
    "character": LogicalType.STRING,
    "bytea": LogicalType.BINARY,
    "inet": LogicalType.IP_ADDRESS,
    "cidr": LogicalType.IP_ADDRESS,
}


class PostgresConnector(BaseConnector):
    name = "postgres"
    capabilities = ConnectorCapabilities(
        metadata=True,
        aggregates=True,
        sampling=True,
        cancellation=True,
        writes=False,  # source connector: read-only by construction
        transactions=True,
        statistics=True,  # pg_stats (privilege dependent)
        tablesample=True,
        named=("metadata", "aggregates", "sampling", "cancellation", "statistics"),
    )

    def __init__(
        self,
        *,
        connect_timeout: int = 10,
        statement_timeout_ms: int = 30_000,
        retry: RetryPolicy | None = None,
        pool_min_size: int = 1,
        pool_max_size: int = 4,
    ) -> None:
        super().__init__()
        self.connect_timeout = connect_timeout
        self.statement_timeout_ms = statement_timeout_ms
        self.retry = retry or RetryPolicy(
            attempts=3,
            base_delay=0.2,
            max_delay=5.0,
            deadline_seconds=statement_timeout_ms / 1000,
        )
        self.pool_min_size = pool_min_size
        self.pool_max_size = max(1, pool_max_size)
        self._pool: AsyncConnectionPool[Any] | None = None
        self._dsn: str | None = None

    # ------------------------------------------------------------- lifecycle

    def _dsn_for(self, config: SourceConfig) -> str:
        if self._dsn is None:
            self._dsn = resolve_secret(config.connection_ref)
        return self._dsn

    async def _open_connection(self, config: SourceConfig) -> Any:
        try:
            import psycopg
        except ImportError as exc:
            raise UnsupportedError(
                "psycopg is not installed; install qc[postgres]",
                detail={"extra": "postgres"},
            ) from exc
        try:
            conn = await psycopg.AsyncConnection.connect(
                self._dsn_for(config),
                connect_timeout=self.connect_timeout,
                autocommit=True,
                options=f"-c statement_timeout={self.statement_timeout_ms}",
            )
        except Exception as exc:
            raise map_db_error(exc, connector=self.name) from exc
        # Enforce read-only session even if the role can write (SPEC 6.2);
        # privilege may disallow it — role-level read-only still applies.
        with contextlib.suppress(Exception):
            await conn.execute("SET default_transaction_read_only = on")
        return conn

    def _get_pool(self, config: SourceConfig) -> AsyncConnectionPool[Any]:
        if self._pool is None:
            self._pool = AsyncConnectionPool(
                lambda: self._open_connection(config),
                lambda conn: conn.close(),
                min_size=self.pool_min_size,
                max_size=self.pool_max_size,
            )
        return self._pool

    @asynccontextmanager
    async def _acquire(self, config: SourceConfig) -> AsyncIterator[Any]:
        """Check out a pooled connection (SPEC 6.1 pooling limits)."""
        pool = self._get_pool(config)
        async with pool.connection() as conn:
            yield conn

    async def close(self) -> None:
        if self._pool is not None:
            with contextlib.suppress(Exception):
                await self._pool.close()
            self._pool = None
        await super().close()

    async def _fetch(
        self, config: SourceConfig, sql: str, params: tuple[Any, ...] = ()
    ) -> list[tuple[Any, ...]]:
        assert_not_dangerous_sql(sql)

        async def attempt() -> list[tuple[Any, ...]]:
            async with self._acquire(config) as conn:
                cur = await conn.execute(sql, params)
                fetched: list[tuple[Any, ...]] = await cur.fetchall()
                return fetched

        def on_retry(attempt_no: int, exc: BaseException) -> None:
            self.retry_count += 1
            log.info(
                "transient query failure; retrying",
                fields={"error_code": "RETRY", "detail": type(exc).__name__},
            )

        try:
            # reads are idempotent: retry only transient failures (SPEC 6.1)
            return await with_retry(attempt, self.retry, cancel=self.cancel, on_retry=on_retry)
        except Exception as exc:
            raise map_db_error(exc, connector=self.name) from exc

    # --------------------------------------------------------------- API

    async def test_connection(self, config: SourceConfig) -> ConnectionTest:
        import time

        start = time.monotonic()
        try:
            rows = await self._fetch(config, "SELECT version()")
            latency = (time.monotonic() - start) * 1000
            version = str(rows[0][0])[:120] if rows else "unknown"
            return ConnectionTest(
                ok=True,
                message="connected (read-only session)",
                latency_ms=round(latency, 1),
                server_info={"version": version},
                capabilities=sorted(self.capabilities.named),
            )
        except SourceError as exc:
            return ConnectionTest(ok=False, message=exc.message, error_code=exc.code.value)

    async def list_datasets(self, config: SourceConfig) -> list[DatasetRef]:
        rows = await self._fetch(
            config,
            """
            SELECT table_schema, table_name, table_type
            FROM information_schema.tables
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
              AND table_schema = COALESCE(%s, table_schema)
            ORDER BY table_schema, table_name
            """,
            (config.default_schema,),
        )
        refs = []
        for schema, name, ttype in rows:
            kind = DatasetKind.VIEW if str(ttype).upper() == "VIEW" else DatasetKind.TABLE
            refs.append(
                DatasetRef(
                    source_id=config.id,
                    namespace=str(schema),
                    name=str(name),
                    kind=kind,
                    locator=f"{schema}.{name}",
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
            raise SourceError("dataset schema unknown; set default_schema")

        col_rows = await self._fetch(
            config,
            """
            SELECT column_name, ordinal_position, data_type, is_nullable,
                   column_default, udt_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (ns, name),
        )
        if not col_rows:
            raise NotFoundError(f"table not found: {dataset.qualified_name}")

        pk_rows = await self._fetch(
            config,
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.table_schema = %s AND tc.table_name = %s
              AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY kcu.ordinal_position
            """,
            (ns, name),
        )
        pk = [str(r[0]) for r in pk_rows]

        fk_rows = await self._fetch(
            config,
            """
            SELECT tc.constraint_name,
                   kcu.column_name,
                   ccu.table_schema AS ref_schema,
                   ccu.table_name AS ref_table,
                   ccu.column_name AS ref_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
            WHERE tc.table_schema = %s AND tc.table_name = %s
              AND tc.constraint_type = 'FOREIGN KEY'
            """,
            (ns, name),
        )
        fks: dict[str, ForeignKeyConstraint] = {}
        for cname, col, rschema, rtable, rcol in fk_rows:
            key = str(cname)
            if key not in fks:
                fks[key] = ForeignKeyConstraint(
                    name=key,
                    columns=[],
                    ref_namespace=str(rschema),
                    ref_name=str(rtable),
                    ref_columns=[],
                )
            fks[key].columns.append(str(col))
            fks[key].ref_columns.append(str(rcol))

        columns = []
        for row in col_rows:
            col_name, ordinal, dtype, nullable, default, udt = row
            source_type = str(dtype)
            if str(udt) and str(dtype) in ("USER-DEFINED", "ARRAY"):
                source_type = str(udt)
            logical = _PG_LOGICAL.get(source_type.lower(), LogicalType.UNSUPPORTED)
            columns.append(
                ColumnSchema(
                    name=str(col_name),
                    ordinal=int(ordinal) - 1,
                    source_type=source_type,
                    logical_type=logical,
                    nullable_declared=str(nullable).upper() == "YES",
                    default=str(default) if default is not None else None,
                    is_primary_key=str(col_name) in pk,
                )
            )

        fingerprint = _fingerprint(ns, name, [c.name for c in columns])
        return DatasetSchema(
            dataset=dataset,
            source_fingerprint=fingerprint,
            columns=columns,
            primary_key=pk,
            primary_key_declared=bool(pk),
            foreign_keys=list(fks.values()),
            row_estimate=await self._estimate_rows(config, ns, name),
            row_estimate_method=Measurement.ESTIMATED,
            provider_metadata={"engine": "postgresql", "schema": ns},
        )

    async def _estimate_rows(self, config: SourceConfig, ns: str, name: str) -> int | None:
        try:
            rows = await self._fetch(
                config,
                "SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass",
                (f"{quote_ident(ns)}.{quote_ident(name)}",),
            )
            if not rows or rows[0][0] is None:
                return None
            estimate = int(rows[0][0])
            # reltuples is -1/-1.0 until the first VACUANALYZE
            return estimate if estimate >= 0 else None
        except Exception:
            return None

    async def estimate_size(self, config: SourceConfig, dataset: DatasetRef) -> SizeEstimate:
        safety_check_dataset(dataset.qualified_name)
        ns, name = split_qualified(dataset.qualified_name, config.default_schema)
        if ns is None:
            raise SourceError("dataset schema unknown")
        qual = f"{quote_ident(ns)}.{quote_ident(name)}"
        del qual
        rows = await self._fetch(
            config,
            "SELECT pg_total_relation_size(%s)::bigint, "
            "(SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass)",
            (f"{ns}.{name}", f"{ns}.{name}"),
        )
        byte_est, row_est = rows[0]
        return SizeEstimate(
            row_estimate=int(row_est) if row_est is not None else None,
            byte_estimate=int(byte_est) if byte_est is not None else None,
            method=Measurement.ESTIMATED,
            note="pg_class estimates (vacuum-dependent)",
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
        where_sql, where_params = build_where(
            query.params.get("predicate"), quote=lambda c: quote_ident(c)
        )
        wparams: tuple[Any, ...] = tuple(where_params)

        if kind == "row_count":
            rows = await self._fetch(config, f"SELECT count(*) FROM {qual}{where_sql}", wparams)
            return self._result(kind, {"row_count": int(rows[0][0])}, started)

        if kind == "null_count":
            self._need(col_sql)
            rows = await self._fetch(
                config,
                f"SELECT count(*), count(*) FILTER (WHERE {col_sql} IS NULL) "
                f"FROM {qual}{where_sql}",
                wparams,
            )
            total, nulls = int(rows[0][0]), int(rows[0][1])
            return self._result(
                kind,
                {"row_count": total, "null_count": nulls},
                started,
            )

        if kind == "distinct":
            self._need(col_sql)
            rows = await self._fetch(
                config,
                f"SELECT count(DISTINCT {col_sql}), count(*) FILTER (WHERE {col_sql} IS NULL) "
                f"FROM {qual}{where_sql}",
                wparams,
            )
            distinct, nulls = int(rows[0][0]), int(rows[0][1])
            return self._result(
                kind,
                {"distinct_count": distinct, "null_count": nulls},
                started,
                estimated=False,
            )

        if kind == "min_max":
            self._need(col_sql)
            rows = await self._fetch(
                config,
                f"SELECT min({col_sql}), max({col_sql}) FROM {qual}{where_sql}",
                wparams,
            )
            lo, hi = rows[0]
            data: dict[str, Any] = {}
            if lo is not None:
                data["min"] = str(lo)
                data["max"] = str(hi)
                data["min_str"] = str(lo)
                data["max_str"] = str(hi)
            return self._result(kind, data, started)

        if kind == "top_k":
            self._need(col_sql)
            k = int(query.params.get("k", 10))
            rows = await self._fetch(
                config,
                f"SELECT {col_sql}::text AS v, count(*) AS c FROM {qual}{where_sql} "
                f"GROUP BY 1 ORDER BY c DESC NULLS LAST LIMIT %s",
                (*wparams, k),
            )
            categories = [{"key": str(v), "count": int(c), "falsy": v is None} for v, c in rows]
            return self._result(kind, {"categories": categories}, started, rows_returned=len(rows))

        if kind == "length_stats":
            self._need(col_sql)
            extra = f" AND {where_sql.strip()[6:]}" if where_sql else ""
            rows = await self._fetch(
                config,
                f"""SELECT min(length({col_sql}::text)), max(length({col_sql}::text)),
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY length({col_sql}::text)),
                       percentile_cont(0.9) WITHIN GROUP (ORDER BY length({col_sql}::text))
                    FROM {qual} WHERE {col_sql} IS NOT NULL{extra}""",
                wparams,
            )
            lo, hi, p50, p90 = rows[0]
            data = {}
            if lo is not None:
                data = {
                    "min": int(lo),
                    "max": int(hi),
                    "p50": int(p50 or 0),
                    "p90": int(p90 or 0),
                }
            return self._result(kind, data, started)

        if kind == "full_scan_values":
            # only reached when the planner has explicit full-scan approval
            self._need(col_sql)
            rows = await self._fetch(config, f"SELECT {col_sql} FROM {qual}{where_sql}", wparams)
            return self._result(
                kind,
                {"values": [r[0] for r in rows]},
                started,
                rows_returned=len(rows),
            )

        raise UnsupportedError(f"unsupported profile query kind: {kind}")

    async def sample_rows(
        self, config: SourceConfig, request: SampleRequest
    ) -> AsyncIterator[EphemeralRow]:
        safety_check_dataset(request.dataset.qualified_name)
        ns, name = split_qualified(request.dataset.qualified_name, config.default_schema)
        if ns is None:
            raise SourceError("dataset schema unknown")
        qual = f"{quote_ident(ns)}.{quote_ident(name)}"

        cols_sql = "*"
        if request.columns:
            cols_sql = ", ".join(quote_ident(c) for c in request.columns)

        where_sql, where_params = build_where(request.predicate, quote=lambda c: quote_ident(c))
        if request.method == "random":
            # documented: random() sampling; bounded by LIMIT
            sql = f"SELECT {cols_sql} FROM {qual}{where_sql} ORDER BY random() LIMIT %s"
            params: tuple[Any, ...] = (*where_params, request.limit)
        else:
            # stable-key ordered sampling: deterministic, avoids full random sort
            order_col = self._stable_key_col(request)
            sql = (
                f"SELECT {cols_sql} FROM {qual}{where_sql} "
                f"ORDER BY {quote_ident(order_col)} LIMIT %s OFFSET %s"
            )
            params = (*where_params, request.limit, request.offset)

        assert_not_dangerous_sql(sql)
        try:
            async with self._acquire(config) as conn:
                cur = await conn.execute(sql, params)
                while True:
                    if request.cancel is not None and request.cancel.cancelled:
                        # close the server-side cursor before unwinding (SPEC 12.6)
                        with contextlib.suppress(Exception):
                            await cur.close()
                        request.cancel.raise_if_cancelled()
                    records = await cur.fetchmany(200)
                    if not records:
                        break
                    names = [d.name for d in cur.description] if cur.description else []
                    for rec in records:
                        yield EphemeralRow(values=dict(zip(names, rec, strict=False)))
        except CancelledError:
            raise
        except Exception as exc:
            raise map_db_error(exc, connector=self.name) from exc

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _need(col_sql: str | None) -> None:
        if not col_sql:
            raise UnsupportedError("query kind requires 'column' param")

    @staticmethod
    def _result(
        kind: str,
        data: dict[str, Any],
        started: float,
        *,
        rows_returned: int = 1,
        estimated: bool = False,
    ) -> ProfileQueryResult:
        import time

        return ProfileQueryResult(
            kind=kind,
            data=data,
            rows_returned=rows_returned,
            duration_seconds=round(time.monotonic() - started, 6),
            estimated=estimated,
        )

    def _stable_key_col(self, request: SampleRequest) -> str:
        if request.columns:
            return request.columns[0]
        raise UnsupportedError("stable-key sampling requires a deterministic order column")

    @staticmethod
    def _safe_predicate(predicate: dict[str, Any]) -> tuple[str, Any]:
        col = predicate.get("column")
        if not isinstance(col, str) or not col.isidentifier():
            raise UnsupportedError("predicate column must be a simple identifier")
        if predicate.get("op") not in (None, "="):
            raise UnsupportedError("only '=' predicates are supported")
        value = predicate.get("value")
        if isinstance(value, (dict, list)):
            raise UnsupportedError("predicate value must be scalar")
        return col, value


def _fingerprint(ns: str, name: str, columns: list[str]) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update(f"pg|{ns}|{name}|{','.join(columns)}".encode())
    return f"pg_{h.hexdigest()[:24]}"


log = get_logger("postgres")
