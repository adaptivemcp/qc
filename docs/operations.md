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

## Remote MCP identity (OIDC)

The v1 bearer (`StaticTokenVerifier`, ADR-003) is **authentication, not
identity**: it proves the caller holds a shared token, says nothing about
*who* they are, and every client presents the same secret. Beyond a single
trusted operator, put real identity in front of remote MCP. Two paths, one
designed and one deployable today.

### Designed path — QC as an OAuth resource server

Treat `qc mcp --transport http` as an OAuth 2.1 **resource server** for MCP
(the MCP authorization model): clients obtain an access token from an
identity provider and present it; QC validates it locally against the
provider's keys. No shared secret, and each token is bound to a user and an
audience.

Validation, per request:

1. **Signature** — verify the token (RS256/ES256) against the provider's
   JWKS, fetched from `/.well-known/jwks.json` and cached.
2. **Claims** — `iss` equals the configured issuer; `aud` equals QC's
   resource URL (audience binding: a token minted for another service must
   not open QC); `exp`/`nbf` are current.
3. **Scopes** — a required scope (e.g. `qc:mcp`) must be present; missing
   scope is `AuthError`, not a tool-level denial.

This is currently *stubbed*, not missing: `build_server_http`
(`src/qc/mcp/server.py`) already wires a `token_verifier` and
`AuthSettings`, but with `issuer_url` pointing at QC itself and
`validate_token_resource=False`, i.e. the token verifier is the only check.
The change is to select the verifier and fill `AuthSettings` from config:

```python
# src/qc/mcp/server.py — sketched delta, not yet implemented
if cfg.oidc is not None:
    verifier = JwtTokenVerifier(          # new: JWKS + iss/aud/exp/scope checks
        issuer=cfg.oidc.issuer_url,
        audience=cfg.oidc.audience,       # == QC's public resource URL
        required_scopes=cfg.oidc.required_scopes,
    )
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(cfg.oidc.issuer_url),
        resource_server_url=AnyHttpUrl(cfg.oidc.audience),
        validate_token_resource=True,
    )
else:
    verifier = StaticTokenVerifier(resolve_secret(cfg.auth_token_ref))
    auth = AuthSettings(issuer_url=AnyHttpUrl(f"http://{cfg.host}:{cfg.port}"))
```

Config (`qc.toml`), alongside the existing `[mcp.http]` keys:

```toml
[mcp.http]
enabled = true
host = "127.0.0.1"
port = 8787
# static mode (today): auth_token_ref = "env:QC_MCP_HTTP_TOKEN"
mode = "oidc"                       # "static" | "oidc"

[mcp.http.oidc]
issuer_url = "https://idp.example.com"
audience = "https://qc.example.com" # this server's public URL
required_scopes = ["qc:mcp"]
```

Adding a JWT verify dependency is the cost; it is isolated to the server
module. `StaticTokenVerifier` stays for local and single-operator setups —
the mode is explicit, so an unconfigured deployment keeps behaving exactly
as it does today.

### Deployable today — OIDC gateway in front

Until the resource-server path lands, terminate identity at a gateway and
keep QC on the static bearer:

```
MCP client ──(OIDC token)──▶ OIDC gateway ──(QC bearer)──▶ qc mcp (127.0.0.1:8787)
                                   │
                                   └─ discovery + JWKS ─▶ identity provider
```

The gateway (your existing OIDC reverse proxy: Envoy, oauth2-proxy, a
cloud API gateway, …) is the resource server: it validates the client's
token against the provider's JWKS and then presents QC's bearer on the
upstream hop. QC keeps binding `127.0.0.1`, so only the gateway reaches it,
and the per-client rate limit and source allowlist still apply at QC.
Known limitation: the QC hop still uses one shared credential, so the
gateway must be the only route in and must not forward arbitrary clients'
`Authorization` through verbatim. That limitation is what the resource-server
path removes.

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

**One rule, applied everywhere:** an artifact records the `schema_version` of the shape
it was written with, and a reader either understands that version or refuses. There is
no "best effort" path, because the alternative is dropping fields on the next write.

| Reader sees | Behaviour | Why |
| --- | --- | --- |
| same version | read | — |
| older version | migrate forward through a **registered** migration | guessing at a shape corrupts data silently |
| newer version | refuse: `… is newer than supported N; upgrade QC …` | a newer file may carry fields this build would drop |

Where it applies, and what a version means:

| Artifact | Version constant | Loader |
| --- | --- | --- |
| Profile (`.profile.json`) | `SCHEMA_VERSION_PROFILE` | `qc.artifacts.profiles.load_profile` |
| Generation plan (`plan.json`) | `SCHEMA_VERSION_PLAN` | `qc.application.generation.load_plan` |
| Generation / validation report | `SCHEMA_VERSION_REPORT` | `qc.artifacts.reports.load_report` (`qc report show`) |
| Policy document | `SCHEMA_VERSION_POLICY` | `qc.config.policies.parse_policy` |
| `state.db` | `STATE_SCHEMA_VERSION` | `qc.application.jobs.StateStore` (migrations in `_MIGRATIONS`) |

The rule is implemented once, in `qc.artifacts.versions.check_supported`, since the
failure mode is a missed call site: the profile loader had the check and the plan loader
did not, so a plan written by a newer QC was accepted without complaint. `state.db` had
the same shape of gap in the other direction — a *newer* database was opened with an
older table set, which is how a downgrade quietly corrupts job history; it is now
refused with the same instruction. Generation and validation reports were the last
artifacts written without a reader that checked them; `qc report show <path>` reads them
through `load_report` and refuses a newer schema the same way.

**What a release guarantees** (SPEC 14):

- **Patch** — no artifact-schema change. Old and new artifacts interchange freely.
- **Minor** — artifact schemas may gain a version. Readers of the older version keep
  working; readers of the newer one must migrate forward. A migration is registered in
  the owning module and covered by a test.
- **Major** — migrations may be removed. An artifact older than the oldest registered
  migration is refused with the same actionable message rather than half-read.
- **Downgrade** is refused, not attempted. The error names the version and the remedy.

`PROFILE_ALGORITHM_VERSION` is tracked separately from `schema_version`: algorithms may
change what is *reported* without changing the artifact's shape, and both are recorded so
a profile is reproducible across tool versions.

Operational practice: `uv sync --locked` in CI; upgrade by bumping pins, running the full
suite, then reviewing artifact diffs (`qc profile diff`) before rolling forward.

### How `state.db` upgrades

The state directory is upgraded **in place**, on open, before any query:

- a fresh file is created at `STATE_SCHEMA_VERSION`, with the current tables and indexes;
- an older file runs the entries in `_MIGRATIONS` whose target version is above its own, in
  ascending order, updating `schema_version` after each;
- every entry is written to be idempotent (`CREATE INDEX IF NOT EXISTS`), so a process that
  dies between the script and the version update is recoverable — replaying it is harmless;
- a **newer** file is refused outright (`upgrade QC to use this state directory`), because
  opening it with an older table set is how a downgrade corrupts job history.

The current migration (v2) adds `idx_jobs_created`, the index behind `qc job list` on a
long-lived state directory. `tests/unit/test_state_migrations.py` builds a v1 database by
hand, inserts a job and a profile row, opens it with the current build, and asserts the rows
survive, the version moved, the index exists exactly once after three reopenings, and a
newer file is still refused. It also exercises the registry itself with injected
migrations, so the mechanism is covered independently of which migration is current.

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

## Measured baselines

`tools/bench/profile_bench.py` profiles a generated SQLite table through the real
`Profiler` and policy engine, offline. It answers the question capacity planning
actually needs: **what does a profile cost, and what does it scale with?**

```bash
uv run python tools/bench/profile_bench.py            # rows 10k / 100k / 1M
uv run python tools/bench/profile_bench.py --json
uv run python tools/bench/profile_bench.py --sizes 100000 --sample-size 5000
```

Recorded 2026-11-06 on Linux 7.2.11 (20 logical cores), Python 3.14, SQLite fixtures,
median of 3 runs, `sample_size=1000`:

| rows      | sample | queries | sampled rows | seconds | peak MB |
| --------: | -----: | ------: | -----------: | ------: | ------: |
|    10,000 |  1,000 |      30 |        1,042 |    0.94 |     1.0 |
|   100,000 |  1,000 |      30 |        1,042 |    1.44 |     0.8 |
| 1,000,000 |  1,000 |      30 |        1,042 |    6.89 |     0.8 |

Sample size at a fixed 100,000 rows:

| sample | queries | sampled rows | seconds | peak MB |
| -----: | ------: | -----------: | ------: | ------: |
|    100 |      30 |          142 |    0.68 |     0.4 |
|  1,000 |      30 |        1,042 |    1.44 |     0.8 |
|  5,000 |      30 |        5,042 |    4.84 |     4.2 |

What the numbers say, and what they do not:

- **Query count is flat (30) across three orders of magnitude.** The adaptive planner
  decides from metadata plus one sample, so a bigger table does not buy more queries —
  which is the budget that binds first on a remote source.
- **Time tracks two things:** the aggregate probes, which scan the table (1M rows ≈ 6.9s
  against 1.4s at 100k), and the sample size, which dominates once the sample grows
  (5,000 rows ≈ 4.8s at a fixed 100k table). Neither is quadratic.
- **Peak memory stays under 5 MB** for every configuration here, consistent with
  "budgets bound memory": the sample is materialized, the table is not.
- These are single-host SQLite numbers with a warm page cache. A networked source adds
  per-query latency, so multiply by query count — which is why keeping it flat is the
  property that matters.
- Re-record this table with the machine and date when the profiler changes; it is a
  regression baseline, not a promise about other hardware.

### Sanitization and generation throughput

`tools/bench/throughput_bench.py` measures the other half of M7: after a dataset is
profiled, how fast do rows move through per-column sanitization, and through synthesis +
validation + write? Both stages run the real application paths (`preview_rows`,
`Profiler`, `create_plan`, `run_generation`) against a real project root, a real policy
file and a real SQLite source — a throughput number from a mocked writer would be a number
about the mock.

```bash
uv run python tools/bench/throughput_bench.py
uv run python tools/bench/throughput_bench.py --sizes 5000,20000 --repeat 5
uv run python tools/bench/throughput_bench.py --json
```

Recorded 2026-11-06, same host and method as the table above (median of 3 runs):

| stage | rows | seconds | rows/sec | peak MB |
| --- | ---: | ---: | ---: | ---: |
| sanitize | 5,000 | 0.15 | 33,952 | 4.7 |
| generate | 5,000 | 3.32 | 1,506 | 8.0 |
| sanitize | 20,000 | 0.55 | 36,326 | 19.2 |
| generate | 20,000 | 13.06 | 1,532 | 10.7 |

What the numbers say:

- **Sanitization is not the cost.** ~34–36k rows/sec, flat across 4× the rows, and the
  work is per-row transforming after a bounded read; peak memory grows with the page (a
  `preview` returns the rows it read), which is why `--limit` and the inline caps exist.
- **Generation is ~20× slower and flat per row** (~1.5k rows/sec): each row is synthesized
  from a learned strategy, checked against uniqueness/format constraints, validated, and
  written. That is the throughput to quote when sizing a synthetic run — a million rows is
  roughly eleven minutes of this stage on this host, before any destination write.
- Both are single-process and Python-bound, so they scale by running more jobs, not by a
  bigger machine.
- The fixture deliberately has duplicate ids: `unique = distinct == row_count` in the
  profiler, so a sequential-key table would make generation fail its uniqueness constraint
  rather than measure throughput. A run against primary-key data is a correctness test,
  not a speed test.
- Re-record with the host and date when either path changes; this is a regression
  baseline, not a promise about other hardware.

