"""Error envelopes must surface as protocol-level MCP errors (SPEC 12.4).

Regression guard for the interop gap: a policy denial / budget cap / validation
failure is a *tool-execution error*, so the result must carry ``isError: true``
(not a successful result with an in-band ``status: "error"``).
"""

from __future__ import annotations

from typing import Any

from qc.mcp.server import _as_mcp_result


def test_error_envelope_is_an_mcp_error() -> None:
    envelope: dict[str, Any] = {
        "status": "error",
        "job_id": None,
        "error": "POLICY_DENIED",
        "message": "source not permitted",
        "detail": {},
        "data": {},
        "warnings": [],
        "provenance": {"tool_version": "0.1.0"},
        "truncated": False,
    }

    result = _as_mcp_result(envelope)

    assert result.isError is True
    assert result.structuredContent == envelope
    assert result.content and result.content[0].type == "text"


def test_success_envelope_passes_through_unchanged() -> None:
    envelope: dict[str, Any] = {"status": "completed", "data": {"rows": 1}}
    assert _as_mcp_result(envelope) is envelope
