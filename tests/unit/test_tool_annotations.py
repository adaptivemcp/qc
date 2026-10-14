"""Every MCP tool carries explicit, non-destructive annotations (SPEC 12.2).

Hosts seed approval risk from the standard hints (``destructiveHint`` /
``readOnlyHint`` / ``openWorldHint``). Because MCP defaults ``destructiveHint``
to *true* when omitted, QC must set these explicitly — otherwise a client may
assume every tool is destructive, or (with none) has no risk signal at all.
"""

from __future__ import annotations

from qc.mcp.schemas import TOOL_ANNOTATIONS, TOOL_SPECS

READ_ONLY_TOOLS = {
    "list_sources",
    "list_datasets",
    "get_dataset_schema",
    "get_profile",
    "preview_sanitized_data",
    "validate_synthetic_data",
    "get_job_status",
    "get_policy_summary",
}


def test_annotations_cover_every_tool() -> None:
    assert {spec.name for spec in TOOL_SPECS} == set(TOOL_ANNOTATIONS)


def test_every_tool_is_explicitly_non_destructive_and_local() -> None:
    for spec in TOOL_SPECS:
        annotations = spec.annotations
        assert annotations is not None, spec.name
        assert annotations.destructiveHint is False, spec.name
        assert annotations.openWorldHint is False, spec.name
        assert annotations.readOnlyHint is not None, spec.name


def test_read_only_tools_are_flagged() -> None:
    for spec in TOOL_SPECS:
        expected = spec.name in READ_ONLY_TOOLS
        assert spec.annotations is not None
        assert spec.annotations.readOnlyHint is expected, spec.name
