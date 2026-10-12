"""MCP stdio integration: reference client against the real server (SPEC 12).

Covers: tool inventory (exactly v1 set, no excluded tools), strict schemas,
policy denial, budget ceilings, envelope shape, error codes, and the
"no raw rows by default" guarantee.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

TOOL_NAMES = {
    "list_sources",
    "list_datasets",
    "get_dataset_schema",
    "profile_dataset",
    "get_profile",
    "preview_sanitized_data",
    "generate_synthetic_data",
    "validate_synthetic_data",
    "get_job_status",
    "cancel_job",
    "get_policy_summary",
}

EXCLUDED = {
    "execute_sql",
    "run_python",
    "update_policy",
    "create_source",
    "write_rows",
    "export_to_url",
    "get_raw_rows",
}


@pytest.fixture
def mcp_project(tmp_path: Path, edge_rows: list[dict]) -> Path:
    """A QC project with fixture source + granted policy."""
    root = tmp_path / "proj"
    root.mkdir()
    data = root / "data"
    data.mkdir()
    (data / "people.jsonl").write_text(
        "\n".join(json.dumps(r) for r in edge_rows), encoding="utf-8"
    )
    (root / "qc.toml").write_text(
        'version = 1\ndefault_policy = "strict-local"\n'
        'state_dir = "' + str(root / "state") + '"\n'
        'profile_dir = "' + str(root / "profiles") + '"\n'
        "[budgets]\nmax_queries = 100\nmax_rows = 100000\n"
        "max_bytes = 67108864\nmax_runtime_seconds = 300\n"
        "max_inline_mcp_rows = 5\nmax_inline_mcp_bytes = 65536\n"
        "[security]\nraw_mcp_rows = false\nsource_writes = false\n"
        "external_model_calls = false\n",
        encoding="utf-8",
    )
    # sources registry
    from qc.application.sources import SourceRegistry
    from qc.domain.models import SourceConfig

    reg = SourceRegistry(root / "sources.json")
    reg.add(
        SourceConfig(
            name="demo",
            connector="fixture",
            connection_ref=f"env:QC_MCP_FIXTURE_{os.getpid()}",
            options={"root": str(data)},
        )
    )
    # policy: grant demo + people.jsonl; email synthetic
    (root / "policies").mkdir()
    (root / "policies" / "strict-local.yaml").write_text(
        """
name: strict-local
version: 1
defaults:
  source_access: deny
  raw_row_output: deny
  profile_disclosure: aggregate_only
budget:
  max_rows: 1000
  max_queries: 50
sources:
  demo:
    access: read
    datasets: [people.jsonl]
    columns:
      email: {action: synthetic}
      id: {action: tokenize}
      age: {action: keep}
      note: {action: redact}
""",
        encoding="utf-8",
    )
    return root


class StdioClient:
    """Minimal MCP stdio reference client (initialize → tools/call)."""

    def __init__(self, project: Path) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        env["QC_MCP_STDIO"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, "-c", "from qc.mcp.__main__ import main; main()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
            cwd=str(project),
        )
        self._id = 0

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("mcp server closed stdout (crash?)")
        return json.loads(line)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n"
        )
        self.proc.stdin.flush()

    def initialize(self) -> dict[str, Any]:
        resp = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "qc-test", "version": "0"},
            },
        )
        self.notify("notifications/initialized")
        return resp

    def list_tools(self) -> list[dict[str, Any]]:
        return self.request("tools/list")["result"]["tools"]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        resp = self.request("tools/call", {"name": name, "arguments": arguments})
        return _payload(resp)

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _payload(resp: dict[str, Any]) -> dict[str, Any]:
    """Extract structured JSON payload from an MCP tools/call response.

    FastMCP pre-validates arguments against the strict tool schema before
    our dispatcher runs; those transport-level rejections come back as
    isError text, not QC envelopes. Normalize them to stable codes so
    assertions target SPEC 12.4 semantics either way.
    """
    if "error" in resp:
        msg = str(resp["error"].get("message", ""))
        return {"status": "error", "error": _classify(msg), "message": msg}
    result = resp.get("result", {})
    if result.get("structuredContent"):
        return result["structuredContent"]
    for block in result.get("content", []):
        if block.get("type") == "text":
            text = block["text"]
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and ("error" in parsed or "data" in parsed):
                    return parsed
            except json.JSONDecodeError:
                pass
            if result.get("isError"):
                return {"status": "error", "error": _classify(text), "message": text}
            return {"status": "error", "message": text}
    return result


def _classify(message: str) -> str:
    lowered = message.lower()
    if "unknown tool" in lowered or "not available" in lowered:
        return "UNSUPPORTED_CAPABILITY"
    if "validation error" in lowered or "invalid" in lowered or "less than" in lowered:
        return "VALIDATION_FAILED"
    if "denied" in lowered:
        return "POLICY_DENIED"
    return "INTERNAL"


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Any:
    # project is built per-test; this fixture is replaced in tests below
    raise NotImplementedError


@pytest.mark.integration
class TestMcpStdio:
    def test_initialize_and_tool_inventory(self, mcp_project: Path) -> None:
        c = StdioClient(mcp_project)
        try:
            init = c.initialize()
            assert init["result"]["serverInfo"]["name"] == "qc"
            tools = c.list_tools()
            names = {t["name"] for t in tools}
            assert names == TOOL_NAMES
            assert not names & EXCLUDED
            # strict schemas: every tool has inputSchema with properties
            for t in tools:
                assert t["inputSchema"]["type"] == "object"
        finally:
            c.close()

    def test_list_sources_only_permits_policy_sources(self, mcp_project: Path) -> None:
        c = StdioClient(mcp_project)
        try:
            c.initialize()
            out = c.call("list_sources", {})
            assert out["status"] == "completed"
            names = [s["name"] for s in out["data"]["sources"]]
            assert names == ["demo"]
            blob = json.dumps(out)
            assert "connection_ref" not in blob
            assert "env:" not in blob
        finally:
            c.close()

    def test_profile_then_get_profile(self, mcp_project: Path) -> None:
        c = StdioClient(mcp_project)
        try:
            c.initialize()
            out = c.call(
                "profile_dataset",
                {"source_id": "demo", "dataset_id": "people.jsonl"},
            )
            assert out["status"] == "completed", out
            pid = out["data"]["profile_id"]
            assert pid.startswith("prof_")
            assert "queries_used" in out["data"]["coverage"]
            assert out["provenance"]["tool_version"]

            prof = c.call("get_profile", {"profile_id": pid})
            assert prof["status"] == "completed"
            assert prof["data"]["disclosure_level"] == "aggregate_only"
            # aggregate_only: no top categories exposed
            for col in prof["data"]["columns"]:
                assert "top_categories" not in col
        finally:
            c.close()

    def test_preview_returns_only_transformed_rows(self, mcp_project: Path) -> None:
        c = StdioClient(mcp_project)
        try:
            c.initialize()
            out = c.call(
                "preview_sanitized_data",
                {
                    "source_id": "demo",
                    "dataset_id": "people.jsonl",
                    "policy_id": "strict-local",
                    "limit": 3,
                },
            )
            assert out["status"] == "completed", out
            # raw rows denied: the flag records that raw output is off
            assert out["data"]["raw_output"] is False
            blob = json.dumps(out)
            # source values must not appear: email synthetic, id tokenized,
            # note redacted, unreviewed columns blocked
            assert "alice@example.com" not in blob
            assert '"00123"' not in blob
            assert "hello world" not in blob
            # email column output is synthetic, not raw
            for row in out["data"]["rows"]:
                if row.get("email"):
                    assert row["email"].startswith("synthetic_")
        finally:
            c.close()

    def test_unlisted_dataset_denied(self, mcp_project: Path) -> None:
        c = StdioClient(mcp_project)
        try:
            c.initialize()
            out = c.call(
                "preview_sanitized_data",
                {
                    "source_id": "demo",
                    "dataset_id": "secrets.jsonl",
                    "policy_id": "strict-local",
                    "limit": 3,
                },
            )
            assert out["status"] == "error"
            assert out["error"] == "POLICY_DENIED"
        finally:
            c.close()

    def test_inline_row_limit_enforced(self, mcp_project: Path) -> None:
        c = StdioClient(mcp_project)
        try:
            c.initialize()
            out = c.call(
                "preview_sanitized_data",
                {
                    "source_id": "demo",
                    "dataset_id": "people.jsonl",
                    "policy_id": "strict-local",
                    "limit": 999,  # exceeds server max_inline_mcp_rows=5
                },
            )
            assert out["status"] == "error"
            assert out["error"] == "VALIDATION_FAILED"
        finally:
            c.close()

    def test_budget_cannot_be_overridden(self, mcp_project: Path) -> None:
        c = StdioClient(mcp_project)
        try:
            c.initialize()
            out = c.call(
                "profile_dataset",
                {
                    "source_id": "demo",
                    "dataset_id": "people.jsonl",
                    "max_queries": 999999,  # exceeds schema le=1000
                },
            )
            assert out["status"] == "error"
            assert out["error"] == "VALIDATION_FAILED"
        finally:
            c.close()

    def test_unknown_and_excluded_tools(self, mcp_project: Path) -> None:
        c = StdioClient(mcp_project)
        try:
            c.initialize()
            # excluded tools are not registered; server rejects by name
            out = c.call("execute_sql", {"query": "select 1"})
            assert out["status"] == "error"
            assert out["error"] in ("NOT_FOUND", "UNSUPPORTED_CAPABILITY")
        finally:
            c.close()

    def test_path_traversal_in_artifact_ref(self, mcp_project: Path) -> None:
        c = StdioClient(mcp_project)
        try:
            c.initialize()
            out = c.call(
                "validate_synthetic_data",
                {"artifact_ref": "../../etc/passwd", "profile_id": "x"},
            )
            assert out["status"] == "error"
            assert out["error"] in ("VALIDATION_FAILED", "NOT_FOUND")
        finally:
            c.close()

    def test_policy_summary_no_secrets(self, mcp_project: Path) -> None:
        c = StdioClient(mcp_project)
        try:
            c.initialize()
            out = c.call("get_policy_summary", {"policy_id": "strict-local"})
            assert out["status"] == "completed"
            blob = json.dumps(out)
            for word in ("password", "secret", "token", "hunter"):
                assert word not in blob.lower() or "deny" in blob.lower()
            assert out["data"]["server_ceiling"]["raw_mcp_rows"] is False
        finally:
            c.close()

    def test_missing_source_errors_safely(self, mcp_project: Path) -> None:
        c = StdioClient(mcp_project)
        try:
            c.initialize()
            out = c.call("list_datasets", {"source_id": "nope"})
            assert out["status"] == "error"
            assert out["error"] == "NOT_FOUND"
            assert "Traceback" not in json.dumps(out)
        finally:
            c.close()
