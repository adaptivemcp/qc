# MCP server

QC exposes a narrow, policy-aware tool surface to AI clients (SPEC
section 12). Both transports share the same application services, policy
checks, budgets, and error envelope.

## Transports

**stdio (default)** — for local AI clients; no listening socket:

```bash
qc mcp                       # or: python -m qc.mcp
# client config example:
# { "mcpServers": { "qc": { "command": "qc", "args": ["mcp"] } } }
```

**Streamable HTTP (optional)** — remote/shared deployments. Refuses to
start unless *all* of the following hold:

```toml
[mcp.http]
enabled = true
host = "127.0.0.1"
port = 8765
require_auth = true
auth_token_ref = "env:QC_MCP_HTTP_TOKEN"   # ≥16 chars
rate_limit_per_minute = 120
max_request_bytes = 1048576
```

Unknown keys are rejected, not ignored: a typo such as
`max_request_body_size` fails startup with `invalid configuration` rather
than leaving you believing a limit is in force.

```bash
export QC_MCP_HTTP_TOKEN='…'
qc mcp --transport http
```

For internet exposure, terminate TLS at a reverse proxy and prefer an
OIDC-capable gateway for identity (ADR-003); the built-in bearer token
is a v1 stopgap with constant-time comparison and per-minute rate
limits.

## Tools (v1)

| Tool | Input | Returns |
|---|---|---|
| `list_sources` | — | policy-visible sources (no credentials) |
| `list_datasets` | `source_id`, namespace?, search?, page_size ≤200, cursor | permitted dataset ids |
| `get_dataset_schema` | `source_id`, `dataset_id` | columns/constraints allowed by policy |
| `profile_dataset` | `source_id`, `dataset_id`, `sample_size` ≤10000, `sensitivity_scan`, `max_queries` ≤1000 | profile summary + `profile_id`, coverage, warnings |
| `get_profile` | `profile_id` | policy-filtered profile summary |
| `preview_sanitized_data` | source/dataset, `policy_id`, `limit` ≤ server `max_inline_mcp_rows`, `columns?` | transformed rows only |
| `generate_synthetic_data` | `profile_id`, `rows`, `seed?`, `output_format` (`summary`\|`inline`), `inline_limit` | summary + artifact refs; inline rows capped |
| `validate_synthetic_data` | `artifact_ref`, `profile_id` | metrics/violations, no source values |
| `get_job_status` | `job_id` | status/progress/cancellable |
| `cancel_job` | `job_id` | signals a running job (`cancelling`) or reports it already finished |
| `get_policy_summary` | `policy_id` | effective permissions + server ceiling |

Every long-running tool (`profile_dataset`, `generate_synthetic_data`)
records a job and returns its `job_id` in the envelope, so clients can
poll `get_job_status` and call `cancel_job`. Cancellation is cooperative:
the job stops between probes/rows, closes its cursor/connection, and the
job record ends `cancelled` (SPEC 12.6).

Every tool also carries standard MCP **annotations**: `destructiveHint: false`
and `openWorldHint: false` (QC never mutates sources and access is
policy-bounded), and a `readOnlyHint` — `true` for the pure reads, `false` for
`profile_dataset` / `generate_synthetic_data` (they persist an artifact) and
`cancel_job`. They are set explicitly because MCP defaults `destructiveHint` to
`true` when omitted, which would make every tool look destructive to a host.

### Explicitly excluded (SPEC 12.3)

Arbitrary SQL, arbitrary Python, policy mutation, source/credential
management, source writes, exports to public URLs, and unrestricted raw
rows. Requesting them returns `UNSUPPORTED_CAPABILITY`.

## Output envelope

```json
{
  "status": "completed",
  "job_id": null,
  "data": {},
  "warnings": [],
  "provenance": {"profile_id": null, "policy_version": "1", "tool_version": "0.1.0"},
  "truncated": false
}
```

Errors use stable codes: `POLICY_DENIED`, `BUDGET_EXCEEDED`,
`SOURCE_UNAVAILABLE`, `UNSUPPORTED_CAPABILITY`, `VALIDATION_FAILED`,
`NOT_FOUND`, `AUTH_FAILED`, `CANCELLED`, `INTERNAL`. Error text never
contains secrets, raw values, internal network details, or unapproved
schema (validation errors report field *paths* only).

An error envelope is returned as a **protocol-level MCP error**: the tool
result carries `isError: true` with the same envelope as its
`structuredContent` (and a JSON text rendering). Standard MCP clients
therefore treat a policy denial / budget cap / validation failure as a
failed tool call, while non-MCP consumers and the structured payload keep
the stable `error` code and message.

## Enforcement model

1. **Schema** — inputs validated against strict Pydantic-derived JSON
   Schema (bounded lengths, ranges, patterns).
2. **Authorization** — policy evaluated on every call, before any source
   read; unlisted sources/datasets denied; unlisted columns redacted.
3. **Budgets** — tool args can only *narrow* server ceilings; exceeding
   schema bounds returns `VALIDATION_FAILED`.
4. **Row/byte caps** — inline responses bounded by `max_inline_mcp_rows`
   (default 20) and `max_inline_mcp_bytes`.
5. **Audit** — every operation recorded with correlation id, decision,
   counts, duration; no raw rows or credentials.
6. **Cancellation** — `cancel_job` marks the job and closes connector
   resources where supported.

## Resources and prompts

Read-only resources are planned as `qc://profiles/{id}`,
`qc://jobs/{id}`, `qc://policies/{id}/summary`, enforcing the same
authorization as tools (not enabled in v1). Prompt templates are
convenience only; server-side enforcement is authoritative.

## Client setup examples

```bash
# Claude Desktop / generic MCP client over stdio
{"mcpServers": {"qc": {"command": "qc", "args": ["mcp"], "cwd": "/path/to/project"}}}

# Remote HTTP client
curl -H "Authorization: Bearer $QC_MCP_HTTP_TOKEN" \
  http://127.0.0.1:8765/mcp
```

## Security notes (SPEC 12.6)

- Source text is untrusted content, never instructions; data content
  cannot trigger follow-up tool execution or network calls.
- Argument values cannot modify allowlists or policy enforcement.
- Correlation IDs appear in safe audit events, not in error text.
- Raw SQL is never returned unless explicitly authorized (redacted).
