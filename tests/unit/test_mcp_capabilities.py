"""What `initialize` promises must be what the server serves (qc#46).

The MCP SDK's FastMCP registers prompt, resource and completion handlers unconditionally,
so its default capability block advertised `prompts`, `resources` (with `subscribe`),
`logging` and `completions` on a server that implements none of them. A client that
trusts the block tries `prompts/list` or `resources/read` and gets an error — the
conformance suite failed exactly those scenarios.

These tests compare the advertised set with the registered one, in both directions.
"""

from __future__ import annotations

from pathlib import Path

from qc.application.runtime import Runtime
from qc.mcp.server import advertise_only_implemented, build_server


def _options(server):  # type: ignore[no-untyped-def]
    return server._mcp_server.create_initialization_options()


def test_stdio_advertises_tools_and_the_resource_it_serves(tmp_path: Path) -> None:
    server = build_server(Runtime(root=tmp_path))

    capabilities = _options(server).capabilities

    assert capabilities.tools is not None
    assert capabilities.resources is not None
    # Everything QC does not implement must be absent, not present-and-broken.
    assert capabilities.prompts is None
    assert capabilities.logging is None
    assert capabilities.completions is None
    assert capabilities.experimental is None


def test_the_resource_capability_matches_what_is_actually_listed(tmp_path: Path) -> None:
    import asyncio

    server = build_server(Runtime(root=tmp_path))
    listed = asyncio.run(server.list_resources())
    capabilities = _options(server).capabilities

    # `resources` is advertised because — and only while — there is something to list.
    assert listed, "the tools-metadata resource should be registered"
    assert capabilities.resources is not None
    assert any(str(resource.uri) == "dev.adaptivemcp://tools-metadata" for resource in listed)


def test_no_resource_means_no_resource_capability(tmp_path: Path) -> None:
    from mcp.server.fastmcp import FastMCP

    bare = FastMCP(name="bare")
    advertise_only_implemented(bare, resources=False)

    capabilities = _options(bare).capabilities

    # FastMCP always registers a tool handler, so `tools` stays advertised; what the flag
    # controls is the resource block, and the unimplemented blocks are gone either way.
    assert capabilities.resources is None
    assert capabilities.prompts is None
    assert capabilities.logging is None
    assert capabilities.completions is None


def test_tool_set_is_unchanged_by_the_trim(tmp_path: Path) -> None:
    import asyncio

    from qc.mcp.schemas import TOOL_SPECS

    server = build_server(Runtime(root=tmp_path))
    listed = asyncio.run(server.list_tools())

    # The trim must not touch the thing that is implemented.
    assert {tool.name for tool in listed} == {spec.name for spec in TOOL_SPECS}
