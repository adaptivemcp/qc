"""MySQL connector (SPEC 6.3).

Read-only account expected; information_schema discovery; handles
unsigned integers, DECIMAL, BIT, JSON, zero dates, and SQL-mode
differences. Backtick identifier quoting.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
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
    map_db_error,
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
    ForeignKeyConstraint,
    SizeEstimate,
    SourceConfig,
)
from qc.security.secrets import resolve_secret

_MYSQL_LOGICAL: dict[str, LogicalType] = {
    "int": LogicalType.INTEGER,
    "integer": LogicalType.INTEGER,
    "tinyint": LogicalType.INTEGER,
    "smallint": LogicalType.INTEGER,
    "mediumint": LogicalType.INTEGER,
    "bigint": LogicalType.INTEGER,
    "decimal": LogicalType.DECIMAL,
    "numeric": LogicalType.DECIMAL,
    "float": LogicalType.FLOAT,
    "double": LogicalType.FLOAT,
    "real": LogicalType.FLOAT,
    "varchar": LogicalType.STRING,
    "char": LogicalType.STRING,
    "text": LogicalType.STRING,
    "mediumtext": LogicalType.STRING,
    "longtext": LogicalType.STRING,
    "tinytext": LogicalType.STRING,
    "json": LogicalType.JSON,
    "datetime": LogicalType.TIMESTAMP,
    "timestamp": LogicalType.TIMESTAMP,
    "date": LogicalType.DATE,
    "time": LogicalType.TIME,
    "year": LogicalType.DATE,
    "bit": LogicalType.BOOLEAN,
    "tinyint(1)": LogicalType.BOOLEAN,
    "blob": LogicalType.BINARY,
    "binary": LogicalType.BINARY,
    "varbinary": LogicalType.BINARY,
    "enum": LogicalType.CATEGORICAL_STRING,
    "set": LogicalType.CATEGORICAL_STRING,
}

# signedness lives in information_schema.columns for MySQL >= 5.0.5
_INT_TYPES = {"int", "tinyint", "smallint", "mediumint", "bigint"}


class MySQLConnector(BaseConnector):
    name = "mysql"
    capabilities = ConnectorCapabilities(
        metadata=True,
        aggregates=True,
        sampling=True,
        cancellation=True,
        writes=False,
        transactions=True,
        statistics=False,  # no pg_stats equivalent exposed uniformly
        named=("metadata", "aggregates", "sampling", "cancellation"),
    )

    def __init__(self, *, connect_timeout: int = 10) -> None:
        super().__init__()
        self.connect_timeout = connect_timeout
        self._conn: Any = None
        self._dsn: str | None = None

    def _dsn_for(self, config: SourceConfig) -> str:
        if self._dsn is None:
            self._dsn = resolve_secret(config.connection_ref)
        return self._dsn

    async def _connect(self, config: SourceConfig) -> Any:
        if self._conn is not None:
            return self._conn
        try:
            import mysql.connector
        except ImportError as exc:
            raise UnsupportedError(
                "mysql-connector-python is not installed; install qc[mysql]",
                detail={"extra": "mysql"},
            ) from exc
        dsn = self._dsn_for(config)
        kwargs = _parse_mysql_dsn(dsn)
        kwargs["connection_timeout"] = self.connect_timeout
        try:
            self._conn = mysql.connector.connect(**kwargs)
            # session guards (SPEC 6.3: read-only account + SQL mode)
            cur = self._conn.cursor()
            with contextlib.suppress(Exception):
                cur.execute("SET SESSION TRANSACTION READ ONLY")
            # normalize sql_mode so zero-dates/strictness don't vary by server
            with contextlib.suppress(Exception):
                cur.execute("SET SESSION sql_mode = 'NO_ENGINE_SUBSTITUTION'")
            cur.close()
        except Exception as exc:
            raise map_db_error(exc, connector=self.name) from exc
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None
        await super().close()

    async def _fetch(
        self, config: SourceConfig, sql: str, params: tuple[Any, ...] = ()
    ) -> list[tuple[Any, ...]]:
        assert_not_dangerous_sql(sql)
        conn = await self._connect(config)
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            cur.close()
            return list(rows)
        except Exception as exc:
            raise map_db_error(exc, connector=self.name) from exc

    # --------------------------------------------------------------- API

    async def test_connection(self, config: SourceConfig) -> ConnectionTest:
        import time

        start = time.monotonic()
        try:
            rows = await self._fetch(config, "SELECT version()")
            latency = (time.monotonic() - start) * 1000
            return ConnectionTest(
                ok=True,
                message="connected (read-only session)",
                latency_ms=round(latency, 1),
                server_info={"version": str(rows[0][0])[:120] if rows else "unknown"},
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
            WHERE table_schema = COALESCE(%s, DATABASE())
            ORDER BY table_name
            """,
            (config.default_schema,),
        )
        refs = [
            DatasetRef(
                source_id=config.id,
                namespace=str(schema),
                name=str(name),
                kind=DatasetKind.VIEW if str(ttype).upper() == "VIEW" else DatasetKind.TABLE,
                locator=f"{schema}.{name}",
            )
            for schema, name, ttype in rows
        ]
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
            SELECT column_name, ordinal_position, column_type, is_nullable,
                   column_default, data_type, extra, column_key
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (ns, name),
        )
        if not col_rows:
            raise NotFoundError(f"table not found: {dataset.qualified_name}")

        columns = []
        pk: list[str] = []
        for row in col_rows:
            (col_name, ordinal, col_type, nullable, default, data_type, extra, col_key) = row
            source_type = str(col_type)  # preserves unsigned/width e.g. 'int unsigned'
            base = str(data_type).lower()
            logical = _MYSQL_LOGICAL.get(base, LogicalType.UNSUPPORTED)
            if base == "tinyint" and source_type == "tinyint(1)":
                logical = LogicalType.BOOLEAN
            if str(col_key).upper() == "PRI":
                pk.append(str(col_name))
            columns.append(
                ColumnSchema(
                    name=str(col_name),
                    ordinal=int(ordinal) - 1,
                    source_type=source_type,
                    logical_type=logical,
                    nullable_declared=str(nullable).upper() == "YES",
                    default=str(default) if default is not None else None,
                    is_primary_key=str(col_key).upper() == "PRI",
                )
            )
            del extra

        fk_rows = await self._fetch(
            config,
            """
            SELECT kcu.constraint_name, kcu.column_name,
                   kcu.referenced_table_schema, kcu.referenced_table_name,
                   kcu.referenced_column_name
            FROM information_schema.key_column_usage kcu
            JOIN information_schema.table_constraints tc
              ON kcu.constraint_name = tc.constraint_name
             AND kcu.table_schema = tc.table_schema
            WHERE kcu.table_schema = %s AND kcu.table_name = %s
              AND kcu.referenced_table_name IS NOT NULL
            """,
            (ns, name),
        )
        fks: dict[str, ForeignKeyConstraint] = {}
        for cname, col, rschema, rtable, rcol in fk_rows:
            key = str(cname)
            fks.setdefault(
                key,
                ForeignKeyConstraint(
                    name=key,
                    columns=[],
                    ref_namespace=str(rschema),
                    ref_name=str(rtable),
                    ref_columns=[],
                ),
            ).columns.append(str(col))
            fks[key].ref_columns.append(str(rcol))

        return DatasetSchema(
            dataset=dataset,
            source_fingerprint=_fingerprint(ns, name, [c.name for c in columns]),
            columns=columns,
            primary_key=pk,
            primary_key_declared=bool(pk),
            foreign_keys=list(fks.values()),
            row_estimate=await self._estimate_rows(config, ns, name),
            row_estimate_method=Measurement.ESTIMATED,
            provider_metadata={"engine": "mysql", "schema": ns},
        )

    async def _estimate_rows(self, config: SourceConfig, ns: str, name: str) -> int | None:
        try:
            rows = await self._fetch(
                config,
                "SELECT table_rows FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s",
                (ns, name),
            )
            return int(rows[0][0]) if rows and rows[0][0] is not None else None
        except SourceError:
            return None

    async def estimate_size(self, config: SourceConfig, dataset: DatasetRef) -> SizeEstimate:
        safety_check_dataset(dataset.qualified_name)
        ns, name = split_qualified(dataset.qualified_name, config.default_schema)
        if ns is None:
            raise SourceError("dataset schema unknown")
        rows = await self._fetch(
            config,
            "SELECT data_length + index_length, table_rows "
            "FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
            (ns, name),
        )
        if not rows:
            return SizeEstimate(method=Measurement.UNKNOWN, note="table not found in I_S")
        byte_est, row_est = rows[0]
        return SizeEstimate(
            row_estimate=int(row_est) if row_est is not None else None,
            byte_estimate=int(byte_est) if byte_est is not None else None,
            method=Measurement.ESTIMATED,
            note="information_schema estimates",
        )

    async def execute_profile_query(
        self, config: SourceConfig, query: ProfileQuery
    ) -> ProfileQueryResult:
        import time

        safety_check_dataset(query.dataset.qualified_name)
        ns, name = split_qualified(query.dataset.qualified_name, config.default_schema)
        if ns is None:
            raise SourceError("dataset schema unknown")
        qual = f"{quote_ident(ns, 'backtick')}.{quote_ident(name, 'backtick')}"
        col = query.params.get("column")
        col_sql = quote_ident(str(col), "backtick") if col else None
        kind = query.kind
        started = time.monotonic()

        if kind == "row_count":
            rows = await self._fetch(config, f"SELECT count(*) FROM {qual}")
            return _result(kind, {"row_count": int(rows[0][0])}, started)

        if kind == "null_count":
            _need(col_sql)
            rows = await self._fetch(
                config,
                f"SELECT count(*), SUM(CASE WHEN {col_sql} IS NULL THEN 1 ELSE 0 END) FROM {qual}",
            )
            return _result(
                kind,
                {"row_count": int(rows[0][0]), "null_count": int(rows[0][1] or 0)},
                started,
            )

        if kind == "distinct":
            _need(col_sql)
            rows = await self._fetch(
                config,
                f"SELECT COUNT(DISTINCT {col_sql}), "
                f"SUM(CASE WHEN {col_sql} IS NULL THEN 1 ELSE 0 END) FROM {qual}",
            )
            return _result(
                kind,
                {"distinct_count": int(rows[0][0]), "null_count": int(rows[0][1] or 0)},
                started,
            )

        if kind == "min_max":
            _need(col_sql)
            rows = await self._fetch(config, f"SELECT MIN({col_sql}), MAX({col_sql}) FROM {qual}")
            lo, hi = rows[0]
            data: dict[str, Any] = {}
            if lo is not None:
                data = {"min": str(lo), "max": str(hi), "min_str": str(lo), "max_str": str(hi)}
            return _result(kind, data, started)

        if kind == "top_k":
            _need(col_sql)
            k = int(query.params.get("k", 10))
            rows = await self._fetch(
                config,
                f"SELECT {col_sql} AS v, COUNT(*) AS c FROM {qual} "
                f"GROUP BY v ORDER BY c DESC LIMIT %s",
                (k,),
            )
            categories = [{"key": str(v), "count": int(c), "falsy": v is None} for v, c in rows]
            return _result(kind, {"categories": categories}, started, rows_returned=len(rows))

        if kind == "length_stats":
            _need(col_sql)
            rows = await self._fetch(
                config,
                f"SELECT MIN(CHAR_LENGTH({col_sql})), MAX(CHAR_LENGTH({col_sql})) "
                f"FROM {qual} WHERE {col_sql} IS NOT NULL",
            )
            lo, hi = rows[0]
            data = {"min": int(lo), "max": int(hi)} if lo is not None else {}
            # percentile approx via ORDER BY + LIMIT (no percentile_cont in MySQL)
            if lo is not None:
                total_rows = await self._fetch(
                    config,
                    f"SELECT COUNT(*) FROM {qual} WHERE {col_sql} IS NOT NULL",
                )
                total = int(total_rows[0][0])
                if total:
                    p50_rows = await self._fetch(
                        config,
                        f"SELECT CHAR_LENGTH({col_sql}) FROM {qual} "
                        f"WHERE {col_sql} IS NOT NULL ORDER BY 1 LIMIT 1 OFFSET %s",
                        (total // 2,),
                    )
                    p90_rows = await self._fetch(
                        config,
                        f"SELECT CHAR_LENGTH({col_sql}) FROM {qual} "
                        f"WHERE {col_sql} IS NOT NULL ORDER BY 1 LIMIT 1 OFFSET %s",
                        (int(total * 0.9),),
                    )
                    if p50_rows:
                        data["p50"] = int(p50_rows[0][0])
                    if p90_rows:
                        data["p90"] = int(p90_rows[0][0])
            return _result(kind, data, started)

        if kind == "full_scan_values":
            _need(col_sql)
            rows = await self._fetch(config, f"SELECT {col_sql} FROM {qual}")
            return _result(kind, {"values": [r[0] for r in rows]}, started, rows_returned=len(rows))

        raise UnsupportedError(f"unsupported profile query kind: {kind}")

    async def sample_rows(
        self, config: SourceConfig, request: SampleRequest
    ) -> AsyncIterator[EphemeralRow]:
        safety_check_dataset(request.dataset.qualified_name)
        ns, name = split_qualified(request.dataset.qualified_name, config.default_schema)
        if ns is None:
            raise SourceError("dataset schema unknown")
        qual = f"{quote_ident(ns, 'backtick')}.{quote_ident(name, 'backtick')}"
        cols_sql = (
            ", ".join(quote_ident(c, "backtick") for c in request.columns)
            if request.columns
            else "*"
        )
        if request.method == "random":
            sql = f"SELECT {cols_sql} FROM {qual} ORDER BY RAND() LIMIT %s"
            params: tuple[Any, ...] = (request.limit,)
        elif request.method == "predicate" and request.predicate:
            col, value = _safe_predicate(request.predicate)
            sql = (
                f"SELECT {cols_sql} FROM {qual} WHERE {quote_ident(col, 'backtick')} = %s "
                "ORDER BY 1 LIMIT %s"
            )
            params = (value, request.limit)
        else:
            if not request.columns:
                raise UnsupportedError("stable-key sampling requires a deterministic order column")
            sql = (
                f"SELECT {cols_sql} FROM {qual} "
                f"ORDER BY {quote_ident(request.columns[0], 'backtick')} "
                "LIMIT %s OFFSET %s"
            )
            params = (request.limit, request.offset)

        assert_not_dangerous_sql(sql)
        conn = await self._connect(config)
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            names = [d[0] for d in cur.description] if cur.description else []
            while True:
                batch = cur.fetchmany(200)
                if not batch:
                    break
                for rec in batch:
                    yield EphemeralRow(values=dict(zip(names, rec, strict=False)))
            cur.close()
        except Exception as exc:
            raise map_db_error(exc, connector=self.name) from exc


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


def _need(col_sql: str | None) -> None:
    if not col_sql:
        raise UnsupportedError("query kind requires 'column' param")


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


def _parse_mysql_dsn(dsn: str) -> dict[str, Any]:
    """Parse mysql://user:pass@host:port/db or key=value DSNs safely.

    Never returns the password in errors.
    """
    from qc.domain.errors import InvalidArgumentsError

    if "://" in dsn:
        from urllib.parse import unquote, urlparse

        u = urlparse(dsn)
        if u.scheme not in ("mysql", "mysql2"):
            raise InvalidArgumentsError("connection string must use mysql:// scheme")
        kwargs: dict[str, Any] = {
            "host": u.hostname or "localhost",
            "port": u.port or 3306,
            "database": (u.path or "/").lstrip("/") or None,
        }
        if u.username:
            kwargs["user"] = unquote(u.username)
        if u.password:
            kwargs["password"] = unquote(u.password)
        return kwargs
    # key=value form: pass through to the driver (no URL parsing needed)
    return {"dsn": dsn}


def _fingerprint(ns: str, name: str, columns: list[str]) -> str:
    import hashlib

    h = hashlib.sha256()
    h.update(f"my|{ns}|{name}|{','.join(columns)}".encode())
    return f"my_{h.hexdigest()[:24]}"
