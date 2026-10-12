"""Fake JDBC sidecar for bridge contract tests (SPEC 4.3, ADR-006).

Speaks newline-delimited JSON on stdin/stdout exactly like the Java
sidecar, with diagnostics on stderr. Used because CI runners may lack a
JVM; the protocol contract is what we test from the Python side.
"""

from __future__ import annotations

import json
import sys

PROTOCOL_VERSION = 1

SCHEMAS = [
    {"schema": "analytics", "name": "orders", "kind": "table"},
    {"schema": "analytics", "name": "customers", "kind": "table"},
]

COLUMNS = [
    {"name": "id", "type": "bigint", "ordinal": 0, "primary_key": True, "nullable": False},
    {"name": "email", "type": "varchar(255)", "ordinal": 1, "nullable": True},
    {"name": "amount", "type": "decimal(18,4)", "ordinal": 2, "nullable": True},
    {"name": "created_at", "type": "timestamp", "ordinal": 3, "nullable": True},
]

# canned result for the demo SELECT
QUERY_RESULT = {
    "columns": ["id", "email"],
    "rows": [[1, "a@example.com"], [2, None], [3, "c@example.com"]],
    "truncated": False,
}


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def reply(
    req: dict, *, status: str = "ok", payload: dict | None = None, error: dict | None = None
) -> None:
    emit(
        {
            "protocol_version": PROTOCOL_VERSION,
            "request_id": req.get("request_id", "?"),
            "status": status,
            "payload": payload or {},
            "error": error,
        }
    )


def main() -> int:
    # diagnostics on stderr only (SPEC 4.3)
    print("fake sidecar started; protocol=1", file=sys.stderr, flush=True)
    connected = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(f"non-JSON request: {line[:60]}", file=sys.stderr, flush=True)
            continue
        version = req.get("protocol_version")
        if version != PROTOCOL_VERSION:
            reply(
                req,
                status="error",
                error={
                    "code": "PROTOCOL_MISMATCH",
                    "message": f"expected {PROTOCOL_VERSION}, got {version}",
                },
            )
            continue
        op = req.get("op")
        payload = req.get("payload") or {}

        if op == "ping":
            reply(req, payload={"pong": True, "java": "fake"})
        elif op == "connect":
            # secrets must arrive via environment, never payload
            for banned in ("password", "secret", "token", "connection_string"):
                if banned in payload:
                    reply(
                        req,
                        status="error",
                        error={"code": "SECRET_IN_PAYLOAD", "message": f"payload carries {banned}"},
                    )
                    continue
            if not payload.get("jdbc_url_env"):
                reply(
                    req,
                    status="error",
                    error={"code": "MISSING_URL", "message": "jdbc_url_env required"},
                )
                continue
            connected = True
            reply(req, payload={"connected": True})
        elif op == "metadata.datasets":
            if not connected:
                reply(
                    req, status="error", error={"code": "NOT_CONNECTED", "message": "connect first"}
                )
                continue
            reply(req, payload={"datasets": SCHEMAS})
        elif op == "metadata.columns":
            if payload.get("table") != "orders":
                reply(
                    req, status="error", error={"code": "NOT_FOUND", "message": "table not found"}
                )
                continue
            reply(req, payload={"columns": COLUMNS})
        elif op == "query.execute":
            sql = payload.get("sql", "")
            stripped = sql.lstrip("( ").lower()
            if not (stripped.startswith("select") or stripped.startswith("with")):
                reply(
                    req,
                    status="error",
                    error={"code": "NOT_SELECT", "message": "only SELECT allowed"},
                )
                continue
            max_rows = payload.get("max_rows")
            if not isinstance(max_rows, int):
                reply(
                    req,
                    status="error",
                    error={"code": "MISSING_CAP", "message": "max_rows required"},
                )
                continue
            rows = QUERY_RESULT["rows"][:max_rows]
            truncated = len(QUERY_RESULT["rows"]) > max_rows
            # echo result shape for COUNT-style queries when sql has COUNT(*)
            if "count(*)" in sql.lower() and "distinct" not in sql.lower():
                if "sum(case" in sql.lower():
                    reply(
                        req,
                        payload={
                            "columns": ["count", "nulls"],
                            "rows": [[3, 1]],
                            "truncated": False,
                        },
                    )
                else:
                    reply(req, payload={"columns": ["count"], "rows": [[42]], "truncated": False})
                continue
            if sql.lower().startswith("select min"):
                reply(
                    req,
                    payload={
                        "columns": ["min", "max"],
                        "rows": [["1.00", "99.00"]],
                        "truncated": False,
                    },
                )
                continue
            reply(
                req,
                payload={"columns": QUERY_RESULT["columns"], "rows": rows, "truncated": truncated},
            )
        elif op == "job.cancel":
            reply(req, payload={"cancelled": True})
        elif op == "shutdown":
            reply(req, payload={"bye": True})
            return 0
        else:
            reply(req, status="error", error={"code": "UNKNOWN_OP", "message": str(op)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
