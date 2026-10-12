# QueryCleaner/QC

**Working name — rename before public release** (ADR-001).

Local-first data profiling, sanitization, and synthetic data generation
with a consistent **CLI (`qc`)** and **MCP server** interface. QC learns
data behavior through bounded, inspectable evidence, enforces privacy
policy *before* disclosure, generates data with explicit constraints,
validates the result, and makes every important decision traceable.

> Status: implementation against [`SPEC.md`](SPEC.md). See
> [SECURITY.md](SECURITY.md) for the threat model and data-handling
> guarantees.

## What it does

- **Connect** to PostgreSQL, MySQL, Alibaba MaxCompute (via JVM JDBC
  sidecar), Google Sheets, and Lark Sheets — read-only by default.
- **Discover** schemas, constraints, keys, and spreadsheet structure.
- **Profile** adaptively under row/byte/query/time budgets: logical types
  (ambiguity retained), null/falsy variants kept distinct, distributions,
  formats, relationships, sensitivity detection, evidence + confidence.
- **Sanitize** with per-column actions: `keep`, `mask`, `redact`,
  `generalize`, `tokenize` (keyed HMAC), `synthetic`, `drop`,
  `manual_review` — deny-by-default.
- **Generate** synthetic data with seeded reproducibility and validated
  fidelity (hard constraints vs soft tolerances reported separately).
- **Export** JSONL/CSV (null encoding + formula-injection protection) or
  an approved destination database, with provenance manifests.
- **Serve** AI clients over MCP (stdio or authenticated Streamable HTTP)
  with 11 narrow, bounded, policy-enforced tools.

## Quickstart

```bash
uv sync
source .venv/bin/activate   # bash/zsh; fish: source .venv/bin/activate.fish
```

`uv sync` installs the `qc` entry point into `.venv/bin/` but does **not**
activate it, so `.venv/bin` is not on your `PATH` until you run the
second line. That is the only setup step the examples below assume.

No-activation alternative: `uv run qc …` resolves the entry point from
this project (it works from the repository directory; `uv run --project
/path/to/qc` works from anywhere) — the Development section uses it
throughout.

```bash
qc init                      # qc.toml, strict policy, directories
qc doctor                    # environment + connector diagnostics

qc source add analytics --type postgres \
  --connection-ref env:PG_DSN --schema public
qc source test analytics
qc policy grant strict-local analytics --dataset public.orders   # explicit grant
qc dataset list analytics

qc profile run analytics public.orders --max-queries 40 --sample-size 2000
qc profile run analytics public.orders --where region=US --where age:>=:18
qc profile show <profile-id>

qc sanitize preview analytics public.orders --policy strict-local --limit 10
qc sanitize export  analytics public.orders --policy strict-local \
  --output output/safe.jsonl
qc sanitize export  analytics public.orders --policy strict-local \
  --output output/safe.csv --format csv --delimiter ';' --no-header

qc generate plan <profile-id> --rows 1000 --seed 42
qc generate run reports/plan_*.plan.json --output output/synth.jsonl
qc generate validate output/synth.jsonl --profile <profile-id>
```

Fixture (JSONL/CSV) sources let you try everything without a database:

```bash
export QC_FIXTURE_ROOT=$PWD/data
qc source add demo --type fixture \
  --connection-ref env:QC_FIXTURE_ROOT --dataset people.jsonl
```

## MCP

```bash
qc mcp                                   # stdio (local AI clients)
qc mcp --transport http                  # requires auth config (see docs/mcp.md)
```

Tools: `list_sources`, `list_datasets`, `get_dataset_schema`,
`profile_dataset`, `get_profile`, `preview_sanitized_data`,
`generate_synthetic_data`, `validate_synthetic_data`, `get_job_status`,
`cancel_job`, `get_policy_summary`.

**Not exposed in v1:** arbitrary SQL/Python, policy mutation, source
writes, credential management, raw-row dumps.

## Security defaults

- Read-only source access; no source mutation.
- **Deny by default:** raw rows, external egress, writes, model calls,
  and exports are denied until a policy explicitly allows them.
- No credentials in config or artifacts — secret *references* only
  (`env:`, `file:` chmod-600, `keyring:`, `cmd:`).
- No raw values in logs, errors, reports, or profile artifacts.
- Budgets are ceilings: tool arguments can only narrow them.
- Full model: [SECURITY.md](SECURITY.md).

## Documentation

| Doc | Contents |
|---|---|
| [SPEC.md](SPEC.md) | full specification |
| [SECURITY.md](SECURITY.md) | threat model, data handling, reporting |
| [docs/connectors.md](docs/connectors.md) | permissions + config per source |
| [docs/profiling.md](docs/profiling.md) | algorithms, sampling limits, confidence |
| [docs/policies.md](docs/policies.md) | policy schema, semantics, actions |
| [docs/synthetic-data.md](docs/synthetic-data.md) | strategies, fidelity, privacy limits |
| [docs/mcp.md](docs/mcp.md) | transports, tools, auth, client setup |
| [docs/operations.md](docs/operations.md) | deploy, log, upgrade, retain |
| [docs/gaps.md](docs/gaps.md) | development gap assessment and resolutions |
| [docs/adr/](docs/adr/) | architecture decision records |

## Development

```bash
uv sync
uv run ruff check src tests && uv run ruff format src tests
uv run mypy
uv run pytest tests/unit tests/property tests/security -q      # fast
uv run pytest tests -q -m "not integration"                    # full non-live
# live connector contract tests (docker):
export QC_TEST_POSTGRES_DSN='postgresql://…'
export QC_TEST_MYSQL_DSN='mysql://…'
uv run pytest tests/contract -m integration
```

Exit codes are stable (SPEC 11.3): `0` ok, `2` bad arguments, `4` policy
denied, `5` budget/cancelled, `6` source error, `7` validation failure,
`9` destination write failure.

## Known limitations (v1)

- Synthetic data is **not anonymous** and not differentially private
  (ADR-005); reports say so explicitly.
- Relational generation across multiple datasets is declarative, not yet
  scheduled (see docs/synthetic-data.md).
- MaxCompute requires a pinned JDBC driver + Java 17+; live driver/dialect
  verification is a release gate (ADR-006).
- HTTP bearer auth is a stopgap in front of an OIDC gateway (ADR-003).

## License

Apache-2.0 (pending dependency and organizational review — ADR-001).
