# ADR-004: Local state store

**Status:** accepted (default) · **Date:** 2026-10-12

## Context

SPEC §19.5: SQLite for job metadata and profile indexing; restrictive
permissions; never raw source rows.

## Decision

- SQLite (`sqlite3` stdlib) at `state_dir/state.db`, created with
  `0600` permissions and `PRAGMA journal_mode=WAL`.
- Tables: `jobs`, `profiles` (index metadata only — profile bodies live
  as JSON files under `profile_dir`), `audit` is file-based JSONL
  (`state_dir/audit.jsonl`), not SQLite.
- Schema migrations are numbered and applied at startup.

## Consequences

- No server database needed; single-writer usage matches the local-first
  model. Concurrent multi-process writers are out of scope for v1.
