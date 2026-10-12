"""JDBC bridge protocol (SPEC 4.3, ADR-006).

Newline-delimited JSON over subprocess stdin/stdout. stdout carries
protocol messages only; diagnostics go to stderr. Every request has a
protocol version and request ID; responses carry status and structured
errors. SQL is always built by the Python connector; the sidecar only
executes parameterized statements it receives. Secrets travel via the
inherited environment, never argv.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

PROTOCOL_VERSION = 1

# Request operations supported by the sidecar
OP_PING = "ping"
OP_CONNECT = "connect"
OP_CLOSE = "close"
OP_METADATA_DATASETS = "metadata.datasets"
OP_METADATA_COLUMNS = "metadata.columns"
OP_EXECUTE = "query.execute"
OP_CANCEL = "job.cancel"
OP_SHUTDOWN = "shutdown"

KNOWN_OPS = frozenset(
    {
        OP_PING,
        OP_CONNECT,
        OP_CLOSE,
        OP_METADATA_DATASETS,
        OP_METADATA_COLUMNS,
        OP_EXECUTE,
        OP_CANCEL,
        OP_SHUTDOWN,
    }
)


class BridgeProtocolError(Exception):
    """Malformed or version-mismatched bridge message."""


@dataclass
class BridgeRequest:
    op: str
    request_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    protocol_version: int = PROTOCOL_VERSION
    deadline_ms: int = 30_000

    def encode(self) -> str:
        return json.dumps(
            {
                "protocol_version": self.protocol_version,
                "request_id": self.request_id,
                "op": self.op,
                "deadline_ms": self.deadline_ms,
                "payload": self.payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass
class BridgeResponse:
    request_id: str
    status: str  # "ok" | "error"
    payload: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    protocol_version: int = PROTOCOL_VERSION

    @classmethod
    def decode(cls, line: str) -> BridgeResponse:
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BridgeProtocolError(f"non-JSON bridge line: {exc}") from exc
        if not isinstance(raw, dict):
            raise BridgeProtocolError("bridge response must be a JSON object")
        version = raw.get("protocol_version")
        if version != PROTOCOL_VERSION:
            raise BridgeProtocolError(
                f"bridge protocol version mismatch: got {version!r}, expected {PROTOCOL_VERSION}"
            )
        request_id = raw.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise BridgeProtocolError("bridge response missing request_id")
        status = raw.get("status")
        if status not in ("ok", "error"):
            raise BridgeProtocolError(f"bridge response bad status: {status!r}")
        error = raw.get("error")
        if error is not None and not isinstance(error, dict):
            raise BridgeProtocolError("bridge error must be an object")
        payload = raw.get("payload") or {}
        if not isinstance(payload, dict):
            raise BridgeProtocolError("bridge payload must be an object")
        return cls(
            request_id=request_id,
            status=status,
            payload=payload,
            error=error,
            protocol_version=version,
        )

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def validate_request_payload(op: str, payload: dict[str, Any]) -> None:
    """Server-side (Python) validation of what we send to the sidecar.

    Defense in depth: SQL must be a SELECT/WITH, parameters must be
    scalars, and limits must be present (SPEC 4.3).
    """
    if op not in KNOWN_OPS:
        raise BridgeProtocolError(f"unknown bridge op: {op}")
    if op == OP_EXECUTE:
        sql = payload.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise BridgeProtocolError("query.execute requires sql")
        stripped = sql.strip().lstrip("( ").lower()
        if not (stripped.startswith("select") or stripped.startswith("with")):
            raise BridgeProtocolError("only SELECT/WITH may cross the bridge")
        params = payload.get("params", [])
        if not isinstance(params, list):
            raise BridgeProtocolError("params must be a list")
        for p in params:
            if isinstance(p, (dict, list)):
                raise BridgeProtocolError("query params must be scalars")
        max_rows = payload.get("max_rows")
        if not isinstance(max_rows, int) or max_rows < 0:
            raise BridgeProtocolError("query.execute requires non-negative max_rows")
        if "max_bytes" not in payload:
            raise BridgeProtocolError("query.execute requires max_bytes cap")
    if op == OP_CONNECT:
        # secrets must never be here: they arrive via the environment
        for banned in ("password", "secret", "token", "connection_string"):
            if banned in payload:
                raise BridgeProtocolError(f"payload must not carry '{banned}'; use environment")


def build_execute(
    request_id: str,
    *,
    sql: str,
    params: list[Any],
    max_rows: int,
    max_bytes: int,
    deadline_ms: int = 30_000,
) -> BridgeRequest:
    payload = {
        "sql": sql,
        "params": params,
        "max_rows": max_rows,
        "max_bytes": max_bytes,
        "fetch_size": min(max_rows, 500),
    }
    validate_request_payload(OP_EXECUTE, payload)
    return BridgeRequest(
        op=OP_EXECUTE, request_id=request_id, payload=payload, deadline_ms=deadline_ms
    )


def error_payload(code: str, message: str, **detail: str) -> dict[str, Any]:
    """Structured error safe for logs (redacted by callers)."""
    return {"code": code, "message": message, "detail": detail}


def as_wire(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    raise TypeError("not a dataclass")
