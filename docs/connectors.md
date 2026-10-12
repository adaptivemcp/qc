# Connectors

Every connector implements the same async contract
(`qc/connectors/base.py`) and runs the shared contract suite
(`tests/contract/test_connector_contract.py`). Capability declarations
tell the profiling planner what probes are possible.

| Connector | Driver dependency | Read-only guarantee | Status |
|---|---|---|---|
| `fixture` (JSONL/CSV) | none | local files, no writes | implemented (internal/testing) |
| `postgres` | `psycopg[binary]` (`qc[postgres]`) | `SET default_transaction_read_only=on` + role | live-tested (PG 16) |
| `mysql` | `mysql-connector-python` (`qc[mysql]`) | `SET SESSION TRANSACTION READ ONLY` + role | live-tested (MySQL 8.4) |
| `maxcompute` | JVM sidecar + pinned JDBC jar | read-only JDBC + SELECT-only bridge | protocol-tested; live opt-in (M4) |
| `google-sheets` | `httpx` + OAuth/service token | readonly scope; no write API calls exist | mocked-contract tested |
| `lark-sheets` | `httpx` + tenant/user token | readonly scope; no write API calls exist | mocked-contract tested |

## Adding a source

```bash
qc source add analytics --type postgres \
  --connection-ref env:DATA_SOURCE_ANALYTICS \
  --schema public \
  --dataset public.orders --dataset public.customers
```

`connection_ref` **must** be a secret reference:

| Scheme | Example | Notes |
|---|---|---|
| `env:` | `env:PG_DSN` | simplest; good for systemd/CI |
| `file:` | `file:~/.config/qc/pg.pass` | refuses files readable by group/others |
| `keyring:` | `keyring:qc/analytics` | needs `keyring` package (OS store) |
| `cmd:` | `cmd:vault get qc/pg` | fixed argv, no shell, 15s timeout |

Inline connection strings with credentials are rejected by validation.

## PostgreSQL

Permissions needed: `CONNECT` + `USAGE` on the schema; `SELECT` on target
tables. `pg_catalog`/`information_schema` reads for metadata.

- Row counts: exact `count(*)` when budget allows; else `pg_class.reltuples`
  marked *estimated* (`-1` before first VACUANALYZE → unknown).
- `pg_stats` availability is capability-flagged; results are estimates.
- `TABLESAMPLE` is capability-declared but **not** used by default: it is
  block-biased and can miss rare values (SPEC 6.2).
- Sampling: stable-key `ORDER BY <key> LIMIT/OFFSET`; `ORDER BY random()`
  only when explicitly requested (costly on large tables).
- SSL/TLS and connect/query timeouts come from the DSN/driver options.

```bash
export PG_DSN='postgresql://readonly@db:5432/analytics?sslmode=verify-full'
qc source add analytics --type postgres --connection-ref env:PG_DSN
```

## MySQL

Permissions: `SELECT` + `SHOW` (information_schema). Recommended:
a read-only account (`GRANT SELECT ON ...`), TLS via DSN.

- Handles unsigned integers (`int unsigned` preserved in `source_type`),
  `DECIMAL`, `BIT`, `JSON`, zero dates, and normalizes `sql_mode` per
  session for deterministic behavior (SPEC 6.3).
- `tinyint(1)` maps to boolean; other `tinyint` stays integer.
- Sampling: `ORDER BY RAND()` for random (bounded by LIMIT); stable-key
  otherwise.

```bash
export MY_DSN='mysql://readonly@db:3306/app?ssl_disabled=false'
qc source add app --type mysql --connection-ref env:MY_DSN --schema app
```

## MaxCompute (via JVM sidecar)

Requires Java 17+ and the pinned JDBC driver (see
`src/qc/jdbc_bridge/sidecar/BUILD.md`). Python never loads JDBC: it talks
newline-delimited JSON to the sidecar over stdio (ADR-006).

```bash
export QC_MCP_JDBC_URL='jdbc:odps://service.cn-hangzhou.maxcompute.aliyun.com!project'
export QC_MCP_JDBC_USER='...' QC_MCP_JDBC_PASSWORD='...'
export QC_DRIVER_LOCK=/path/driver-lock.json QC_DRIVER_JAR=/path/odps-jdbc.jar
qc source add mc --type maxcompute --connection-ref env:QC_MCP_JDBC_URL \
  --dataset project.table
```

- Secrets: environment only (never argv, never protocol payloads).
- Only `SELECT`/`WITH` crosses the bridge, always with `max_rows`,
  `max_bytes`, and deadline caps; cancellation issues `job.cancel`.
- Sampling uses portable `ORDER BY/LIMIT/OFFSET` only — MaxCompute
  dialect sampling is **not** assumed (SPEC 6.4) and must be
  contract-tested before use.
- Partition filters/scan-budget warnings: configurable via source
  options; oversized scans require explicit approval.
- **Open before GA (ADR-006):** verify current official driver version,
  supported Java runtime, auth modes, and SQL dialect; pin version +
  SHA-256 in `driver-lock.json` (sidecar refuses to start on mismatch).

## Google Sheets

Permissions: the `spreadsheets.readonly` scope only; the spreadsheet
must be shared with the service account / authorized user.

```bash
export GS_TOKEN="$(gcloud auth print-access-token)"
qc source add sheets --type google-sheets --connection-ref env:GS_TOKEN \
  --option spreadsheet_id=1AbC... 
qc source add sheets --type google-sheets --connection-ref env:GS_TOKEN \
  --option spreadsheet_id=1AbC --option header_row=1 --option data_range=A1:H500
```

Normalization options (SPEC 6.7, prefix `sheet_` in source options):

| Option | Values | Default |
|---|---|---|
| `header_row` | row index (0-based) | `0` |
| `data_range` | A1 range or `A:Z` | used range |
| `skip_columns` | comma-separated names | — |
| `locale` | e.g. `en_US`, `de_DE` | `en_US` |
| `profile_formulas` | `true` = profile formula text | `false` (evaluated values) |
| `merged_cells` | `reject` \| `expand` \| `missing` | `reject` |
| `stop_on_blank_row` | `true`/`false` | `false` |
| `duplicate_headers` | `suffix` \| `error` \| `first` | `suffix` |
| `coerce_types` | `true`/`false` | `true` |

Blank cell vs `""` vs formula-empty stay distinguishable when the API
exposes them. Reads are bounded (1M-cell cap, marked truncated).
Tokens are never persisted. Sheets are never modified.

## Lark Sheets

Permissions: Lark app with the sheets read scope; tenant or user
authorization (document your app's exact scopes when registering).

```bash
export LARK_TOKEN='t-...'   # tenant_access_token, read scope only
qc source add lark --type lark-sheets --connection-ref env:LARK_TOKEN \
  --option spreadsheet_id=shtcnXXX --option schema=default
```

- Provider IDs (`sheet_id`) stay separate from display names.
- Same normalization options as Google Sheets.
- Handles in-body business error codes (token expiry, rate limits) with
  retry-safe, redacted messages; respects pagination and tenant bounds.
- Explicit range allowlists via `data_range`; source sheets never modified.

## Contract suite

`tests/contract/test_connector_contract.py` runs the same assertions
against every registered connector: connect/close, discovery, missing
dataset → redacted actionable error, row bounds, null-vs-empty
preservation, `COUNT(DISTINCT)` semantics, credential-free diagnostics,
identifier/path validation, and SELECT-only enforcement.

Live database tests activate via environment:

```bash
export QC_TEST_POSTGRES_DSN='postgresql://user:pass@127.0.0.1:5432/db'
export QC_TEST_MYSQL_DSN='mysql://user:pass@127.0.0.1:3306/db'
uv run pytest tests/contract -m integration
```
