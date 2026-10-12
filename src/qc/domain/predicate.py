"""Typed predicate parsing (SPEC 11.2).

CLI ``--where`` accepts only a *typed* predicate form, never raw SQL:

    col=value              equality
    col:op:value           op in {=,!=,<,<=,>,>=,like,in}

Values are parsed to int/float/bool/None where unambiguous and otherwise
kept as strings; connectors bind them as parameters. Anything that could
be SQL is rejected.
"""

from __future__ import annotations

from typing import Any

from qc.domain.errors import InvalidArgumentsError

_ALLOWED_OPS = {"=", "!=", "<", "<=", ">", ">=", "like", "in"}
_FORBIDDEN = set(";'\"`\\\x00")


def _coerce(raw: str) -> Any:
    text = raw.strip()
    if text == "":
        return ""
    low = text.lower()
    if low in ("null", "none"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def parse_predicate(spec: str, allowed_columns: set[str] | None = None) -> dict[str, Any]:
    """Parse one ``--where`` expression into a typed predicate dict."""
    if not spec or not spec.strip():
        raise InvalidArgumentsError("empty --where expression")
    if any(ch in _FORBIDDEN for ch in spec):
        raise InvalidArgumentsError(
            "predicate contains illegal characters; use the typed form col=value or col:op:value"
        )

    if ":" in spec:
        parts = spec.split(":", 2)
        if len(parts) != 3:
            raise InvalidArgumentsError(f"predicate '{spec}' must be col:op:value or col=value")
        column, op, raw_value = parts[0].strip(), parts[1].strip().lower(), parts[2]
    elif "=" in spec:
        column, raw_value = (p.strip() for p in spec.split("=", 1))
        op = "="
    else:
        raise InvalidArgumentsError(f"predicate '{spec}' must be col=value or col:op:value")

    if op not in _ALLOWED_OPS:
        raise InvalidArgumentsError(
            f"unsupported predicate operator '{op}'; allowed: {sorted(_ALLOWED_OPS)}"
        )
    if not column or not (column.replace("_", "").replace(".", "").isalnum()):
        raise InvalidArgumentsError(f"invalid predicate column: {column!r}")
    if allowed_columns is not None and column not in allowed_columns:
        raise InvalidArgumentsError(
            f"predicate column '{column}' is not in the profiled column set"
        )

    if op == "in":
        values = [_coerce(v) for v in raw_value.split(",")]
        if not values:
            raise InvalidArgumentsError("'in' predicate requires at least one value")
        return {"column": column, "op": op, "values": values}
    if op == "like":
        pattern = raw_value
        if any(ch in pattern for ch in (";", "\x00")):
            raise InvalidArgumentsError("illegal characters in 'like' pattern")
        return {"column": column, "op": op, "value": pattern}
    return {"column": column, "op": op, "value": _coerce(raw_value)}


def parse_predicates(specs: list[str], allowed_columns: set[str] | None = None) -> dict[str, Any]:
    """Parse multiple ``--where`` flags into one AND-combined predicate.

    Returns a mapping consumed by connectors: ``{"clauses": [...]}`` with
    an implicit AND. A single clause is returned unwrapped for
    compatibility with the simple equality form.
    """
    clauses = [parse_predicate(s, allowed_columns) for s in specs]
    if len(clauses) == 1:
        return clauses[0]
    return {"op": "and", "clauses": clauses}


def matches_predicate(value: Any, predicate: dict[str, Any]) -> bool:
    """Evaluate a typed predicate against one value (in-memory connectors).

    Used by the fixture and spreadsheet connectors, which filter rows
    client-side instead of composing SQL. Comparison is type-aware and
    never executes code from the predicate.
    """
    if not predicate:
        return True
    if predicate.get("op") == "and":
        return all(matches_predicate(value, c) for c in predicate.get("clauses", []))
    op = predicate.get("op", "=")
    target = predicate.get("value")
    if op == "in":
        return any(_eq(value, v) for v in predicate.get("values", []))
    if op == "like":
        return _like(value, str(target))
    if op == "=":
        return _eq(value, target)
    if op == "!=":
        return not _eq(value, target)
    # ordering comparisons: only for comparable numeric/str values
    try:
        if value is None or target is None:
            return False
        if op == "<":
            return bool(value < target)
        if op == "<=":
            return bool(value <= target)
        if op == ">":
            return bool(value > target)
        if op == ">=":
            return bool(value >= target)
    except TypeError:
        return False
    return False


def matches_row(row: dict[str, Any], predicate: dict[str, Any] | None) -> bool:
    """Evaluate a predicate against a row (clauses carry their own column)."""
    if not predicate:
        return True
    if predicate.get("op") == "and":
        return all(matches_row(row, c) for c in predicate.get("clauses", []))
    return matches_predicate(row.get(predicate["column"]), predicate)


def _eq(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b if isinstance(a, bool) and isinstance(b, bool) else a == b
    # numeric coercion so 1 == 1.0 and "1" == 1
    try:
        if isinstance(a, (int, float)) or isinstance(b, (int, float)):
            return float(a) == float(b)
    except (TypeError, ValueError):
        pass
    return bool(a == b)


def _like(value: Any, pattern: str) -> bool:
    """SQL LIKE semantics with % and _ wildcards; case-insensitive."""
    if value is None:
        return False
    import fnmatch

    # translate SQL wildcards to fnmatch ones
    fn_pattern = pattern.replace("%", "*").replace("_", "?")
    return fnmatch.fnmatchcase(str(value).lower(), fn_pattern.lower())


__all__ = ["matches_predicate", "matches_row", "parse_predicate", "parse_predicates"]
