# Operations

Deployment, logging, upgrades, backup, and retention for QC (SPEC 15).

## Deployment modes

| Mode | Command | Network | Auth |
|---|---|---|---|
| Local CLI | `qc …` | none | OS user |
| Local MCP (stdio) | `qc mcp` | none | client process |
| Remote MCP (HTTP) | `qc mcp --transport http` | TCP (bind `127.0.0.1` behind proxy) | bearer token ≥16 chars, TLS at proxy |

Container image for remote MCP is a release deliverable (ADR/§19.10);
until then run the wheel in your own image with a read-only filesystem
except `state_dir`.

## Configuration

`qc.toml` (validated at startup; unknown schema versions fail):

```toml
version = 1
default_policy = "strict-local"
state_dir = "~/.local/share/qc"
profile_dir = "~/.local/share/qc/profiles"

[budgets]
max_queries = 100
max_rows = 100000
max_bytes = 67108864
max_runtime_seconds = 300
max_inline_mcp_rows = 20
max_inline_mcp_bytes = 65536

[security]
raw_sample_persistence = false
raw_mcp_rows = false
external_model_calls = false
source_writes = false
redact_logs = true
```

Secrets are **references** (`env:`/`file:`/`keyring:`/`cmd:`), never
inline. `file:` secrets must be chmod 600.

## Logging and metrics

- Structured JSON lines on **stderr**; redaction applied to every field
  (`password=`, `Bearer`, JWTs, URLs with credentials, AWS keys, emails).
- Fields: `ts`, `level`, `logger`, `message`, `operation_id`,
  `source_id`, `dataset_id`, `job_id`, `correlation_id`, `error_code`.
- **Never logged:** row values, credentials, access tokens, raw query
  parameters, unredacted SQL, connection strings.
- Audit trail: append-only `audit.jsonl` (mode 0600) with actor,
  operation, decision, counts, duration, correlation id.
- **Metrics** (`qc.observability.metrics`): each profile/generation run
  records queries, rows scanned/returned, bytes processed, retries,
  cancellations, budget rejections, generated rows, validation failures,
  connector health, and elapsed time; a `qc.metrics` structured log line
  is emitted and the values appear in `--json` output.
- `--verbose` → debug level; `--quiet` → warnings and errors only.

## Reliability

- **Retries:** transient, idempotent read failures (connection resets,
  deadlocks, serialization failures, HTTP 429/5xx) are retried with
  bounded exponential backoff + jitter, respecting the request deadline
  and cancellation. Non-transient errors (validation, policy) never
  retry. Retry counts feed metrics (SPEC 6.1).
- **Connection pooling:** SQL connectors use a bounded pool
  (`AsyncConnectionPool`); a connection that errors while checked out is
  discarded rather than reused. Pool bounds are configurable per source.
- **Cancellation:** CLI `Ctrl-C` and MCP `cancel_job` cooperatively stop
  in-flight work and close cursors/connections (SPEC 4.3, 12.6).


## Diagnostics

```bash
qc doctor          # python, config, policy, state, connectors, secret resolvers
qc version --json  # tool + dependency versions
```

`doctor` output contains no secrets and is safe to attach to bug reports.

## State, backup, retention

Layout (all directories created 0700, files 0600):

```
project/
  qc.toml
  sources.json          # source configs (secret references only)
  policies/*.yaml
  profiles/*.profile.json
  reports/*.plan.json, *.report.json
  output/*.jsonl|csv + *.manifest.json
  state.db              # SQLite: jobs + profile index (no raw rows)
  audit.jsonl
```

- **Backup:** copy the project directory. Profiles/policies are plain
  JSON/YAML; `state.db` may be rebuilt (jobs lost, profiles reindexed by
  re-running with the same ids).
- **Retention:** sanitized aggregate profiles are retained indefinitely
  by default; retention is configurable and deletion is a file
  operation — `rm profiles/*.profile.json` plus optional `state.db`
  vacuum. Raw samples are never stored, so there is nothing else to purge.
- Temporary files are written with `O_EXCL` semantics next to their
  destination and replaced atomically; they are removed on success and
  on failure paths.

## Upgrades

- `schema_version` gates every artifact; unknown **future** versions fail
  with an actionable message (upgrade the tool, don't guess).
- Older versions migrate forward on load (explicit migrations only).
- Algorithm version ≠ artifact schema version; both are recorded in
  profiles and reports for reproducibility.
- Lockfile-pinned dependencies; `uv sync --locked` in CI. Upgrade by
  bumping pins, running the full suite, then reviewing artifact diffs.

## Health checks (remote mode)

Readiness = process up + config validated + policy loaded; expose a
proxy-level `/healthz` that returns no source metadata. Liveness must
not probe sources (avoid side effects). Rate limits and request-size
caps are enforced in-process (`rate_limit_per_minute`,
`max_request_body_size`).

## Cancellation and shutdown

- Budget/runtime deadlines abort profiling with exit code 5; partial
  profiles are marked with `incomplete_checks`.
- MCP `cancel_job` marks the job and closes connector resources; the
  sidecar receives `job.cancel` → `Statement.cancel()`.
- SIGINT/SIGTERM: graceful close of connectors, sidecar, HTTP server;
  temp files cleaned up.

## Capacity guidance

- One profiling operation per process is the design center; budgets
  bound memory (sample sizes, inline caps).
- SQLite WAL handles the single-writer local model; do not point multiple
  QC processes at one `state.db`.
- Sheet reads cap at 1M cells per query (truncated flag set).
