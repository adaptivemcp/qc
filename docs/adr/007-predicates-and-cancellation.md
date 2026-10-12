# ADR-007: Typed predicates and cooperative cancellation

**Status:** accepted · **Date:** 2026-10-12

## Context

SPEC 11.2 requires `--where` to be "a safe typed predicate interface, not
arbitrary concatenated SQL". SPEC 4.3/6.1/12.6 require query
timeout/cancellation that closes resources, and SPEC 6.1 requires
"retry only transient, idempotent operations". None of these existed.

## Decision

- **Predicates** are parsed into a typed structure
  (`qc/domain/predicate.py`): `col=value` or `col:op:value` with
  `op ∈ {=, !=, <, <=, >, >=, like, in}`. Raw SQL fragments are rejected.
  SQL connectors translate the structure with `build_where` (columns
  validated + quoted, values bound as parameters); in-memory connectors
  use `matches_row`. Only this path reaches SQL.
- **Cancellation** is cooperative via `CancellationToken`
  (`qc/domain/cancellation.py`) and a process registry
  (`qc/application/cancellation.py`). Work polls it between probes,
  columns, and fetched/sampled batches; `CancelledError` is re-raised
  from every broad error handler and never records raw values.
  `cancel_job` (MCP) and SIGINT (CLI) signal the token and close cursors.
- **Jobs** are created by `JobRunner`/`run_job` for both CLI and MCP, so
  `job_id`, status, progress, and cancellation are uniform (SPEC 5.9).
- **Retries** use `with_retry` + `RetryPolicy` (bounded exponential
  backoff + jitter + deadline) and `is_transient`; only reads retry.
- **Metrics** (`qc/observability/metrics.py`) capture the SPEC 15 metric
  set, emitted as structured logs and available to callers.

## Consequences

- Predicates cannot express arbitrary expressions (deliberate); complex
  filtering remains a non-goal for v1.
- Cancellation is cooperative, not preemptive: a single long-running
  query is interrupted at the driver's `cancel()` (JDBC bridge) or
  between batches (DB-API). Documented in docs/operations.md.
- Retry is limited to idempotent reads; write paths never retry.
