# Development gap assessment

A systematic review of the implementation against `SPEC.md`, the gaps
found, and their resolution. Each item lists the spec reference, the gap,
and the fix with its tests.

For the MCP SDK tiering checklist (conformance, triage labels, releases, docs,
dependency policy, roadmap) and where this server falls short, see
[`sdk-tiers.md`](sdk-tiers.md).

This document is **point-in-time**: "Addressed in this pass" records one
review pass, not a live tracker. New drift is caught by the recurring
cadence below rather than by trusting this list to stay current.

## Review cadence

A lightweight pass runs **at each milestone and before any release**, not
on a fixed clock (there is no separate reviewer, so a time-based cadence
would just go stale). Steps:

1. **Diff against the spec.** Walk the `SPEC.md` sections the code has
   touched since the last pass; the spec is the source of truth, and the
   implementation must meet it or explicitly defer.
2. **Re-validate the exclusions.** Re-read the "Deliberately out of scope"
   table and confirm each row still holds — a new caller, a dependency
   release, or a changed constraint can retire one.
3. **Record new items.** Append dated sections under "Addressed in this
   pass" using the template below; keep the spec reference in the heading.
4. **Pin the fix.** Run the full suite (`uv run pytest`) and name the
   covering tests in the item's "Tests." line so the claim is checkable.

### Item template

```markdown
### N. <one-line gap> — SPEC <refs>

**Gap.** What the spec requires and what was actually missing or deviant.

**Fix.** The change, naming the files/modules that carry it (and the ADR,
if the fix encodes a decision).

**Tests.** The test file(s) and cases that pin the behavior.
```

## Addressed in this pass

### 10. Currency-like values were never inferred — SPEC 7.6, 9.3

**Gap.** SPEC 7.6 lists "currency-like value" among the logical types to
infer independently of the declared type. `LogicalType.CURRENCY` existed and
Postgres' `money` mapped to it, but no observation ever produced it: a text
column of `$12.50` stayed `string`. Downstream, generation could not preserve
an amount's marker because it never knew one was there.

**Fix.**
- `qc/profiling/type_inference.py` — `CURRENCY_RE` and a `currency` format
  builder. A currency marker (**symbol or ISO code**) attached to a number is
  the evidence; a bare number is deliberately *not* currency, because an
  amount column and a plain decimal are indistinguishable without it and
  SPEC 7.6 asks for ambiguity to be retained rather than guessed away.
- `qc/profiling/engine.py` — currency columns collect format patterns, so the
  marker and its placement survive profiling.
- `qc/generation/strategies.py` — `currency_amount`, which re-emits the
  observed marker (`$` prefix, `USD` suffix), grouping, and the observed
  numeric range when known. Registered before `format_generator`.

**Tests.** `tests/unit/test_type_inference.py`
(`test_currency_with_symbols`,
`test_currency_with_iso_code_and_grouping`,
`test_currency_symbol_after_the_amount`,
`test_bare_numbers_are_not_currency`,
`test_currency_mixed_with_plain_text_is_ambiguous`,
`test_currency_pattern_keeps_the_marker`) and
`tests/unit/test_currency_generation.py` (marker prefix/suffix, grouping,
range, determinism, no-pattern fallback).

### 11. `uv run pytest` errored instead of skipping live DB tests — SPEC 16.3

**Gap.** `docs/gaps.md` names `uv run pytest` as the full-suite command, but
running it without `QC_TEST_MYSQL_DSN` raised `KeyError` from
`_mysql_schema()` inside a fixture — before the test body's own `pytest.skip`
could run. The suite reported a collection/teardown error rather than
"skipped".

**Fix.** `tests/integration/test_db_export.py::TestMySQLDestination.dest_config`
skips before deriving the schema, matching every other live fixture.

**Tests.** `uv run pytest` — 477 passed, 39 skipped, exit 0 (previously
errored).

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

### 12. Country-code and postal-code inference was deferred — SPEC 7.6

**Gap.** `LogicalType.COUNTRY_CODE` and `POSTAL_CODE` existed and generation supported
both, but no observation ever produced them: `COUNTRY_RE` matched in `_classify_value` and
was deliberately ignored, `POSTAL_RE` was never referenced. `US` stayed
`categorical_string`, `90210` stayed an integer.

**Fix.**
- `qc/profiling/type_inference.py` — inference gated on a **name or declared-type hint**
  (`country`/`country_code`/`iso_country`, `postal`/`postcode`/`zip`/`pincode`), because
  shape alone is not evidence. Country additionally requires every value in the ISO 3166-1
  alpha-2 set; postal requires a known shape and reports `locale_hint:
  postal_shape_only`. Both come back `ambiguous=True` with a note naming the alternative
  reading.
- `qc/profiling/engine.py` — passes the column name as the hint.
- Generation preserves the shape (existing `format_generator`), so an inferred
  `country_code` emits a real code and `postal_code` a five-digit form.

**Tests.** `tests/unit/test_type_inference.py`: hint required for country, ISO-set
rejection, hint required for postal, locale ambiguity reported, postal hint without a
shape unchanged, zero-padded values still non-postal without the hint.

### 13. Sensitivity-detector accuracy was unmeasured — SPEC 8.3

**Gap.** The detectors are layered and their precision/recall on any corpus was unknown;
"regex based" was the whole story.

**Fix.** `tools/bench/sensitivity_bench.py` scores the real detector over 23 labelled
columns (13 sensitive, 10 controls, three with a sensitive word in the name). It caught two
defects, both fixed:

- `123-45-6789` matched the loose phone pattern, so an SSN column was reported as
  `government_id` *and* `phone` — government-ID/date shapes are now excluded from the phone
  detector.
- the name layer treated a sensitive word as proof: `email_verified` (boolean),
  `token_count` (count) and `phone_model` (product names) were flagged, giving precision
  0.75/0.67/0.50. A name-derived label is now dropped when the values contradict it
  (booleans; all-numeric for email/credential; a "phone" column with fewer than a fifth of
  values carrying seven digits). Masked addresses and digit-only phones still survive.

The fixture was then hardened with the shapes real tables contain — masked addresses,
E.164 and spaced numbers, JWTs, base32 secrets, free text with contact details, plus
near-miss controls (SKUs, 40-hex git SHAs, UUID request ids, ISO timestamps). That found a
third defect: a git SHA was flagged as a credential, so digest-shaped values (32/40/64/128
hex) now only count when the column name already claims a credential. Then **embedded
extraction** landed: free text reports `free_text` plus `email`/`phone` with
`evidence: embedded:*`, guarded against the two shapes that made it noisy (a bare eight-digit
run in prose, and dates/times such as `2025-05-01T12:00:00Z`). Unseparated numbers are now
also extracted, but only when the sentence justifies it: a phone word near the digits makes
them a phone, a reference word without one (`order 12345678`, `invoice 90123456`) does not,
and a compact date (`20250501`) never does. That is four prose columns with identical digit
counts, added to the fixture and classified correctly.

Numbers and caveats: `docs/profiling.md#sensitivity-detector-accuracy`.

**Tests.** `tests/unit/test_sensitivity_bench.py` (controls present, scoring arithmetic,
bounded metrics) and `tests/security/test_security.py` for the detector layer itself.

### 14. Profiles did not state how much of a dataset they saw — SPEC 7.4, 7.7

**Gap.** An artifact carried `sample_size` and `row_count` in different places and left the
reader to divide them, and the only disclosure was a warning string. "Sampled 1,000 rows" is
not a claim about coverage, and a sample cannot establish that a rare value is absent.

**Fix.** `qc/profiling/representativeness.py` — `summarize_sampling()` produces a
`Representativeness` block on every artifact: method, sampled rows, total, coverage (or
`null` when the total is unknown), a confidence rule (high for a full scan, medium at ≥ 20%
coverage of an exactly-counted table, low otherwise), one statement safe to show a user, and
the limitations a reader would otherwise assume away. The artifact schema moves to 2; v1
artifacts migrate with the field absent, because they did not record it.

Deliberately **not** a confidence interval: that needs per-column variance, a chosen level,
and still cannot answer "does this value exist at all".

**Tests.** `tests/unit/test_representativeness.py` (7: coverage arithmetic, the confidence
rule, estimated totals, unknown totals, full scan, no sampling, short samples),
`tests/contract/test_fixture_profiling.py` (artifact carries the block and its statement),
and `tests/unit/test_artifact_versions.py` (v1 → v2 migration).

### 15. Generation reports had no version-checked reader — SPEC 10.4, 14

**Gap.** `run_generation` and `validate_output` write report artifacts, and nothing in QC
read them back. The schema-version rule therefore applied to profiles, plans and `state.db`
but not to the reports a user is most likely to archive and revisit.

**Fix.** `qc/artifacts/reports.py::load_report` applies `check_supported` and validates the
model; `qc report show <path> [--json]` is the reader — a summary in text, the artifact in
JSON, both refusing a report written by a newer QC with the upgrade instruction. Corrupt
and version-less files fail as validation errors rather than tracebacks.

**Tests.** `tests/unit/test_artifact_versions.py` (supported version loads, future refused,
corrupt/missing/version-less), and the CLI journey now reads the report its own
`generate run` produced (`tests/e2e/test_cli_journeys.py`).

### 16. The `state.db` migration registry was empty and untested — SPEC 14

**Gap.** `StateStore` checked `schema_version` and refused a newer file, but `_MIGRATIONS`
was empty, so the upgrade path for an *older* file had never been exercised. The first real
schema change would have shipped without a test that it upgrades in place or preserves rows.

**Fix.** The registry now has a real entry — v2 adds `idx_jobs_created`, the index behind
`qc job list` — and `_migrate` applies entries from an injectable registry (default: the
real one) in ascending order, updating `schema_version` after each. Fresh files are created
at the current version. Entries are written idempotently so a crash between script and
version bump replays safely.

**Tests.** `tests/unit/test_state_migrations.py` (5): a hand-built v1 database with a job
and a profile row is upgraded in place with both rows intact and the new index present;
three reopenings apply the migration exactly once; a fresh file starts at the current
version; a newer file is still refused; injected migrations run in version order.

### 17. HTTP auth stopped at one shared token — SPEC 12.4, ADR-003

**Gap.** `docs/operations.md` specified an OIDC resource server — JWKS signature check,
`iss`/`aud`/`exp`, required scopes, audience binding — but the code wired only
`StaticTokenVerifier` with `validate_token_resource=False`. Every remote deployment needed an
OIDC gateway to compensate, and the QC hop still carried one shared credential.

**Fix.** `mode = "oidc"` selects `JwtTokenVerifier` (`src/qc/mcp/oidc.py`): RS256/ES256 only
(`none`/HS256 refused before claims are read), key by `kid` from a cached JWKS with one
refetch on an unknown `kid` (rotation, bounded against a fetch loop), `iss`/`aud`/`exp`/`nbf`
with 30s leeway, and `required_scopes`. The server binds the verifier to its audience
(`validate_token_resource=True`), so a token minted for another service cannot open QC.
Failures are `401 invalid_token`; the reason is logged, never returned.

**Tests.** `tests/unit/test_mcp_oidc.py` (15): valid RS256/ES256, expired, wrong audience,
wrong issuer, missing scope, unsigned/HMAC refusal, JWKS caching, rotation, discovery, config
parsing and the `_auth_for` wiring (verifier + audience binding). Offline: the JWKS document
is injected.

## Deliberately out of scope (documented, not defects)

| Area | Spec | Status |
|---|---|---|
| Differential privacy | 8.5, 19.6 | excluded for v1 (ADR-005); reports warn |
| Cross-dataset relational generation scheduling | 9.4 | declarative only; recorded as a report warning |
| Source mutation / DDL | 1.2 | never exposed; read-only by construction |
| Arbitrary SQL/Python, policy mutation over MCP | 12.3 | excluded by design; stable error |
| MCP resources / prompt templates | 12.5 | `dev.adaptivemcp://tools-metadata` is published; `qc://` resources and prompt templates declined 2026-10-15 (each duplicates a tool and adds a second surface to authorize) — revisit on concrete client demand |
| ~~`capabilities.experimental` advertisement of the extension~~ | 12.5, SEP-2133 | **Done 2026-11-07.** The earlier note said FastMCP could not expose it; that was wrong — the wire schema carries `extensions`, and `advertise_only_implemented()` now declares `dev.adaptivemcp/tools-metadata` in `initialize`, gated on the resource actually being registered. `tests/unit/test_mcp_capabilities.py` asserts both directions (advertised when served, absent when not). |
| Live MaxCompute driver/dialect verification | 6.4, ADR-006 | protocol tested; live confirmation is a release gate |
| Project rename / public package name | 19.1 | resolved 2026-10-16: product **QueryCleaner**, distribution **`querycleaner`**, import/CLI **`qc`**; license Apache-2.0 (ADR-001) |
