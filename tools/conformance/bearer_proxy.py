#!/usr/bin/env python3
"""Loopback bearer-token proxy for the conformance harness.

The harness's server mode takes only ``--url`` — it cannot send an ``Authorization``
header — and QC refuses to serve HTTP without one (SPEC 12.1). This sits in front of QC,
adds the header, and changes nothing else: no TLS, no body rewriting, no logging of
content, and it refuses to bind anywhere but loopback. The point is to keep the security
guarantee under review intact rather than to add a "conformance mode" that disables it.

    QC_MCP_HTTP_TOKEN=… uv run qc mcp --transport http &
    QC_MCP_HTTP_TOKEN=… uv run python tools/conformance/bearer_proxy.py \\
        --listen 127.0.0.1:3001 --upstream http://127.0.0.1:8765

    npx @modelcontextprotocol/conformance server --url http://127.0.0.1:3001/mcp

The token is read from the environment, never from argv, so it does not appear in the
process list. Responses are streamed chunk-by-chunk because Streamable HTTP may hold an
SSE stream open; buffering would turn a working server into a timeout.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable
from typing import Any

import httpx

#: Only these hosts are acceptable for the listener. `0.0.0.0` would expose an
#: unauthenticated surface that forwards a bearer token — the exact thing this proxy
#: exists to avoid.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

#: Hop-by-hop headers are per-connection and must not be forwarded.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def parse_listen(value: str) -> tuple[str, int]:
    """Parse `host:port` and refuse anything that is not loopback."""
    host, _, port_text = value.rpartition(":")
    if not host or not port_text.isdigit():
        raise ValueError(f"--listen must be host:port, got {value!r}")
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"refusing to bind {host!r}: this proxy forwards a bearer token, so it may only "
            f"listen on loopback ({', '.join(sorted(LOOPBACK_HOSTS))})"
        )
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range: {port}")
    return host, port


def token_from_env(env: dict[str, str] | None = None, name: str = "QC_MCP_HTTP_TOKEN") -> str:
    """Read the token from the environment; empty is a refusal, not a default."""
    value = (env if env is not None else os.environ).get(name, "").strip()
    if not value:
        raise ValueError(
            f"{name} is not set; the proxy needs the token QC requires "
            "(export it, do not pass it on the command line)"
        )
    return value


def _request_headers(scope: dict[str, Any], token: str) -> list[tuple[bytes, bytes]]:
    headers: list[tuple[bytes, bytes]] = []
    for name, value in scope.get("headers", []):
        lowered = name.decode("latin-1").lower()
        if lowered in HOP_BY_HOP or lowered in {"host", "authorization", "content-length"}:
            continue
        headers.append((name, value))
    headers.append((b"authorization", f"Bearer {token}".encode("latin-1")))
    return headers


def _response_headers(headers: Iterable[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    kept: list[tuple[bytes, bytes]] = []
    for name, value in headers:
        lowered = name.decode("latin-1").lower()
        if lowered in HOP_BY_HOP or lowered == "content-length":
            continue
        kept.append((name, value))
    return kept


def create_app(
    upstream: str,
    token: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
):
    """An ASGI app that forwards to `upstream` with the bearer token attached."""
    base = upstream.rstrip("/")

    async def app(scope: dict[str, Any], receive, send) -> None:
        if scope["type"] != "http":
            # Lifespan and websocket are not needed to serve MCP over HTTP.
            if scope["type"] == "lifespan":
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            return

        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        url = f"{base}{scope.get('root_path', '')}{scope['path']}"
        if scope.get("query_string"):
            url += "?" + scope["query_string"].decode("latin-1")

        headers = _request_headers(scope, token)

        async with httpx.AsyncClient(transport=transport, timeout=None) as client:
            request = client.build_request(scope["method"], url, headers=headers, content=body)
            upstream_response = await client.send(request, stream=True)
            try:
                await send(
                    {
                        "type": "http.response.start",
                        "status": upstream_response.status_code,
                        "headers": _response_headers(upstream_response.headers.raw),
                    }
                )
                # Stream as it arrives: an SSE stream never "completes" until the client
                # disconnects, so buffering the body would hang the harness.
                async for chunk in upstream_response.aiter_raw():
                    if chunk:
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            finally:
                await upstream_response.aclose()

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:3001", help="host:port (loopback only)")
    parser.add_argument("--upstream", default="http://127.0.0.1:8765", help="QC's HTTP origin")
    parser.add_argument(
        "--token-env",
        default="QC_MCP_HTTP_TOKEN",
        help="environment variable holding the bearer token (default: QC_MCP_HTTP_TOKEN)",
    )
    args = parser.parse_args(argv)

    import uvicorn

    try:
        host, port = parse_listen(args.listen)
        token = token_from_env(name=args.token_env)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    app = create_app(args.upstream, token)
    # Access logging off on purpose: request paths can name datasets.
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
