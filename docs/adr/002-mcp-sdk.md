# ADR-002: MCP SDK and protocol version

**Status:** accepted (default) · **Date:** 2026-10-12

## Context

SPEC §4.4 requires the official MCP Python SDK, pinned; §19.2 requires
pinning a tested stable SDK.

## Decision

- Use the official **`mcp` Python SDK** (`mcp>=1.2,<2`), pinned in
  `uv.lock`, against the stable protocol revision it ships.
- All tool schemas are declared with Pydantic models in `qc/mcp/` and
  validated strictly; the server enforces budgets and policy before any
  connector call.
- Contract tests in `tests/e2e/` run a reference stdio client against the
  server to freeze tool behavior; SDK upgrades must pass them.

## Consequences

- Upgrading the SDK is a deliberate, tested change.
- Protocol features beyond tools/resources/prompts are not used.
