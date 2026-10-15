# Development gap assessment

A systematic review of the implementation against `SPEC.md`, the gaps
found, and their resolution. Each item lists the spec reference, the gap,
and the fix with its tests.

## Addressed in this pass

### 1. Cancellation was declared but non-functional — SPEC 4.3, 6.1, 12.6, 15

**Gap.** Connectors advertised `cancellation=True`, and MCP exposed
`cancel_job`, but nothing checked for cancellation: a running profile or
generation could not be stopped, and `cancel_job` only flipped a database
row. Resources were not closed on cancellation.

**Fix.**
- `qc/domain/cancellation.py::CancellationToken` — thread-safe signal.
- `qc/application/cancellation.py::CancellationRegistry` (+`REGISTRY`) —
  maps job ids to tokens; cancellable from any thread.
- Profiler polls the token before every probe, per column, and between
  sampled rows; `CancelledError` is re-raised from all broad
  `except QCError` handlers so it is never swallowed.
- SQL connectors check the token between fetched batches and close the
  server-side cursor before unwinding; the fixture connector checks per
  row; the JDBC bridge already issues `job.cancel`.
- CLI `profile run` installs a SIGINT handler that cancels the job;
  MCP `cancel_job` signals the live token (returns `cancelling`).
- `run_generation` polls the token between rows and before writing output.

**Tests.** `tests/unit/test_cancellation.py` (token, registry, engine
cancel-before/midway, per-row sampling cancel, connector close).

### 2. Job lifecycle was incomplete — SPEC 5.9, 12.2, 15

**Gap.** CLI profile runs recorded a job, but MCP `profile_dataset` /
`generate_synthetic_data` did not; `job_id` was always null in MCP
envelopes, so `get_job_status`/`cancel_job` were inert for MCP work.

**Fix.** `qc/application/job_runner.py` gives both interfaces identical
job semantics: `JobRunner` (register token, persist record, progress) and
`run_job` (status transitions + audit). MCP tools now create jobs and
return `job_id`; failures record `FAILED` + stable error code;
cancellation records `CANCELLED`.

**Tests.** `tests/unit/test_cancellation.py::TestJobRunner`,
`tests/e2e/test_mcp_stdio.py`, CLI journey tests assert `job_id`.

### 3. No retry/backoff for transient failures — SPEC 6.1, 6.5, 6.6

**Gap.** "Retry only transient, idempotent operations" was not
implemented; a transient connection reset or a 429 failed the run.

**Fix.** `qc/connectors/resilience.py`:
`RetryPolicy` (bounded exponential backoff + jitter + deadline),
`is_transient` classifier, `TransientError` marker, and
`with_retry(...)` that respects cancellation and never retries
non-transient errors. PostgreSQL/MySQL `_fetch` and the Google/Lark HTTP
clients retry through it; exhausted retries surface as redacted
`SourceError`s. Retries are counted into metrics.

**Tests.** `tests/unit/test_resilience_metrics.py`,
`tests/contract/test_sheets.py` (rate-limit path).

### 4. No metrics — SPEC 15

**Gap.** Only per-profile budget usage was recorded; the SPEC metric set
(queries, rows scanned/returned, bytes, elapsed, retries, budget
utilisation, generated rows, validation failures, connector health) did
not exist.

**Fix.** `qc/observability/metrics.py`: `OperationMetrics` with
`budget_utilisation()` and `as_dict()`, plus a process-wide `RECORDER`
that emits a structured `qc.metrics` log event and exposes totals.
Wired into the profiler and generation; exposed in CLI `--json` output.

**Tests.** `tests/unit/test_resilience_metrics.py::TestMetrics`.

### 5. `--where` predicate interface missing — SPEC 11.2

**Gap.** `profile run` had no `--where`; the safe typed predicate
interface described by the spec did not exist.

**Fix.** `qc/domain/predicate.py` parses only `col=value` /
`col:op:value` (ops `=`, `!=`, `<`, `<=`, `>`, `>=`, `like`, `in`),
rejects SQL fragments, and provides `matches_row` for in-memory
connectors. `qc/connectors/sql_common.py::build_where` turns a predicate
into a parameterized WHERE clause (columns validated + quoted, values
bound). PostgreSQL, MySQL, and the fixture connector apply it to every
aggregate and to sampling. Implemented for both live backends.

**Tests.** `tests/unit/test_cancellation.py::TestPredicateParsing`,
`tests/unit/test_predicate_pool.py`, `tests/integration/test_predicate_live.py`
(live PG + MySQL), `tests/e2e/test_cli_journeys.py` (`--where` flows).

### 6. `--include-null-patterns` was a no-op — SPEC 11.2

**Gap.** The flag was accepted but ignored.

**Fix.** `preview_rows` now emits a `null_patterns` block: per-column
*counts* of distinct null-like representations (SQL null, empty string,
whitespace-only, string-null, literal N/A) — aggregate only, no values.

**Tests.** `tests/unit/test_output_formats.py::TestNullPatterns`.

### 7. CSV/JSONL output was not configurable / precision policy implicit — SPEC 10.1, 10.2

**Gap.** CSV delimiter/header/formula-protection were fixed; JSONL
decimal handling relied on `default=str` with no policy.

**Fix.** `sanitize export` gained `--delimiter`, `--header/--no-header`,
`--protect-formulas/--no-protect-formulas`, `--columns`. New
`qc/exports/jsonl.py` serializes `Decimal` as a JSON number only when it
round-trips exactly, otherwise as a lossless string; bytes are base64;
field order is stable. Manifests record the CSV options and the decimal
encoding policy.

**Tests.** `tests/unit/test_output_formats.py`.

### 8. No connection pooling with configurable limits — SPEC 6.1

**Gap.** SQL connectors reused a single connection with no configurable
bound.

**Fix.** `qc/connectors/pool.py::AsyncConnectionPool` — bounded,
dependency-free pool (min/max size, checkout semaphore, discard-on-error,
drain-on-close). The PostgreSQL connector now checks connections in and
out (`pool_min_size`/`pool_max_size`, default max 4).

**Tests.** `tests/unit/test_predicate_pool.py::TestPool`.

### 9. A hidden flaky property test

**Gap.** `test_secret_redacted_in_logs_and_errors` asserted a generated
secret substring was absent; when the secret equalled a keyword
("password") the keyword legitimately remained, making the test flaky.

**Fix.** The assertion now targets the value position (`=secret`,
`:secret@`) and requires the redaction marker, which is precise.

## Deliberately out of scope (documented, not defects)

| Area | Spec | Status |
|---|---|---|
| Differential privacy | 8.5, 19.6 | excluded for v1 (ADR-005); reports warn |
| Cross-dataset relational generation scheduling | 9.4 | declarative only; recorded as a report warning |
| Source mutation / DDL | 1.2 | never exposed; read-only by construction |
| Arbitrary SQL/Python, policy mutation over MCP | 12.3 | excluded by design; stable error |
| MCP resources / prompt templates | 12.5 | `dev.adaptivemcp://tools-metadata` is published; `qc://` resources and prompt templates declined 2026-10-15 (each duplicates a tool and adds a second surface to authorize) — revisit on concrete client demand |
| `capabilities.experimental` advertisement of the extension | 12.5, SEP-2133 | the resource is discoverable via `resources/list`; FastMCP does not expose `experimental_capabilities` yet |
| Live MaxCompute driver/dialect verification | 6.4, ADR-006 | protocol tested; live confirmation is a release gate |
| Project rename / public package name | 19.1 | resolved 2026-10-16: product **QueryCleaner**, distribution **`querycleaner`**, import/CLI **`qc`**; license Apache-2.0 (ADR-001) |
