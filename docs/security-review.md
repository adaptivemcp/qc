# Security review record

**Reviewed:** 2026-11-06 · **Version:** 0.2.1 · **Scope:** the whole shipped surface —
connectors, policy engine, profiling/generation, MCP transports, CLI, artifacts, state.

This is the *record* of a review, not a second copy of [`SECURITY.md`](../SECURITY.md).
`SECURITY.md` states the stance and the guarantees; this page says what was actually
checked, which code enforces each claim, and what remains accepted risk. The row format
is deliberate: `tests/security/test_security_review.py` parses this table and fails if a
cited symbol or test stops existing, so a claim cannot outlive its enforcement.

## Trust boundaries

```
 source (untrusted data, may hold PII/secrets)
   │  read-only connector, SELECT only, bound params, identifier validation
   ▼
 QC process ── policy engine (deny by default, ceiling-capped)
   │              │
   │              ├── artifacts (aggregates + digests; schema_version gated)
   │              ├── logs/audit (redaction on every field)
   │              └── MCP client (untrusted, policy-gated, inline caps)
   ▼
 exports (denied until explicitly allowed) ─▶ destination
```

Actors, from `SECURITY.md`: compromised/curious MCP client; log observer; local
filesystem observer; malicious source content. Out of scope for v1: differential
privacy, global-adversary re-identification, guaranteed anonymization (ADR-005).

## What was checked

| # | Threat | Mitigation | Enforced by | Test |
| --- | --- | --- | --- | --- |
| 1 | Unlisted source or dataset is read anyway | deny by default; unlisted source/dataset refuses | `src/qc/security/policy.py::PolicyEngine.check` | `tests/security/test_security.py::test_disallowed_dataset_denied` |
| 2 | A tool bypasses policy by calling a connector directly | policy checked on every operation, `require` raises | `src/qc/security/policy.py::PolicyEngine.require` | `tests/security/test_security.py::test_require_raises_with_exit_code_4` |
| 3 | A permissive policy widens server limits (budget/write/egress) | ceiling is checked independently and only narrowed | `src/qc/security/policy.py::Ceiling` | `tests/security/test_security.py::test_args_cannot_exceed_ceiling` |
| 4 | Raw rows leave through MCP | `raw_mcp_rows` denied; `ROW_OUTPUT` gate | `src/qc/security/policy.py::Operation` | `tests/security/test_security.py::test_raw_sample_denied` |
| 5 | Source is written to through a “read-only” connector | session set read-only; SQLite read-only at the engine | `src/qc/connectors/postgres.py`, `src/qc/connectors/mysql.py` | `tests/unit/test_sqlite_connector.py::test_writes_refused_by_engine` |
| 6 | SQLite writes, DDL or `ATTACH` slip past `query_only` | authorizer denies non-read statements and pragmas | `src/qc/connectors/sqlite_conn.py::_deny_write` | `tests/security/test_security.py::test_source_connector_declares_no_writes` |
| 7 | SQL injection through a dataset/column name from config | identifiers validated then quoted; only SELECT/WITH cross the wire | `src/qc/connectors/sql_common.py::quote_ident` | `tests/security/test_security.py::test_identifier_rejected` |
| 8 | Template bug sends a non-SELECT statement | last-resort statement guard before any wire call | `src/qc/connectors/sql_common.py::assert_not_dangerous_sql` | `tests/security/test_security.py::test_only_select_crosses_bridge` |
| 9 | Credentials smuggled in a JDBC payload or argv | payload key scan; secrets arrive only via inherited env | `src/qc/jdbc_bridge/protocol.py::validate_request_payload` | `tests/contract/test_jdbc_bridge.py::test_secret_in_connect_payload_rejected` |
| 10 | Secrets appear in logs, errors or crash output | field-level redaction applied on every log call | `src/qc/security/redaction.py::redact_text` | `tests/security/test_security.py::test_redaction_of_injection_payload_in_errors` |
| 11 | Inline connection strings with passwords are accepted | model validation rejects inline credentials; `connection_ref` schemes only | `src/qc/security/secrets.py::resolve_secret` | `tests/security/test_security.py::test_source_config_rejects_inline_creds` |
| 12 | Artifacts leak raw values or traverse the filesystem | aggregates only; ids charset-validated; `..` rejected | `src/qc/artifacts/profiles.py::artifact_path` | `tests/security/test_security.py::test_profile_ref_rejected` |
| 13 | CSV export executes a formula in a spreadsheet | leading `= + - @` tab/CR are prefixed; manifest records it | `src/qc/exports/csv_writer.py::csv_safe` | `tests/security/test_security.py::test_formula_prefix_escaped` |
| 14 | Source content is treated as instructions (prompt injection) | values are transformed, never interpreted; tool output is structured | `src/qc/profiling/engine.py` | `tests/security/test_security.py::test_injection_content_never_executed` |
| 15 | HTTP transport starts without authentication | refuse unless `require_auth` and a ≥16-char token ref are set | `src/qc/mcp/server.py::StaticTokenVerifier` | `tests/security/test_security.py::test_refuses_disabled_auth` |
| 16 | Artifacts from a newer QC are half-read, or state.db is silently downgraded | schema-version policy: same reads, older migrates, newer refuses | `src/qc/artifacts/versions.py::check_supported` | `tests/unit/test_artifact_versions.py::test_plan_from_the_future_is_refused` |
| 17 | State directory is world-readable | files created 0600, directory 0700 where supported | `src/qc/application/jobs.py::StateStore` | `tests/security/test_state_permissions.py::test_state_database_is_private` |

## Residual risks (accepted, with the reason)

| Risk | Why it is accepted | What would change it |
| --- | --- | --- |
| Sensitivity detection is regex/checksum based | Precision/recall on real corpora is unknown; a detector that guesses harder produces false confidence | A labelled corpus and a measured false-positive/negative rate |
| `cmd:` secret helper is trusted | It runs a fixed argv with no shell, but the helper's own behaviour is out of QC's control | A sandboxed helper protocol, or keyring-only |
| Tokenization key via `QC_TOKENIZATION_KEY` | The key must live somewhere the process can read; a compromised host defeats it | KMS/HSM-backed key resolution |
| HTTP bearer token is a v1 stopgap | Constant-time compare, but no rotation, audience or scopes | OIDC gateway (ADR-003) in front, which is the documented deployment |
| MaxCompute live behaviour unverified | The bridge protocol is tested against a fake; a real driver may differ (#21) | The release gate in ADR-006 |
| Spreadsheet connectors are mocked in CI | Live access needs tenant credentials; the contract pins shape and read-only behaviour only | A live test tenant in CI |

## Re-running this review

The review is a checklist, not a ceremony. Before a release:

1. `uv run pytest tests/security tests/unit/test_artifact_versions.py -q` — the enforcing
   tests must pass *before* the claims are believed.
2. Re-read the trust-boundary diagram against the code: a new connector, transport or
   artifact type needs a row here.
3. Confirm each accepted risk still has its reason; a new caller can retire one, and a
   changed constraint can invalidate one.
4. Update the version and date above — the test compares the version with
   `pyproject.toml`, so a stale record fails CI.

## Not covered

Dependency-vulnerability scanning of third-party packages, container image hardening, and
host-level isolation are deployment concerns, not properties of this codebase. They are
handled by the `security_board` tooling around the repo and by the operator's environment,
and claiming otherwise here would be a review that reviewed itself.
