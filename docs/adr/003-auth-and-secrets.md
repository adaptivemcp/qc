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
  gateway; integrating gateway identity natively is deferred.

## Consequences

- v1 ships without custom password auth; token auth is a stopgap that
  composes with any reverse proxy.
- Keyring support is optional (extra dependency), env/file/cmd always
  available.
