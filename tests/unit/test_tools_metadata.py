"""The server-published ``dev.adaptivemcp/tools-metadata`` resource (SEP-2133).

QC publishes a *static, advisory* policy per tool: risk, owner, tags and which
calls deserve a human confirmation. These tests pin the properties that make it
safe to publish: it is complete, it carries only server-authored fields, it
never contradicts itself (a tool that asks for confirmation is high risk), and
it renders identically every time so a host can cache it.
"""

from __future__ import annotations

import yaml

from qc.mcp.metadata import (
    DOCUMENT,
    TOOLS_METADATA_MIME,
    TOOLS_METADATA_NAME,
    TOOLS_METADATA_URI,
    AnnotationSuggestion,
    ToolsMetadataDocument,
    build_document,
    render_tools_metadata,
)
from qc.mcp.schemas import TOOL_SPECS

TOOL_NAMES = {spec.name for spec in TOOL_SPECS}
CLIENT_REPORTED_FIELDS = {"insights", "recommendations", "stats", "metrics"}
RISK_LEVELS = {"low", "medium", "high"}


def test_document_covers_exactly_the_v1_tool_set() -> None:
    assert {tool.name for tool in DOCUMENT.tools} == TOOL_NAMES
    assert len(DOCUMENT.tools) == len(TOOL_NAMES)
    assert DOCUMENT.version == "0.1.0"


def test_document_publishes_only_server_authored_fields() -> None:
    """No client-reported field may appear: QC never observes tool usage."""
    for tool in DOCUMENT.tools:
        payload = tool.model_dump()
        assert not CLIENT_REPORTED_FIELDS & set(payload["annotation"]), tool.name
        assert not CLIENT_REPORTED_FIELDS & set(payload), tool.name
    raw = yaml.safe_load(render_tools_metadata())
    assert not CLIENT_REPORTED_FIELDS & set(raw), "top level is static only"


def test_every_suggestion_is_well_formed() -> None:
    for tool in DOCUMENT.tools:
        annotation = tool.annotation
        assert annotation.risk in RISK_LEVELS, tool.name
        assert annotation.owner == "qc", tool.name
        assert isinstance(annotation.tags, list) and annotation.tags, tool.name


def test_confirmation_is_only_suggested_for_high_risk_tools() -> None:
    """A `require_approval` with a lower risk would be self-contradictory:
    hosts gate on risk, so the two signals have to agree."""
    for tool in DOCUMENT.tools:
        if tool.annotation.require_approval:
            assert tool.annotation.risk == "high", tool.name


def test_data_egress_and_artifact_writes_are_the_high_risk_tools() -> None:
    high_risk = {tool.name for tool in DOCUMENT.tools if tool.annotation.risk == "high"}
    assert high_risk == {"preview_sanitized_data", "generate_synthetic_data"}
    assert {tool.name for tool in DOCUMENT.tools if tool.annotation.require_approval} == high_risk


def test_rendered_yaml_round_trips_to_the_document() -> None:
    text = render_tools_metadata()
    assert TOOLS_METADATA_MIME == "application/yaml"
    parsed = ToolsMetadataDocument.model_validate(yaml.safe_load(text))
    assert parsed == DOCUMENT


def test_etag_is_stable_across_renders_and_processes() -> None:
    """Content is static, so a host can cache for the life of the server."""
    other = build_document()
    assert other.etag == DOCUMENT.etag
    assert len(DOCUMENT.etag) == 32


def test_annotation_suggestion_defaults() -> None:
    """Defaults stay on the safe side: low risk means no forced confirmation."""
    default = AnnotationSuggestion(risk="low")
    assert default.require_approval is False
    assert default.owner == "qc"
    assert default.tags == []


def test_wire_identifiers_match_the_extension() -> None:
    assert TOOLS_METADATA_URI == "dev.adaptivemcp://tools-metadata"
    assert TOOLS_METADATA_NAME == "tools-metadata"


async def test_register_publishes_the_document_as_a_resource() -> None:
    """The seam between this module and the MCP server: the resource is
    listed, and reading it yields the document in the advertised MIME type."""
    from mcp.server.fastmcp import FastMCP

    from qc.mcp.metadata import register_tools_metadata

    server = FastMCP(name="qc-test")
    register_tools_metadata(server)

    resources = await server.list_resources()
    assert [str(r.uri) for r in resources] == [TOOLS_METADATA_URI]
    assert resources[0].mimeType == TOOLS_METADATA_MIME

    contents = await server.read_resource(TOOLS_METADATA_URI)
    assert [c.mime_type for c in contents] == [TOOLS_METADATA_MIME]
    assert ToolsMetadataDocument.model_validate(yaml.safe_load(contents[0].content)) == DOCUMENT
