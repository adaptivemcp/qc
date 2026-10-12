"""Destination database writers (SPEC 10.3).

Separate from source connectors: a *destination* is an explicitly
configured, separately-credentialed role that may write. Supported modes:
`create` (default), `append`, `replace`. Transactions are used where the
driver supports them so failures roll back (SPEC 10.3).
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

from qc.connectors.sql_common import quote_ident, split_qualified
from qc.domain.errors import InvalidArgumentsError, ValidationError, WriteError
from qc.domain.models import SourceConfig
from qc.security.redaction import safe_error_message
from qc.security.secrets import resolve_secret

_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

_SQL_TYPE_MAP: dict[str, str] = {
    "str": "text",
    "int": "bigint",
    "float": "double precision",
    "bool": "boolean",
    "NoneType": "text",
}


def _sql_literal_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "bigint"
    if isinstance(value, float):
        return "double precision"
    return "text"


class PostgresDestination:
    """Writable destination for PostgreSQL (SPEC 10.3)."""

    name = "postgres-destination"

    def __init__(self, *, connect_timeout: int = 10) -> None:
        self.connect_timeout = connect_timeout
        self._conn: Any = None

    async def _connect(self, config: SourceConfig) -> Any:
        if self._conn is not None:
            return self._conn
        try:
            import psycopg
        except ImportError as exc:
            raise WriteError("psycopg not installed; install qc[postgres]") from exc
        dsn = resolve_secret(config.connection_ref)
        try:
            self._conn = await psycopg.AsyncConnection.connect(
                dsn, connect_timeout=self.connect_timeout, autocommit=False
            )
        except Exception as exc:
            raise WriteError(f"destination connect failed: {safe_error_message(exc)}") from exc
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None

    async def write_rows(
        self,
        config: SourceConfig,
        table: str,
        columns: list[str],
        rows: list[dict[str, Any]],
        *,
        mode: str = "create",
    ) -> int:
        if mode not in ("create", "append", "replace"):
            raise ValidationError(f"invalid mode '{mode}'")
        if not rows:
            raise ValidationError("no rows to write")
        ns, name = split_qualified(table, config.default_schema)
        qual = f"{quote_ident(ns)}.{quote_ident(name)}" if ns else quote_ident(name)
        conn = await self._connect(config)
        try:
            if mode in ("create", "replace"):
                await conn.execute(f"DROP TABLE IF EXISTS {qual}")
                ddl = _ddl_postgres(table, config.default_schema, columns, rows)
                await conn.execute(ddl)
            col_sql = ", ".join(quote_ident(c) for c in columns)
            placeholders = ", ".join(["%s"] * len(columns))
            insert = f"INSERT INTO {qual} ({col_sql}) VALUES ({placeholders})"
            # table/column ids validated by quote_ident/split_qualified above
            cur = conn.cursor()
            for row in rows:
                await cur.execute(insert, tuple(row.get(c) for c in columns))
            await conn.commit()
            return len(rows)
        except Exception as exc:
            with contextlib.suppress(Exception):
                await conn.rollback()
            raise WriteError(f"destination write failed: {safe_error_message(exc)}") from exc

    # destination role never offers read APIs on this object


class MySQLDestination:
    """Writable destination for MySQL (SPEC 10.3)."""

    name = "mysql-destination"

    def __init__(self) -> None:
        self._conn: Any = None

    async def _connect(self, config: SourceConfig) -> Any:
        if self._conn is not None:
            return self._conn
        try:
            import mysql.connector
        except ImportError as exc:
            raise WriteError("mysql-connector-python not installed; install qc[mysql]") from exc
        from qc.connectors.mysql import _parse_mysql_dsn

        kwargs = _parse_mysql_dsn(resolve_secret(config.connection_ref))
        try:
            self._conn = mysql.connector.connect(**kwargs)
        except Exception as exc:
            raise WriteError(f"destination connect failed: {safe_error_message(exc)}") from exc
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None

    async def write_rows(
        self,
        config: SourceConfig,
        table: str,
        columns: list[str],
        rows: list[dict[str, Any]],
        *,
        mode: str = "create",
    ) -> int:
        if mode not in ("create", "append", "replace"):
            raise ValidationError(f"invalid mode '{mode}'")
        if not rows:
            raise ValidationError("no rows to write")
        ns, name = split_qualified(table, config.default_schema)
        qual = (
            f"{quote_ident(ns, 'backtick')}.{quote_ident(name, 'backtick')}"
            if ns
            else quote_ident(name, "backtick")
        )
        conn = await self._connect(config)
        try:
            cur = conn.cursor()
            if mode in ("create", "replace"):
                cur.execute(f"DROP TABLE IF EXISTS {qual}")
                ddl = _ddl_mysql(table, config.default_schema, columns, rows)
                cur.execute(ddl)
            col_sql = ", ".join(quote_ident(c, "backtick") for c in columns)
            placeholders = ", ".join(["%s"] * len(columns))
            insert = f"INSERT INTO {qual} ({col_sql}) VALUES ({placeholders})"
            for row in rows:
                cur.execute(insert, tuple(row.get(c) for c in columns))
            conn.commit()
            cur.close()
            return len(rows)
        except Exception as exc:
            with contextlib.suppress(Exception):
                conn.rollback()
            raise WriteError(f"destination write failed: {safe_error_message(exc)}") from exc


def _types_for(columns: list[str], rows: list[dict[str, Any]]) -> dict[str, str]:
    types: dict[str, str] = {}
    for row in rows:
        for c in columns:
            v = row.get(c)
            if v is None:
                continue
            t = _sql_literal_type(v)
            if c not in types:
                types[c] = t
            elif types[c] != t:
                types[c] = "text"
    return types


def _ddl_postgres(
    table: str, default_schema: str | None, columns: list[str], rows: list[dict[str, Any]]
) -> str:
    for c in columns:
        if not _SAFE_IDENT.match(c):
            raise InvalidArgumentsError("unsafe column name", detail={"column": c[:64]})
    ns, name = split_qualified(table, default_schema)
    qual = f"{quote_ident(ns)}.{quote_ident(name)}" if ns else quote_ident(name)
    types = _types_for(columns, rows)
    cols_sql = ", ".join(f"{quote_ident(c)} {types.get(c, 'text')}" for c in columns)
    return f"CREATE TABLE {qual} ({cols_sql})"


def _ddl_mysql(
    table: str, default_schema: str | None, columns: list[str], rows: list[dict[str, Any]]
) -> str:
    for c in columns:
        if not _SAFE_IDENT.match(c):
            raise InvalidArgumentsError("unsafe column name", detail={"column": c[:64]})
    ns, name = split_qualified(table, default_schema)
    qual = (
        f"{quote_ident(ns, 'backtick')}.{quote_ident(name, 'backtick')}"
        if ns
        else quote_ident(name, "backtick")
    )
    types = _types_for(columns, rows)
    mysql_types = {
        "bigint": "BIGINT",
        "double precision": "DOUBLE",
        "boolean": "TINYINT(1)",
        "text": "LONGTEXT",
    }
    cols_sql = ", ".join(
        f"{quote_ident(c, 'backtick')} {mysql_types.get(types.get(c, 'text'), 'LONGTEXT')}"
        for c in columns
    )
    return f"CREATE TABLE {qual} ({cols_sql})"
