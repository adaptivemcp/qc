# Policies

A policy is a declarative set of allowed operations, field handling,
budgets, and disclosure rules (SPEC 5.7, 13). **Deny by default.** A
policy can never grant more access than the server's configured ceiling.

## File location and format

Policies live in `policies/<name>.yaml` inside the project (YAML or
JSON). `qc init` writes `policies/strict-local.yaml`.

```yaml
version: 1
name: strict-local
description: Deny-by-default local policy
defaults:
  source_access: deny        # unlisted sources denied
  raw_row_output: deny       # raw rows denied everywhere
  source_writes: deny
  external_egress: deny
  profile_disclosure: aggregate_only   # none | aggregate_only | full
  external_model_calls: false
budget:
  max_rows: 10000
  max_bytes: 16777216
  max_runtime_seconds: 120
  max_queries: 100
  allow_full_scan: false
export_allowed: false
write_allowed: false
allow_raw_samples: false
rare_category_min: 5
allowed_destinations: []      # required non-empty if export_allowed
sources:
  analytics-readonly:
    access: read
    datasets:
      - public.orders
      - public.customers
    columns:
      customer_email: {action: synthetic}
      customer_name: {action: synthetic}
      order_id:      {action: tokenize}
      created_at:    {action: generalize, generalize_format: month}
      total_amount:  {action: keep}
      notes:         {action: manual_review}
```

## Semantics and precedence

Effective access for an operation = **ceiling ∩ policy ∩ request**; the
narrowest wins.

1. **Server ceiling** (`qc.toml` `[budgets]`/`[security]`) — hard caps
   on rows/bytes/runtime/queries, inline MCP rows, raw MCP rows, writes,
   model calls. Policy can only narrow these.
2. **Policy defaults** — apply to anything not explicitly listed.
3. **Per-source entry** — `access: read|deny`, dataset allowlist
   (empty list = none; `*` is rejected), per-column actions.
4. **Request** — tool/command arguments can only narrow further
   (e.g. `--limit`, `--max-queries`).

Column lookup is exact-name. Columns **not listed** under
`sources.<name>.columns` get `manual_review` — they are redacted in
previews and block export until reviewed (fail closed).

## Actions

| Action | Effect | Notes |
|---|---|---|
| `keep` | pass through unchanged | only for approved non-sensitive fields |
| `mask` | mask characters (`mask_char`, `mask_keep_prefix/suffix`) | structure-preserving |
| `redact` | replace with `[REDACTED]` | value gone |
| `generalize` | reduce precision (`generalize_format`) | `year`, `month`, `day`, `age_band_10`, `first_digit` |
| `tokenize` | keyed-HMAC pseudonym `tok_…` | **linkable, not anonymization**; key from `QC_TOKENIZATION_KEY` or ephemeral |
| `synthetic` | generated replacement (preview emits a placeholder token; `generate run` produces real values) | format/distribution-aware |
| `drop` | exclude the column | |
| `manual_review` | block export until a human decides | default for unlisted columns |

## Validation

```bash
qc policy validate policies/strict-local.yaml
```

Rejects: unknown keys, invalid actions/access, `*` dataset wildcards,
read access with an empty dataset list, `export_allowed` without
`allowed_destinations`, and malformed budgets. Validation runs after
`qc policy grant` mutations too.

## Granting access explicitly

Deny-by-default means an unlisted source cannot be read. Granting is an
explicit, auditable step:

```bash
qc policy grant strict-local analytics --dataset public.orders
qc policy apply strict-local analytics.public.orders   # preview the decision
```

## Budget semantics

- `max_rows` / `max_bytes` / `max_queries` / `max_runtime_seconds` —
  per-operation; `0` means "none allowed", not "unlimited".
- `allow_full_scan: false` (default) blocks any probe needing a full
  table read; `qc profile run --allow-full-scan` must *also* pass policy.
- `allow_raw_samples` + `defaults.raw_row_output: allow` are both
  required before any raw row can leave the process.

## Disclosure levels

`profile_disclosure` controls profile artifacts and MCP `get_profile`:

- `none` — column names themselves redacted (`col_0`, …).
- `aggregate_only` *(default)* — counts, types, confidences; **no**
  category values, no examples.
- `full` — category values and sensitivity detail included, still
  subject to minimum-group suppression.

## Migration and versioning

`schema_version` is embedded in every serialized artifact; unknown
future versions fail with an actionable error. Policy `version` (a free
string) is recorded in audit events and MCP provenance. Semantic changes
bump `schema_version` and ship an explicit migration (SPEC 14).
