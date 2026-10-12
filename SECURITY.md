# Security

## Threat model

QC processes source data that may contain personal information,
business-confidential values, credentials, tokens, and rare combinations
that identify people (SPEC 8.1).

**Adversaries considered:**

- A compromised or curious **MCP client** requesting excessive data,
  probing budgets, or attempting prompt injection through source content.
- **Log/telemetry observers** — raw values and credentials must never
  appear in logs, errors, artifacts, or crash reports.
- **Local filesystem observers** — artifacts hold aggregates only;
  credentials live in `env:`/`keyring:`/`file:` (chmod 600)/`cmd:` refs.
- **Malicious source content** — cell values, SQL identifiers, spreadsheet
  headers, and comments are *data*, never instructions.

**Out of scope for v1:** differential privacy guarantees, protection
against a global adversary with full auxiliary information, and
guaranteed anonymization (see ADR-005).

## Data handling guarantees

| Location | Raw rows | Credentials | Notes |
|---|---|---|---|
| Profile artifacts | never | never | aggregates + digests only; category values subject to minimum group sizes |
| Generation reports | never | never | metrics and violations only |
| Logs (JSON, stderr) | never | never | redaction applied to every field |
| Audit events | never | never | hashed sentinel keys, no SQL parameters |
| MCP responses | policy-gated | never | raw rows denied by default; inline row caps from server config |
| SQLite state | never | never | job metadata + profile index only |
| CSV/JSONL exports | transformed only | never | manifest records provenance, no secrets |

## Security defaults (deny-by-default)

- Read-only source sessions (`SET default_transaction_read_only` /
  `SET SESSION TRANSACTION READ ONLY`, read-only JDBC connections).
- Raw MCP row output: **denied** (`raw_mcp_rows = false`).
- Source writes: **denied** (`source_writes = false`, enforced by ceiling).
- External model calls: **denied**.
- External egress: **denied**.
- Exports: **denied** until `export_allowed: true` *and* an explicit
  `allowed_destinations` list exist.
- Unlisted sources/datasets/columns: **denied**; unlisted columns fall
  back to `manual_review` and are redacted rather than leaked.
- HTTP transport: refuses to start with `require_auth = false` or a
  missing/short token.

A policy can never grant more than the server ceiling (SPEC 13): ceilings
are checked independently on every operation.

## Injection defenses

- **SQL:** identifiers are validated against `^[A-Za-z_][A-Za-z0-9_$]*$`
  *and* quoted; dataset references containing `;`, `--`, `/*`, or NUL are
  rejected; only `SELECT`/`WITH` statements reach any wire (Python-side
  assertion for DB connectors, protocol-side assertion for the JDBC
  bridge); all values are bound as query parameters.
- **CSV formula injection:** cells starting `=`, `+`, `-`, `@`, tab, or
  CR are prefixed with `'` in safe-export mode; the manifest records
  that protection was applied.
- **Path traversal:** profile ids, fingerprints, source names, artifact
  references, and fixture paths are charset-validated; `..` segments are
  rejected; exports resolve under the project output directory only.
- **Prompt injection:** source content is passed through transformation
  and never interpreted; MCP tool outputs are structured data.
- **Deserialization:** artifacts parse with Pydantic + schema versions;
  YAML policies use `safe_load`; no pickle/eval/exec anywhere.

## Secret management

- `connection_ref` scheme: `env:NAME`, `file:/path` (chmod 600 enforced),
  `keyring:service/user`, `cmd:helper`. Inline connection strings with
  credentials are rejected by model validation.
- JDBC sidecar receives secrets via inherited environment — never argv,
  never protocol payloads (payload key scan rejects smuggled secrets).
- Google/Lark access tokens are resolved per-connector and never written
  to artifacts.
- Tokenization keys come from `QC_TOKENIZATION_KEY` or an ephemeral
  in-process random key; keys are never persisted.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the maintainers
(mailbox to be published before public release; tracked in ADR-001).
Include: affected version, reproduction steps, and impact. We
acknowledge reports within 5 business days and credit reporters who ask
for it.

## Supported deployment modes

1. **Local CLI / stdio MCP** — recommended. No listening sockets.
2. **Streamable HTTP MCP** — requires `mcp.http.enabled = true`,
   `require_auth = true`, `auth_token_ref` to a ≥16-char token, explicit
   source allowlists, per-client rate limits, and a recommended reverse
   proxy providing TLS. Bind to `127.0.0.1` unless intentionally exposed.

Place HTTP deployments behind an OIDC-capable gateway for identity
(ADR-003); the built-in bearer token is a v1 stopgap.
