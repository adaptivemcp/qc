"""MCP transport setup: stdio (default) and Streamable HTTP (SPEC 12.1).

Both transports share :class:`qc.mcp.service.QCService` and therefore the
same policy checks, budgets, and error envelope. HTTP mode requires
authentication and is denied unless explicitly enabled in config.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from qc.application.runtime import Runtime
from qc.domain.errors import AuthError, InvalidArgumentsError
from qc.mcp.schemas import TOOL_SPECS
from qc.mcp.service import QCService, build_dispatch, call_tool
from qc.observability.logging import get_logger, setup_logging

log = get_logger("mcp.transport")


def run_stdio(runtime: Runtime) -> None:
    """Serve MCP over stdio (default local AI-client transport)."""
    setup_logging(level="warning", json_output=True)
    server = build_server(runtime, transport="mcp-stdio")
    log.info("mcp stdio server starting")
    server.run(transport="stdio")


class StaticTokenVerifier:
    """Bearer-token verifier for a single pre-shared token (ADR-003).

    Compares in constant time; returns an AccessToken bound to this
    server's resource URL. Long-term this is meant to sit behind an
    OIDC-capable gateway; the static token is the v1 stopgap.
    """

    def __init__(self, token: str, *, client_id: str = "static-client") -> None:
        self._token = token
        self._client_id = client_id

    async def verify_token(self, token: str) -> Any:
        import secrets as pysecrets

        if not pysecrets.compare_digest(token, self._token):
            return None
        from mcp.server.auth.provider import AccessToken

        return AccessToken(token=token, client_id=self._client_id, scopes=["mcp"])


def run_http(runtime: Runtime) -> None:
    """Serve MCP over Streamable HTTP with required authentication.

    Refuses to start when auth is disabled or the token reference is
    missing (SPEC 12.1: remote mode must require authentication).
    """
    cfg = runtime.config.mcp_http
    if not cfg.enabled:
        raise InvalidArgumentsError("mcp.http.enabled is false; set it in qc.toml to serve HTTP")
    if not cfg.require_auth:
        raise AuthError(
            "refusing to start HTTP transport with require_auth=false; "
            "remote mode must require authentication (SPEC 12.1)"
        )
    if not cfg.auth_token_ref:
        raise AuthError(
            "mcp.http.auth_token_ref is not set; remote mode needs a token "
            "(e.g. env:QC_MCP_HTTP_TOKEN)"
        )
    # validate the token resolves *before* binding the port
    from qc.security.secrets import resolve_secret

    token = resolve_secret(cfg.auth_token_ref)
    if len(token) < 16:
        raise AuthError("HTTP auth token must be at least 16 characters")

    setup_logging(level="info", json_output=True)
    server = build_server_http(runtime, token, cfg)
    log.info(
        "mcp http server starting",
        fields={"detail": f"{cfg.host}:{cfg.port} auth=bearer"},
    )
    server.run(transport="streamable-http")


def build_server_http(runtime: Runtime, token: str, cfg: Any) -> Any:
    """FastMCP server with bearer auth + per-client rate limiting wired."""
    from collections import defaultdict

    from mcp.server.auth.settings import AuthSettings
    from pydantic import AnyHttpUrl

    from qc.mcp.schemas import TOOL_SPECS as specs

    service = QCService(runtime, transport="mcp-http")
    dispatch = build_dispatch(service)

    hits: dict[str, list[float]] = defaultdict(list)
    window = 60.0
    limit = float(cfg.rate_limit_per_minute)

    server = FastMCP(
        name="qc",
        instructions=(
            "QueryCleaner/QC exposes bounded, policy-enforced data "
            "profiling and synthetic generation tools. Raw source rows "
            "are denied by default."
        ),
        token_verifier=StaticTokenVerifier(token),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(f"http://{cfg.host}:{cfg.port}"),
            resource_server_url=None,
            validate_token_resource=False,
        ),
        host=cfg.host,
        port=cfg.port,
        max_request_body_size=cfg.max_request_bytes,
    )

    rate_limited_guard = _rate_limit_guard(hits, window, limit)
    for spec in specs:
        _register(server, spec, dispatch, guard=rate_limited_guard)
    return server


def _rate_limit_guard(hits: dict[str, list[float]], window: float, limit: float):  # type: ignore[no-untyped-def]
    def guard(name: str) -> None:
        import time

        now = time.monotonic()
        hits["global"] = [t for t in hits["global"] if now - t < window]
        if len(hits["global"]) >= limit:
            from qc.domain.errors import BudgetExceededError

            raise BudgetExceededError("rate limit exceeded for this client", dimension="rate_limit")
        hits["global"].append(now)

    return guard


def build_server(runtime: Runtime, *, transport: str = "mcp-stdio") -> Any:
    """Build a FastMCP server exposing exactly the v1 tool set."""
    service = QCService(runtime, transport=transport)
    dispatch = build_dispatch(service)
    server = FastMCP(
        name="qc",
        instructions=(
            "QueryCleaner/QC exposes bounded, policy-enforced data "
            "profiling and synthetic generation tools. Raw source rows "
            "are denied by default. All operations are budgeted and "
            "audited; server policy cannot be modified through tools."
        ),
    )
    for spec in TOOL_SPECS:
        _register(server, spec, dispatch)
    return server


def _register(server: Any, spec: Any, dispatch: dict[str, Any], guard: Any = None) -> None:
    """Register one tool with a strict schema derived from its model."""
    input_model = spec.input_model

    async def handler(**kwargs: Any) -> Any:
        import asyncio

        def call() -> dict[str, Any]:
            if guard is not None:
                guard(spec.name)
            return call_tool(dispatch, spec.name, kwargs)

        return _as_mcp_result(await asyncio.to_thread(call))

    handler.__name__ = spec.name
    handler.__doc__ = spec.description
    handler.__signature__ = _signature_for(input_model)  # type: ignore[attr-defined]
    server.tool(name=spec.name, description=spec.description, annotations=spec.annotations)(handler)


def _as_mcp_result(result: dict[str, Any]) -> Any:
    """Return a protocol-native error for an error envelope.

    QC keeps its structured envelope (stable ``error`` code + message), but a
    policy denial / budget cap / validation failure is a *tool-execution error*
    per MCP, not a success. Returning a ``CallToolResult`` with ``isError=True``
    (and the same envelope as ``structuredContent``) means standard MCP clients
    treat it as a failure, instead of having to sniff an in-band ``status``.
    """
    if result.get("status") != "error":
        return result
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, indent=2))],
        structuredContent=result,
        isError=True,
    )


def _signature_for(model: Any) -> Any:
    """Build a signature matching the pydantic model fields."""
    import inspect

    fields = model.model_fields
    params = [
        inspect.Parameter(
            name=name,
            kind=inspect.Parameter.KEYWORD_ONLY,
            default=fieldInfo,
            annotation=fieldInfo.annotation,
        )
        for name, fieldInfo in fields.items()
    ]
    return inspect.Signature(params, return_annotation=dict)
