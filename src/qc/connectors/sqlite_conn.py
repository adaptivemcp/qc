"""SQLite connector (read-only, file-backed).

SQLite is a *file*, not a network service, so the source's secret
reference holds a filesystem path rather than a DSN. The path still goes
through the normal secret machinery: ``env:QC_SQLITE_PATH``,
``file:~/.config/qc/db.path``, ``keyring:``, or ``cmd:``. Nothing about
the path is a credential, but keeping it behind a reference means a
database location is never hardcoded in ``sources.json`` and the same
redaction/audit path applies to every connector.

Read-only is enforced by SQLite itself, not by convention:

- the connection is opened as a ``file:...?mode=ro`` URI, so the engine
  refuses writes rather than trusting the caller;
- ``PRAGMA query_only=ON`` is a second, session-level guard;
- a statement authorizer denies everything that is not a read, which
  blocks ``PRAGMA``/``ATTACH`` tricks that could escape the sandbox;
- the pool is bounded and every query passes the SELECT-only guard.

Limitations versus the networked SQL connectors, documented rather than
hidden:

- No cost estimates and no ``pg_stats`` equivalent, so row counts come
  from an exact ``count(*)`` (fast, but a real scan) and are marked exact.
- SQLite has no ``percentile_cont``; ``length_stats`` percentiles use the
  nearest-rank definition (``ceil(n*p)``) via a window-function rank in a
  single scan. This is *not* interpolated like PostgreSQL's
  ``percentile_cont``, so a p50 between two observed lengths differs by up
  to one rank; min/max are exact. Requires SQLite >= 3.25.
- ``top_k`` has no ``NULLS LAST`` on very old engines, so the ordering
  falls back to an explicit null-flag sort.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
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

# SQLite declared type -> logical type. SQLite uses type *affinity*, so
# declared types are advisory; inference still verifies against values.
_SQLITE_LOGICAL: dict[str, LogicalType] = {
    "integer": LogicalType.INTEGER,
    "int": LogicalType.INTEGER,
    "bigint": LogicalType.INTEGER,
    "smallint": LogicalType.INTEGER,
    "tinyint": LogicalType.INTEGER,
    "mediumint": LogicalType.INTEGER,
    "int2": LogicalType.INTEGER,
    "int8": LogicalType.INTEGER,
    "boolean": LogicalType.BOOLEAN,
    "bool": LogicalType.BOOLEAN,
    "decimal": LogicalType.DECIMAL,
    "numeric": LogicalType.DECIMAL,
    "real": LogicalType.FLOAT,
    "float": LogicalType.FLOAT,
    "double": LogicalType.FLOAT,
    "double precision": LogicalType.FLOAT,
    "text": LogicalType.STRING,
    "varchar": LogicalType.STRING,
    "char": LogicalType.STRING,
    "character": LogicalType.STRING,
    "character varying": LogicalType.STRING,
    "clob": LogicalType.STRING,
    "json": LogicalType.JSON,
    "date": LogicalType.DATE,
    "time": LogicalType.TIME,
    "datetime": LogicalType.TIMESTAMP,
    "timestamp": LogicalType.TIMESTAMP,
    "blob": LogicalType.BINARY,
    "uuid": LogicalType.UUID,
}

#: Read-only introspection pragmas allowed through the authorizer. This is
#: an exact allowlist: anything else (including ``journal_mode``,
#: ``writable_schema``, ``foreign_keys``) stays denied.
_SQLITE_READONLY_PRAGMAS = frozenset({"table_info", "foreign_key_list", "table_xinfo"})

#: authorizer action codes for statements that must never run (read-only).
_SQLITE_DENY_ACTIONS = frozenset(
    {
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_INDEX,
        sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
        sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_INDEX,
        sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER,
        sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_REINDEX,
        sqlite3.SQLITE_ANALYZE,
        sqlite3.SQLITE_CREATE_VTABLE,
        sqlite3.SQLITE_DROP_VTABLE,
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_SAVEPOINT,
    }
)


def _logical_for(declared: str) -> LogicalType:
    """Map a SQLite declared type to a logical type by affinity rules."""
    base = declared.strip().lower().split("(")[0].strip()
    if not base:
        return LogicalType.UNSUPPORTED
    if base in _SQLITE_LOGICAL:
        return _SQLITE_LOGICAL[base]
    # affinity substrings (SQLite type-affinity rules, documented order)
    if "int" in base:
        return LogicalType.INTEGER
    if "char" in base or "clob" in base or "text" in base:
        return LogicalType.STRING
    if "blob" in base:
        return LogicalType.BINARY
    if "real" in base or "floa" in base or "doub" in base:
        return LogicalType.FLOAT
    if "bool" in base:
        return LogicalType.BOOLEAN
    if "dec" in base or "num" in base:
        return LogicalType.DECIMAL
    if "date" in base or "time" in base:
        return LogicalType.TIMESTAMP
    return LogicalType.UNSUPPORTED


class SQLiteConnector(BaseConnector):
    """Read-only access to a local SQLite database file."""

    name = "sqlite"
    capabilities = ConnectorCapabilities(
        metadata=True,
        aggregates=True,
        sampling=True,
        cancellation=True,
        writes=False,  # source connector: read-only by construction
        transactions=False,
        statistics=False,  # no sqlite_stat1 equivalent we can trust
        cost_estimates=False,
        named=("metadata", "aggregates", "sampling", "cancellation"),
    )

    def __init__(
        self,
        *,
        busy_timeout_ms: int = 5_000,
        retry: RetryPolicy | None = None,
        pool_min_size: int = 0,
        pool_max_size: int = 4,
    ) -> None:
        super().__init__()
        self.busy_timeout_ms = busy_timeout_ms
        self.retry = retry or RetryPolicy(attempts=3, base_delay=0.2, max_delay=5.0)
        self.pool_min_size = pool_min_size
        self.pool_max_size = max(1, pool_max_size)
        self._pool: AsyncConnectionPool[Any] | None = None
        self._path: Path | None = None

    # ------------------------------------------------------------- lifecycle

    def _path_for(self, config: SourceConfig) -> Path:
        """Resolve the database path from the source's secret reference.

        The resolved value is a path, not a credential, but it is still
        treated as secret material: it is never logged, and it appears in
        errors only as the file *name*.
        """
        if self._path is not None:
            return self._path
        raw = config.options.get("path")
        if not raw:
            raw = resolve_secret(config.connection_ref)
        path = Path(raw.strip()).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        self._path = path
        return path

    def _open_sync(self, config: SourceConfig) -> sqlite3.Connection:
        """Open a read-only connection (blocking; run off the event loop)."""
        path = self._path_for(config)
        if not path.is_file():
            raise SourceError(
                f"sqlite database not found: {path.name}",
                detail={"connector": self.name},
            )
        # mode=ro is enforced by the engine, so a stray write is refused
        # by SQLite itself rather than by our own convention.
        uri = f"file:{_uri_path(path)}?mode=ro"
        try:
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=self.busy_timeout_ms / 1000,
                # the connection is used from a single worker thread at a
                # time (pool checkout), and results are plain tuples
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise map_db_error(exc, connector=self.name) from exc
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_deny_write)
        return conn

    async def _open_connection(self, config: SourceConfig) -> sqlite3.Connection:
        return await asyncio.to_thread(self._open_sync, config)

    def _get_pool(self, config: SourceConfig) -> AsyncConnectionPool[Any]:
        if self._pool is None:
            self._pool = AsyncConnectionPool(
                lambda: self._open_connection(config),
                lambda conn: asyncio.to_thread(conn.close),
                min_size=self.pool_min_size,
                max_size=self.pool_max_size,
            )
        return self._pool

    @contextlib.asynccontextmanager
    async def _acquire(self, config: SourceConfig) -> AsyncIterator[sqlite3.Connection]:
        pool = self._get_pool(config)
        async with pool.connection() as conn:
            yield conn

    async def close(self) -> None:
        if self._pool is not None:
            with contextlib.suppress(Exception):
                await self._pool.close()
            self._pool = None
        self._path = None
        await super().close()

    async def _fetch(
        self, config: SourceConfig, sql: str, params: tuple[Any, ...] = ()
    ) -> list[tuple[Any, ...]]:
        assert_not_dangerous_sql(sql)

        async def attempt() -> list[tuple[Any, ...]]:
            async with self._acquire(config) as conn:
                return await asyncio.to_thread(_run_query, conn, sql, params)

        def on_retry(attempt_no: int, exc: BaseException) -> None:
            self.retry_count += 1
            log.info(
                "transient query failure; retrying",
                fields={"error_code": "RETRY", "detail": type(exc).__name__},
            )

        try:
            return await with_retry(attempt, self.retry, cancel=self.cancel, on_retry=on_retry)
        except Exception as exc:
            raise map_db_error(exc, connector=self.name) from exc

    async def _fetch_meta(
        self, config: SourceConfig, pragma: str, ident: str
    ) -> list[tuple[Any, ...]]:
        """Run a read-only introspection pragma against one validated table.

        PRAGMA is not SQL and cannot be parameterized, so it is kept out of
        the query path entirely: ``pragma`` must be on the allowlist and
        ``ident`` must pass ``quote_ident`` (strict regex + quoting) before
        it is interpolated. The authorizer re-checks the pragma name.
        """
        if pragma.lower() not in _SQLITE_READONLY_PRAGMAS:
            raise UnsupportedError(f"sqlite pragma not permitted: {pragma}")
        qual = quote_ident(ident)
        sql = f"PRAGMA {pragma}({qual})"

        async def attempt() -> list[tuple[Any, ...]]:
            async with self._acquire(config) as conn:
                return await asyncio.to_thread(_run_query, conn, sql, ())

        def on_retry(attempt_no: int, exc: BaseException) -> None:
            self.retry_count += 1
            log.info(
                "transient metadata failure; retrying",
                fields={"error_code": "RETRY", "detail": type(exc).__name__},
            )

        try:
            return await with_retry(attempt, self.retry, cancel=self.cancel, on_retry=on_retry)
        except Exception as exc:
            raise map_db_error(exc, connector=self.name) from exc

    # --------------------------------------------------------------- API

    async def test_connection(self, config: SourceConfig) -> ConnectionTest:
        import time

        start = time.monotonic()
        try:
            rows = await self._fetch(config, "SELECT sqlite_version()")
            latency = (time.monotonic() - start) * 1000
            version = str(rows[0][0])[:120] if rows else "unknown"
            return ConnectionTest(
                ok=True,
                message="connected (read-only, query_only)",
                latency_ms=round(latency, 1),
                server_info={"version": f"sqlite {version}", "engine": "sqlite"},
                capabilities=sorted(self.capabilities.named),
            )
        except SourceError as exc:
            return ConnectionTest(ok=False, message=exc.message, error_code=exc.code.value)

    async def list_datasets(self, config: SourceConfig) -> list[DatasetRef]:
        rows = await self._fetch(
            config,
            "SELECT name, type FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name",
        )
        refs = [
            DatasetRef(
                source_id=config.id,
                # SQLite has no schemas; "main" is the canonical namespace
                namespace="main",
                name=str(name),
                kind=DatasetKind.VIEW if str(ttype) == "view" else DatasetKind.TABLE,
                locator=str(name),
            )
            for name, ttype in rows
        ]
        if config.allowed_datasets:
            allowed = {a.rpartition(".")[2] for a in config.allowed_datasets}
            refs = [r for r in refs if r.name in allowed]
        return refs

    async def get_schema(self, config: SourceConfig, dataset: DatasetRef) -> DatasetSchema:
        safety_check_dataset(dataset.qualified_name)
        ns, name = split_qualified(dataset.qualified_name, config.default_schema)
        del ns  # SQLite is single-namespace ("main")

        col_rows = await self._fetch_meta(config, "table_info", name)
        if not col_rows:
            raise NotFoundError(f"table not found: {dataset.qualified_name}")

        pk = [str(r[1]) for r in sorted(col_rows, key=lambda r: int(r[5])) if int(r[5]) > 0]

        # PRAGMA foreign_key_list: (id, seq, table, from, to, ...)
        fk_rows = await self._fetch_meta(config, "foreign_key_list", name)
        fks: dict[str, ForeignKeyConstraint] = {}
        for fid, _seq, ref_table, from_col, to_col, *_rest in fk_rows:
            key = f"fk_{fid}_{ref_table}"
            if key not in fks:
                fks[key] = ForeignKeyConstraint(
                    name=str(ref_table),
                    columns=[],
                    ref_namespace="main",
                    ref_name=str(ref_table),
                    ref_columns=[],
                )
            fks[key].columns.append(str(from_col))
            fks[key].ref_columns.append(str(to_col) if to_col else str(from_col))

        columns = []
        for row in col_rows:
            col_name, declared, notnull, default, _pk_pos = row[1], row[2], row[3], row[4], row[5]
            source_type = str(declared or "")
            columns.append(
                ColumnSchema(
                    name=str(col_name),
                    ordinal=int(row[0]),
                    source_type=source_type or "BLOB",
                    logical_type=_logical_for(source_type),
                    # SQLite columns are nullable unless declared NOT NULL
                    nullable_declared=int(notnull) == 0,
                    default=None if default is None else str(default),
                    is_primary_key=int(_pk_pos) > 0,
                )
            )

        return DatasetSchema(
            dataset=dataset,
            source_fingerprint=_fingerprint(name, [c.name for c in columns]),
            columns=columns,
            primary_key=pk,
            primary_key_declared=bool(pk),
            foreign_keys=list(fks.values()),
            row_estimate=None,
            row_estimate_method=Measurement.UNKNOWN,
            provider_metadata={"engine": "sqlite", "namespace": "main"},
        )

    async def estimate_size(self, config: SourceConfig, dataset: DatasetRef) -> SizeEstimate:
        safety_check_dataset(dataset.qualified_name)
        # validate the reference even though only the file size is reported
        split_qualified(dataset.qualified_name, config.default_schema)
        # file size is known exactly; row count would require a scan, so it
        # is left to the profiler's explicit row_count probe
        try:
            byte_estimate: int | None = self._path_for(config).stat().st_size
        except OSError:
            byte_estimate = None
        return SizeEstimate(
            row_estimate=None,
            byte_estimate=byte_estimate,
            method=Measurement.ESTIMATED,
            note="database file size; row count requires a scan",
        )

    async def execute_profile_query(
        self, config: SourceConfig, query: ProfileQuery
    ) -> ProfileQueryResult:
        import time

        safety_check_dataset(query.dataset.qualified_name)
        _ns, name = split_qualified(query.dataset.qualified_name, config.default_schema)
        qual = quote_ident(name)
        col = query.params.get("column")
        col_sql = quote_ident(str(col)) if col else None
        kind = query.kind
        started = time.monotonic()
        where_sql, where_params = build_where(
            query.params.get("predicate"), quote=lambda c: quote_ident(c), placeholder="?"
        )
        wparams: tuple[Any, ...] = tuple(where_params)

        if kind == "row_count":
            rows = await self._fetch(config, f"SELECT count(*) FROM {qual}{where_sql}", wparams)
            return self._result(kind, {"row_count": int(rows[0][0])}, started)

        if kind == "null_count":
            self._need(col_sql)
            # CASE instead of FILTER: FILTER needs 3.30+, CASE needs nothing
            rows = await self._fetch(
                config,
                f"SELECT count(*), sum(CASE WHEN {col_sql} IS NULL THEN 1 ELSE 0 END) "
                f"FROM {qual}{where_sql}",
                wparams,
            )
            total, nulls = int(rows[0][0]), int(rows[0][1] or 0)
            return self._result(kind, {"row_count": total, "null_count": nulls}, started)

        if kind == "distinct":
            self._need(col_sql)
            rows = await self._fetch(
                config,
                f"SELECT count(DISTINCT {col_sql}), "
                f"sum(CASE WHEN {col_sql} IS NULL THEN 1 ELSE 0 END) "
                f"FROM {qual}{where_sql}",
                wparams,
            )
            distinct, nulls = int(rows[0][0]), int(rows[0][1] or 0)
            return self._result(
                kind, {"distinct_count": distinct, "null_count": nulls}, started, estimated=False
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
            # NULLS LAST needs 3.30+; the flag sort is the portable form
            rows = await self._fetch(
                config,
                f"SELECT CAST({col_sql} AS TEXT) AS v, count(*) AS c FROM {qual}{where_sql} "
                f"GROUP BY v ORDER BY c DESC, (v IS NOT NULL) ASC, v ASC LIMIT ?",
                (*wparams, k),
            )
            categories = [{"key": str(v), "count": int(c), "falsy": v is None} for v, c in rows]
            return self._result(kind, {"categories": categories}, started, rows_returned=len(rows))

        if kind == "length_stats":
            self._need(col_sql)
            rows = await self._fetch(
                config,
                f"""
                WITH lens AS (
                    SELECT length(CAST({col_sql} AS TEXT)) AS l
                    FROM {qual}{where_sql}
                    WHERE {col_sql} IS NOT NULL
                ),
                ranked AS (
                    SELECT l,
                           ROW_NUMBER() OVER (ORDER BY l) AS rn,
                           COUNT(*) OVER () AS n
                    FROM lens
                )
                SELECT (SELECT min(l) FROM ranked),
                       (SELECT max(l) FROM ranked),
                       (SELECT max(n) FROM ranked),
                       (SELECT l FROM ranked
                         WHERE rn = MAX(1, CAST((n * 50 + 99) / 100 AS INTEGER))),
                       (SELECT l FROM ranked
                         WHERE rn = MAX(1, CAST((n * 90 + 99) / 100 AS INTEGER)))
                """,
                wparams,
            )
            lo, hi, _n, p50, p90 = rows[0]
            data = {}
            if lo is not None:
                data = {"min": int(lo), "max": int(hi)}
                if p50 is not None:
                    data["p50"] = int(p50)
                if p90 is not None:
                    data["p90"] = int(p90)
            return self._result(kind, data, started)

        if kind == "full_scan_values":
            # only reached when the planner has explicit full-scan approval
            self._need(col_sql)
            rows = await self._fetch(config, f"SELECT {col_sql} FROM {qual}{where_sql}", wparams)
            return self._result(
                kind, {"values": [r[0] for r in rows]}, started, rows_returned=len(rows)
            )

        raise UnsupportedError(f"unsupported profile query kind: {kind}")

    async def sample_rows(
        self, config: SourceConfig, request: SampleRequest
    ) -> AsyncIterator[EphemeralRow]:
        safety_check_dataset(request.dataset.qualified_name)
        _ns, name = split_qualified(request.dataset.qualified_name, config.default_schema)
        qual = quote_ident(name)

        cols_sql = "*"
        if request.columns:
            cols_sql = ", ".join(quote_ident(c) for c in request.columns)

        where_sql, where_params = build_where(
            request.predicate, quote=lambda c: quote_ident(c), placeholder="?"
        )
        if request.method == "random":
            sql = f"SELECT {cols_sql} FROM {qual}{where_sql} ORDER BY random() LIMIT ?"
            params: tuple[Any, ...] = (*where_params, request.limit)
        else:
            order_col = await self._resolve_order_col(config, name, request)
            sql = (
                f"SELECT {cols_sql} FROM {qual}{where_sql} "
                f"ORDER BY {quote_ident(order_col)} LIMIT ? OFFSET ?"
            )
            params = (*where_params, request.limit, request.offset)

        assert_not_dangerous_sql(sql)
        try:
            async with self._acquire(config) as conn:
                names, rows = await asyncio.to_thread(_run_query_named, conn, sql, params)
                for row in rows:
                    if request.cancel is not None and request.cancel.cancelled:
                        request.cancel.raise_if_cancelled()
                    yield EphemeralRow(values=dict(zip(names, row, strict=False)))
                    await asyncio.sleep(0)
        except CancelledError:
            raise
        except sqlite3.Error as exc:
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

    async def _resolve_order_col(
        self, config: SourceConfig, name: str, request: SampleRequest
    ) -> str:
        """Resolve a deterministic ORDER BY column for stable-key sampling.

        Requested columns win (matching the other SQL connectors). When the
        caller names none -- e.g. ``sanitize preview``, which passes
        ``columns=None`` -- SQLite's implicit ``rowid`` is used, because it
        is stable for the life of the table. ``WITHOUT ROWID`` tables have
        no ``rowid``, so those fall back to the first declared column and,
        failing that, to the documented error.
        """
        if request.columns:
            return request.columns[0]
        if await asyncio.to_thread(self._has_rowid, config, name):
            return "rowid"
        rows = await self._fetch_meta(config, "table_info", name)
        if rows:
            return str(rows[0][1])
        raise UnsupportedError("stable-key sampling requires a deterministic order column")

    def _has_rowid(self, config: SourceConfig, name: str) -> bool:
        """True when the table exposes SQLite's implicit ``rowid``."""
        try:
            conn = self._open_sync(config)
        except Exception:
            return False
        try:
            # SELECT rowid FROM t LIMIT 0 fails on WITHOUT ROWID tables
            conn.execute(f'SELECT "rowid" FROM {quote_ident(name)} LIMIT 0').fetchall()
            return True
        except sqlite3.Error:
            return False
        finally:
            conn.close()


def _deny_write(
    action: int, arg1: str | None, arg2: str | None, db: str | None, trigger: str | None
) -> int:
    """Statement authorizer: allow reads, deny everything else.

    ``mode=ro`` already refuses writes; this is defense in depth against
    ``ATTACH`` and state-changing ``PRAGMA`` reaching outside the single
    database file. Only the read-only introspection pragmas on the
    allowlist are permitted.
    """
    del arg2, db, trigger
    if action == sqlite3.SQLITE_PRAGMA:
        # allowlist is exact-match and case-insensitive on the pragma name
        return (
            sqlite3.SQLITE_OK
            if (arg1 or "").lower() in _SQLITE_READONLY_PRAGMAS
            else sqlite3.SQLITE_DENY
        )
    if action in _SQLITE_DENY_ACTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _run_query(
    conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]
) -> list[tuple[Any, ...]]:
    """Execute one statement and materialize rows (blocking)."""
    return _run_query_named(conn, sql, params)[1]


def _run_query_named(
    conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Execute one statement, returning (column_names, rows).

    ``description`` lives on the cursor, not the connection, so it has to be
    captured before the cursor is closed (sampling needs the names to build
    row dicts).
    """
    cur = conn.execute(sql, params)
    try:
        names = [d[0] for d in cur.description or []]
        return names, list(cur.fetchall())
    finally:
        cur.close()


def _uri_path(path: Path) -> str:
    """Percent-encode a path for a SQLite ``file:`` URI."""
    from urllib.parse import quote

    return quote(str(path))


def _fingerprint(name: str, columns: list[str]) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update(f"sqlite|main|{name}|{','.join(columns)}".encode())
    return f"sq_{h.hexdigest()[:24]}"


log = get_logger("sqlite")
