"""Server-published adaptive policy: the ``dev.adaptivemcp/tools-metadata`` resource (SEP-2133).

MCP standardizes the capability (a server *has* tools); adaptive hosts learn how
those tools behave. This module gives an adaptive host a static, server-authored
*suggestion* per tool — risk, owner, tags, and which calls deserve a human
confirmation — served through an ordinary MCP resource. Hosts that do not know
the extension read nothing and lose nothing: the document is advisory, and QC's
own enforcement (policy checks, budgets, audit) never depends on a client
reading it.

Deliberately excluded from the document:

- **Learned behaviour** (failure rates, latencies, insights). QC has no view of
  what a client does with a tool, and the SEP assigns those fields to the
  client, which MAY fold them back. A server that does not persist reports
  publishes the static annotation only.
- **Budgets.** The extension's ``budget`` is a *cost* guardrail the client
  spends against; QC's budgets are time/row/query ceilings enforced inside QC in
  units the client cannot convert. Publishing a number in the wrong unit would
  be worse than publishing none, so QC states its budgets in ``instructions``
  and leaves ``budget`` unset.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Literal

import yaml
from pydantic import AnyUrl, BaseModel, Field

from qc.mcp.schemas import TOOL_SPECS

#: Wire identifiers (SEP-2133: scheme-less extension id, URI form on the wire).
TOOLS_METADATA_URI = "dev.adaptivemcp://tools-metadata"
TOOLS_METADATA_MIME = "application/yaml"
TOOLS_METADATA_NAME = "tools-metadata"
#: Document schema version, independent of the QC tool version.
DOCUMENT_VERSION = "0.1.0"

#: QC's owner string, so a host can tell which server suggested a policy.
OWNER = "qc"

RiskLevel = Literal["low", "medium", "high"]


class AnnotationSuggestion(BaseModel):
    """Static, server-authored suggestion for one tool. No learned data."""

    risk: RiskLevel
    owner: str = OWNER
    tags: list[str] = Field(default_factory=list)
    #: Ask a human before calling. Hosts enforce this however they like; QC
    #: never sees the answer, and the tool stays callable by policy either way.
    require_approval: bool = False


class ToolSuggestion(BaseModel):
    name: str
    annotation: AnnotationSuggestion


class ToolsMetadataDocument(BaseModel):
    """The published document (schema in the SEP; client-reported fields omitted)."""

    version: str
    etag: str
    generated_at: str
    tools: list[ToolSuggestion]


READ = ["read"]
_DISCOVERY = ["discovery", "read"]
_PROFILING = ["profiling", "job"]
_SYNTHETIC = ["synthetic", "write-artifact"]
_JOB_CONTROL = ["job-control"]

#: QC's risk call per tool, explicit for every tool so a new tool cannot ship
#: without one (mirrors ``TOOL_ANNOTATIONS`` in schemas.py).
#:
#: Deliberately independent of the MCP annotations: those say *read-only vs
#: artifact-writing* to any MCP client, whereas this says what an adaptive
#: *agent* should be careful about — including the two calls that move data out
#: of QC. Both high-risk tools also set ``require_approval``; a suggestion to
#: confirm without the risk to match it would be contradictory, and hosts gate
#: on risk.
_TOOL_POLICIES: dict[str, AnnotationSuggestion] = {
    "list_sources": AnnotationSuggestion(risk="low", tags=_DISCOVERY),
    "list_datasets": AnnotationSuggestion(risk="low", tags=_DISCOVERY),
    "get_dataset_schema": AnnotationSuggestion(risk="low", tags=["schema", *READ]),
    "profile_dataset": AnnotationSuggestion(risk="medium", tags=[*_PROFILING, "write-artifact"]),
    "get_profile": AnnotationSuggestion(risk="low", tags=[*_PROFILING, *READ]),
    "preview_sanitized_data": AnnotationSuggestion(
        risk="high", tags=["egress", "sanitized", *READ], require_approval=True
    ),
    "generate_synthetic_data": AnnotationSuggestion(
        risk="high", tags=_SYNTHETIC, require_approval=True
    ),
    "validate_synthetic_data": AnnotationSuggestion(risk="low", tags=[*_SYNTHETIC, *READ]),
    "get_job_status": AnnotationSuggestion(risk="low", tags=["job", *READ]),
    "cancel_job": AnnotationSuggestion(risk="medium", tags=_JOB_CONTROL),
    "get_policy_summary": AnnotationSuggestion(risk="low", tags=["policy", *READ]),
}

assert set(_TOOL_POLICIES) == {spec.name for spec in TOOL_SPECS}, "policy covers every tool"


def build_document(*, generated_at: str | None = None) -> ToolsMetadataDocument:
    """Render the static document; the etag covers the tools only.

    The tools are fixed, so the etag is stable across processes and a host can
    cache the document for the lifetime of the server.
    """
    tools = [
        ToolSuggestion(name=spec.name, annotation=_TOOL_POLICIES[spec.name]) for spec in TOOL_SPECS
    ]
    body = yaml.safe_dump([t.model_dump(mode="json") for t in tools], sort_keys=True)
    etag = hashlib.sha256(body.encode()).hexdigest()[:32]
    return ToolsMetadataDocument(
        version=DOCUMENT_VERSION,
        etag=etag,
        generated_at=generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        tools=tools,
    )


#: Built once per process: static content, and a stable ``etag`` for caching.
DOCUMENT: ToolsMetadataDocument = build_document()


def render_tools_metadata(document: ToolsMetadataDocument | None = None) -> str:
    """Serialize the document as YAML (the MIME type hosts parse)."""
    doc = document or DOCUMENT
    return yaml.safe_dump(doc.model_dump(mode="json"), sort_keys=False, allow_unicode=True)


def register_tools_metadata(server: Any) -> None:
    """Publish the document as a resource on a FastMCP server."""
    from mcp.server.fastmcp.resources.types import TextResource

    server.add_resource(
        TextResource(
            uri=AnyUrl(TOOLS_METADATA_URI),
            name=TOOLS_METADATA_NAME,
            title="Adaptive MCP Tools Metadata",
            description=(
                "Server-authored, advisory policy for QC's tools (risk, owner, "
                "tags, require_approval). Suggested, never enforced by QC; "
                "clients that do not understand it can ignore it."
            ),
            mime_type=TOOLS_METADATA_MIME,
            text=render_tools_metadata(),
        )
    )
