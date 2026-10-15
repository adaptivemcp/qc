# ADR-006: JDBC bridge transport and MaxCompute driver verification

**Status:** accepted (default) · **Date:** 2026-10-12

## Context

SPEC §4.3 and §19.7: newline-delimited JSON over subprocess stdio for
v1; the MaxCompute JDBC driver/auth/dialect must be verified before the
bridge contract freezes.

## Decision

- **Transport:** subprocess with stdin/stdout pipes speaking
  newline-delimited JSON (`qc.jdbc_bridge.protocol`). stdout carries
  protocol messages only; Java diagnostics go to stderr.
- **Protocol:** versioned (`protocol_version: 1`), request IDs, status,
  structured errors, deadlines, max-rows/max-bytes caps, cancel requests.
- **SQL**: Python validates/constructs all SQL; the sidecar executes
  parameterized statements only and never interpolates parameters.
- **Secrets**: delivered via inherited environment (not argv).
- **Driver**: pinned by version + SHA-256 checksum in a lock file the
  sidecar build verifies. Actual MaxCompute driver version, Java
  compatibility, and auth modes must be confirmed against current
  official documentation before GA (open item; sidecar code isolates this
  behind `MaxComputeConnector`).

## Live verification status

**Open release gate (2026-10-16).** No live MaxCompute cluster has been
driven; `driver-lock.json` still holds placeholders. The bridge protocol
is contract-tested against a fake sidecar, and the sidecar refuses to
start on a missing/mismatched lock, so an unverified build cannot ship the
sidecar enabled by accident. The gate is kept closed deliberately: the
failure modes (dialect rejection of portable sampling, an auth mode that
ignores inherited-env credentials, an unsupported Java runtime, an
unmatched checksum) are invisible to a fake sidecar and would surface only
on a user's cluster.

A reproducible verification checklist (fetch + pin the driver, build the
sidecar, exercise auth modes, probe the dialect, run the opt-in suite, and
record the result) lives in `docs/connectors.md` § "Release gate — live
driver/dialect verification". The gate is satisfied by a dated record, not
by assertion.

## Consequences

- Python-side bridge contract tests run against a scripted fake sidecar;
  live MaxCompute tests are opt-in.
