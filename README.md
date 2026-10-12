# QueryCleaner/QC

**Working name — rename before public release.**

Local-first data profiling, sanitization, and synthetic data generation
with a consistent CLI (`qc`) and MCP server interface.

> **Status:** active development against [`SPEC.md`](SPEC.md).

## What it does

- Connect to PostgreSQL, MySQL, Alibaba MaxCompute (via JVM JDBC sidecar),
  Google Sheets, and Lark Sheets.
- Learn schema and data behavior through **bounded, adaptive profiling** —
  every query is budgeted (rows, bytes, queries, time, cost).
- Detect likely sensitive fields and enforce **explicit policy before any
  data leaves the source boundary**.
- Sanitize (mask / redact / generalize / tokenize) or generate synthetic
  data with Faker, inferred formats, distributions, and constraints.
- Validate generated data against learned profiles and report deviations.
- Export JSONL, CSV, or to an approved destination database.
- Expose narrow, policy-aware tools to AI clients over MCP (stdio or
  Streamable HTTP).

## Security defaults

- Read-only source access; no source mutation.
- **Deny by default**: raw rows, external egress, writes, and model calls
  are all denied unless a policy explicitly allows them.
- No credentials in config or artifacts — secret *references* only
  (`env:`, `file:`, `keyring:`, `cmd:`).
- No raw values in logs, reports, or profile artifacts.

## Quickstart

```bash
uv sync
uv run qc init
uv run qc doctor
```

Full usage: see [`SPEC.md`](SPEC.md) and `docs/`.

## License

Apache-2.0 (pending dependency and organizational review — ADR-001).
