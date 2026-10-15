# ADR-003: Secret references and remote authentication

**Status:** accepted (default) · **Date:** 2026-10-12

## Context

SPEC §19.3 prefers an established identity layer for remote MCP;
§19.4 starts with environment references and OS credential stores.

## Decision

- **Secrets**: `connection_ref` scheme with resolvers
  `env:`, `file:` (chmod 600 enforced), `keyring:`, `cmd:`. Plaintext
  credentials are rejected by `SourceConfig` validation. Resolved values
  never reach artifacts, logs, or errors.
- **Remote MCP auth**: bearer token from a secret reference
  (`env:QC_MCP_HTTP_TOKEN`) enforced by the HTTP transport in v1, with
  per-client rate limits and explicit source allowlists. Deployment
  documentation recommends placing the service behind an OIDC-capable
  gateway; the native **resource-server** path (QC validating provider
  JWTs: signature, `iss`/`aud`, scopes) is designed in
  `docs/operations.md` "Remote MCP identity (OIDC)" and deferred to a
  later change.

## Consequences

- v1 ships without custom password auth; token auth is a stopgap that
  composes with any reverse proxy.
- The designed OIDC path is a **mode** (`mcp.http.mode = "static" | "oidc"`),
  so adopting it is additive: an unconfigured deployment keeps the static
  bearer exactly as shipped, and the verifier swap is isolated to
  `src/qc/mcp/server.py`.
- Keyring support is optional (extra dependency), env/file/cmd always
  available.
