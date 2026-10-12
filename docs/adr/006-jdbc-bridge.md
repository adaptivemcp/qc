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

## Consequences

- Python-side bridge contract tests run against a scripted fake sidecar;
  live MaxCompute tests are opt-in.
