"""The conformance bearer proxy (tools/conformance/bearer_proxy.py).

QC requires a bearer token and the harness cannot send one, so the proxy is what makes
conformance evidence possible at all. These tests pin the three properties that matter:
it forwards the token, it refuses to expose that token beyond loopback, and it streams a
response instead of buffering it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

PROXY = Path(__file__).resolve().parents[2] / "tools" / "conformance" / "bearer_proxy.py"


class _Bytes(httpx.AsyncByteStream):
    """A one-chunk stream, so a mocked response behaves like a real streamed one."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self):
        if self._data:
            yield self._data


def _streamed(status: int = 200, data: bytes = b"", headers: dict[str, str] | None = None):
    return httpx.Response(status, headers=headers or {}, stream=_Bytes(data))


def _proxy() -> ModuleType:
    spec = importlib.util.spec_from_file_location("qc_bearer_proxy", PROXY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _client(module: ModuleType, handler) -> httpx.AsyncClient:
    app = module.create_app(
        "http://upstream.invalid",
        "secret-token",
        transport=httpx.MockTransport(handler),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.invalid"
    )


async def test_forwards_the_bearer_token_and_the_path() -> None:
    module = _proxy()
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization", "")
        seen["url"] = str(request.url)
        return _streamed(200, b'{"ok": true}', {"content-type": "application/json"})

    async with _client(module, handler) as client:
        response = await client.post("/mcp?session=1", json={"jsonrpc": "2.0"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert seen["authorization"] == "Bearer secret-token"
    assert seen["url"] == "http://upstream.invalid/mcp?session=1"


async def test_replaces_a_caller_supplied_authorization_header() -> None:
    module = _proxy()
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization", "")
        return _streamed(204)

    async with _client(module, handler) as client:
        # A client that tries its own token must not be able to override the proxy's.
        await client.get("/mcp", headers={"authorization": "Bearer attacker"})

    assert seen["authorization"] == "Bearer secret-token"


async def test_passes_the_response_through_without_hop_by_hop_headers() -> None:
    module = _proxy()

    def handler(request: httpx.Request) -> httpx.Response:
        return _streamed(
            200,
            b"event: message\ndata: {}\n\n",
            {"content-type": "text/event-stream", "transfer-encoding": "chunked"},
        )

    async with _client(module, handler) as client:
        response = await client.get("/mcp")

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    # Length is recomputed by the server; forwarding the upstream's would truncate a stream.
    assert "transfer-encoding" not in {k.lower() for k in response.headers}
    assert response.content == b"event: message\ndata: {}\n\n"


async def test_streams_the_body_rather_than_buffering_it() -> None:
    """An SSE stream never completes, so the proxy must emit chunks as they arrive."""
    import asyncio

    module = _proxy()

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in (b"one", b"two", b"three"):
                await asyncio.sleep(0)
                yield chunk

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())

    app = module.create_app("http://upstream.invalid", "t", transport=httpx.MockTransport(handler))
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    await app(
        {"type": "http", "method": "GET", "path": "/mcp", "headers": [], "query_string": b""},
        receive,
        send,
    )

    body_messages = [m for m in messages if m["type"] == "http.response.body"]
    streamed = [m["body"] for m in body_messages if m.get("more_body")]
    assert streamed == [b"one", b"two", b"three"]
    # The terminator carries the empty final body, not the content.
    assert body_messages[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


def test_refuses_to_bind_beyond_loopback() -> None:
    module = _proxy()

    for bad in ("0.0.0.0:3001", "192.168.1.5:3001", "example.com:3001"):
        with pytest.raises(ValueError, match="loopback"):
            module.parse_listen(bad)

    assert module.parse_listen("127.0.0.1:3001") == ("127.0.0.1", 3001)
    assert module.parse_listen("::1:3001") == ("::1", 3001)


def test_requires_a_token_from_the_environment() -> None:
    module = _proxy()

    with pytest.raises(ValueError, match="not set"):
        module.token_from_env({})
    with pytest.raises(ValueError, match="not set"):
        module.token_from_env({"QC_MCP_HTTP_TOKEN": "   "})

    assert module.token_from_env({"QC_MCP_HTTP_TOKEN": "abc"}) == "abc"
