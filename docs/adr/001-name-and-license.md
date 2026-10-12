# ADR-001: Project name and license

**Status:** accepted (default) · **Date:** 2026-10-12

## Context

SPEC §19.1 requires a name and license decision before public release.

## Decision

- Keep the working name **QueryCleaner/QC** with the `qc` package and CLI
  executable. Rename before public release (tracked as release gate).
- License under **Apache-2.0**, pending dependency and organizational
  review.

## Consequences

- Package/CLI name collisions on public indexes must be re-checked at
  rename time; internal imports (`qc.*`) are cheap to rename with `rg`.
