"""Shared SQL-connector scaffolding: identifier quoting, safety helpers,
read-only guards, and async wrappers for synchronous DB-API drivers
(SEPC 6.1, 6.2, 6.3).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from typing import Any

from qc.domain.errors import InvalidArgumentsError, SourceError, UnsupportedError

# Conservative identifier: what we allow into DDL/DML templates after quoting.
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
MAX_IDENTIFIER_LEN = 128


def quote_ident(name: str, style: str = "double") -> str:
    """Quote an identifier; rejects anything not matching the safe pattern.

    Defense in depth: even though we quote, we also validate, so a
    malicious *dataset name* from config cannot break out (SPEC 16.5
    SQL injection through dataset names).
    """
    if not isinstance(name, str) or not _SAFE_IDENT.match(name) or len(name) > MAX_IDENTIFIER_LEN:
        raise InvalidArgumentsError(
            "unsafe identifier rejected",
            detail={"identifier": name[:64]},
        )
    if style == "double":
        return f'"{name}"'
    return f"`{name}`"


def split_qualified(ref: str, default_schema: str | None = None) -> tuple[str | None, str]:
    """Split 'schema.table' safely; validates both parts."""
    if "." in ref:
        ns, _, name = ref.rpartition(".")
        if not _SAFE_IDENT.match(ns) or not _SAFE_IDENT.match(name):
            raise InvalidArgumentsError("unsafe qualified name", detail={"name": ref[:64]})
        return ns, name
    if not _SAFE_IDENT.match(ref):
        raise InvalidArgumentsError("unsafe table name", detail={"name": ref[:64]})
    return default_schema, ref


def run_sync(fn, *args: Any, **kwargs: Any) -> Any:  # type: ignore[no-untyped-def]
    """Run a blocking driver call (connectors run inside a thread pool)."""
    return fn(*args, **kwargs)


class QueryTimer:
    """Measure wall time of a driver call for budget accounting."""

    def __enter__(self) -> QueryTimer:
        self.start = time.monotonic()
        self.duration = 0.0
        return self

    def __exit__(self, *exc: object) -> None:
        self.duration = time.monotonic() - self.start


def clamp_limit(requested: int | None, hard_max: int) -> int:
    if requested is None:
        return hard_max
    if requested < 0:
        raise InvalidArgumentsError("limit must be >= 0")
    return min(requested, hard_max)


def map_db_error(exc: Exception, *, connector: str) -> SourceError:
    """Map driver errors to structured, redacted SourceError (SPEC 6.1)."""
    from qc.security.redaction import safe_error_message

    code = getattr(exc, "sqlstate", None) or getattr(exc, "errno", None)
    detail: dict[str, str] = {"connector": connector}
    if code is not None:
        detail["provider_code"] = str(code)
    return SourceError(
        safe_error_message(exc) or "query failed",
        detail=detail,
    )


def require_rows_budget(rows: int, limit: int) -> int:
    return clamp_limit(rows, limit)


def chunked(seq: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def normalize_logical_from_source(source_type: str, dialect: str) -> tuple[str, Any]:
    """Map a source type string to (normalized_source_type, hints).

    The logical type itself is decided by inference (SPEC 7.6); here we
    only normalize spelling across dialects.
    """
    t = source_type.lower().split("(")[0].strip()
    if dialect == "mysql":
        mapping = {
            "int": "integer",
            "tinyint": "integer",
            "smallint": "integer",
            "mediumint": "integer",
            "bigint": "bigint",
            "decimal": "decimal",
            "numeric": "decimal",
            "float": "float",
            "double": "double",
            "varchar": "varchar",
            "char": "char",
            "text": "text",
            "longtext": "text",
            "json": "json",
            "datetime": "datetime",
            "timestamp": "timestamp",
            "date": "date",
            "time": "time",
            "bit": "bit",
            "boolean": "boolean",
            "bool": "boolean",
            "blob": "blob",
        }
        return mapping.get(t, t), None
    # postgres / default
    mapping = {
        "integer": "integer",
        "bigint": "bigint",
        "smallint": "smallint",
        "numeric": "numeric",
        "decimal": "numeric",
        "real": "real",
        "double precision": "double",
        "character varying": "varchar",
        "varchar": "varchar",
        "character": "char",
        "text": "text",
        "jsonb": "jsonb",
        "json": "json",
        "timestamp with time zone": "timestamptz",
        "timestamp without time zone": "timestamp",
        "boolean": "boolean",
        "uuid": "uuid",
        "bytea": "bytea",
        "date": "date",
        "time": "time",
    }
    return mapping.get(t, t), None


def assert_not_dangerous_sql(sql: str) -> None:
    """Last-resort guard: only SELECT/WITH statements reach the wire.

    The Python connector builds all SQL; this catches template bugs
    (SPEC 4.3: no SQL without validation).
    """
    stripped = sql.strip().lstrip("( ").lower()
    if not (stripped.startswith("select") or stripped.startswith("with")):
        raise UnsupportedError("only SELECT/WITH statements may be executed")


class IdentifierMap(dict[str, str]):
    """Case-insensitive column lookup preserving original spelling."""

    def __init__(self, names: list[str]) -> None:
        super().__init__({n.lower(): n for n in names})
        self.original = list(names)

    def original_of(self, lower: str) -> str | None:
        return self.get(lower)


def safety_check_dataset(ref: str) -> None:
    """Validate dataset reference before any SQL is composed."""
    if any(ch in ref for ch in (";", "--", "/*", "*/", "\x00")):
        raise InvalidArgumentsError(
            "dataset reference contains illegal characters", detail={"ref": ref[:64]}
        )


def build_where(
    predicate: dict[str, Any] | None,
    *,
    quote: Callable[[str], str],
    placeholder: str = "%s",
) -> tuple[str, list[Any]]:
    """Translate a typed predicate into a parameterized WHERE clause.

    Returns ``("", [])`` when there is no predicate, otherwise
    ``(" WHERE ...", [params...])``. Column names are validated and
    quoted; values are always bound as parameters. ``and`` predicates
    combine recursively. This is the *only* path by which CLI/MCP
    predicates reach SQL (SPEC 11.2, 16.5).
    """
    if not predicate:
        return "", []
    clauses: list[str] = []
    params: list[Any] = []
    for clause in _clauses(predicate):
        col = clause.get("column")
        op = clause.get("op", "=")
        if not isinstance(col, str) or not _SAFE_IDENT.match(col):
            raise InvalidArgumentsError("unsafe predicate column", detail={"column": str(col)[:64]})
        qcol = quote(col)
        if op == "in":
            values = clause.get("values") or []
            if not values:
                raise InvalidArgumentsError("'in' predicate needs at least one value")
            marks = ", ".join(placeholder for _ in values)
            clauses.append(f"{qcol} IN ({marks})")
            params.extend(values)
        elif op in ("=", "!=", "<", "<=", ">", ">="):
            sql_op = "<>" if op == "!=" else op
            clauses.append(f"{qcol} {sql_op} {placeholder}")
            params.append(clause.get("value"))
        elif op == "like":
            clauses.append(f"{qcol} LIKE {placeholder}")
            params.append(clause.get("value"))
        else:
            raise InvalidArgumentsError(f"unsupported predicate operator '{op}'")
    return " WHERE " + " AND ".join(clauses), params


def _clauses(predicate: dict[str, Any]) -> list[dict[str, Any]]:
    if predicate.get("op") == "and":
        out: list[dict[str, Any]] = []
        for c in predicate.get("clauses", []):
            out.extend(_clauses(c))
        return out
    return [predicate]
