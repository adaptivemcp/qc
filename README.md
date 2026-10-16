# QueryCleaner/QC

Local-first data profiling, sanitization, and synthetic data generation
with a consistent **CLI (`qc`)** and **MCP server** interface. QC learns
data behavior through bounded, inspectable evidence, enforces privacy
policy *before* disclosure, generates data with explicit constraints,
validates the result, and makes every important decision traceable.

> Status: implementation against [`SPEC.md`](SPEC.md). See
> [SECURITY.md](SECURITY.md) for the threat model and data-handling
> guarantees.

## What it does

- **Connect** to PostgreSQL, MySQL, SQLite, Alibaba MaxCompute (via JVM JDBC
  sidecar), Google Sheets, and Lark Sheets — read-only by default.
- **Discover** schemas, constraints, keys, and spreadsheet structure.
- **Profile** adaptively under row/byte/query/time budgets: logical types
  (ambiguity retained), null/falsy variants kept distinct, distributions,
  formats, relationships, sensitivity detection, evidence + confidence.
- **Sanitize** with per-column actions: `keep`, `mask`, `redact`,
  `generalize`, `tokenize` (keyed HMAC), `synthetic`, `drop`,
  `manual_review` — deny-by-default.
- **Generate** synthetic data with seeded reproducibility and validated
  fidelity (hard constraints vs soft tolerances reported separately).
- **Export** JSONL/CSV (null encoding + formula-injection protection) or
  an approved destination database, with provenance manifests.
- **Serve** AI clients over MCP (stdio or authenticated Streamable HTTP)
  with 11 narrow, bounded, policy-enforced tools.

## Quickstart

QC is distributed as the **`querycleaner`** package (the `qc` name is taken
on PyPI); the import package and CLI are both `qc`.

```bash
uv tool install querycleaner   # end users; or: pipx install querycleaner
```

For development from a checkout:

```bash
uv sync
source .venv/bin/activate   # bash/zsh; fish: source .venv/bin/activate.fish
```

`uv sync` installs the `qc` entry point into `.venv/bin/` but does **not**
activate it, so `.venv/bin` is not on your `PATH` until you run the
second line. That is the only setup step the examples below assume.

No-activation alternative: `uv run qc …` resolves the entry point from
this project (it works from the repository directory; `uv run --project
/path/to/qc` works from anywhere) — the Development section uses it
throughout.

```bash
qc init                      # qc.toml, strict policy, directories
qc doctor                    # environment + connector diagnostics

qc source add analytics --type postgres \
  --connection-ref env:PG_DSN --schema public
qc source test analytics
qc policy grant strict-local analytics --dataset public.orders   # explicit grant
qc dataset list analytics

qc profile run analytics public.orders --max-queries 40 --sample-size 2000
qc profile run analytics public.orders --where region=US --where age:>=:18
qc profile show <profile-id>

qc sanitize preview analytics public.orders --policy strict-local --limit 10
qc sanitize export  analytics public.orders --policy strict-local \
  --output output/safe.jsonl
qc sanitize export  analytics public.orders --policy strict-local \
  --output output/safe.csv --format csv --delimiter ';' --no-header

qc generate plan <profile-id> --rows 1000 --seed 42
qc generate run reports/plan_*.plan.json --output output/synth.jsonl
qc generate validate output/synth.jsonl --profile <profile-id>
```

SQLite needs no server — point a source at a local file:

```bash
export QC_SQLITE_PATH="$PWD/data/analytics.db"
qc source add analytics --type sqlite --connection-ref env:QC_SQLITE_PATH \
  --dataset main.orders
qc policy grant strict-local analytics --dataset main.orders
qc profile run analytics main.orders
```

### Try it with a sample database

If you have no SQLite file handy, generate one that deliberately exercises
QC's harder paths — layered sensitivity detection, type-inference
ambiguity, null/falsy variants, and a `WITHOUT ROWID` table:

```bash
mkdir -p workbench && cd workbench
qc init
python3 "$QC_REPO/tools/make_demo_db.py" --out demo.db --reset

export SQLITE_QC="$PWD/demo.db"          # fish: set -x SQLITE_QC "$PWD/demo.db"
qc source add demo --type sqlite --connection-ref env:SQLITE_QC
qc source test demo
qc dataset list demo
qc profile run demo main.customers --max-queries 60 --sample-size 800
```

where `QC_REPO` is this checkout, e.g. `QC_REPO=$(git rev-parse --show-toplevel)`
before you `cd`. All values are fabricated (example.com domains, 555-01xx
phones, documentation IP ranges, Luhn-valid card numbers issued by
nobody), and the generator is deterministic.

The six tables deliberately target: layered sensitivity detection
(`customers`), free text and null/falsy variants (`orders`), type-inference
ambiguity including an untyped mixed column and `NaN`/`Infinity`
(`events`), a `WITHOUT ROWID` table (`event_log`), view discovery
(`metrics_daily`), and zero-row aggregates (`empty_audit`). Two behaviors
look like bugs but are correct fail-closed design — see
[docs/gaps.md](docs/gaps.md) and the demo walkthrough in your
`workbench/README-demo.md`.

Fixture (JSONL/CSV) sources let you try everything without a database:

```bash
export QC_FIXTURE_ROOT=$PWD/data
qc source add demo --type fixture \
  --connection-ref env:QC_FIXTURE_ROOT --dataset people.jsonl
```

The Quickstart uses `env:PG_DSN` for brevity. Before wiring a **real**
database, read [Credentials](#credentials) — it covers the OS keyring on
Linux, macOS, and Windows, and how to confirm nothing leaked to disk.

## Credentials

QC never stores credentials. `qc.toml`, `sources.json`, and policies hold a
**reference** only; the value is resolved in-process at connect time and is
never written to artifacts, logs, audit entries, or error messages.

| Scheme | Secret lives in | Use for |
|---|---|---|
| `cmd:<helper>` | OS keyring / credential manager, read by a script | **interactive work — start here** |
| `file:/path` | a file you protect (`chmod 600`, or NTFS ACL) | servers, containers, CI images |
| `keyring:service/account` | OS store via the `keyring` package | shorthand, once installed |
| `env:VAR` | process environment | supervised environments only |

Prefer `cmd:` when *you* are typing the command. `env:` leaves the value in
the process environment where `env`, `ps -e`, and every child process can
read it — correct when a supervisor injects it, wrong when you type it.

### How `cmd:` behaves

`cmd:` runs a fixed argument vector with **no shell**, and splits arguments
on whitespace. Two consequences worth knowing before you write a helper:

- `;`, `|`, `&&`, and `$(...)` are passed through literally, never
  interpreted — so a helper reference cannot be turned into a shell command.
- **A path containing a space cannot be used.** There is no quoting.
  Keep helper paths space-free: `~/.config/qc-secrets/…`, `C:\qc-secrets\…`.

Because only *attribute names* appear on the command line, the password
never reaches shell history or `ps` output.

### Linux — GNOME Keyring / KWallet

```bash
# 1. store — prompted, so the password never enters shell history
read -rsp "DB password: " PW && echo "$PW" \
  | secret-tool store --label="QC postgres analytics" service qc account analytics
unset PW

# 2. helper — prints the password and nothing else
mkdir -p ~/.config/qc-secrets
printf '%s\n' '#!/bin/sh' \
  'exec secret-tool lookup service qc account analytics' \
  > ~/.config/qc-secrets/pg-analytics.sh
chmod 700 ~/.config/qc-secrets/pg-analytics.sh

# 3. reference it
qc source add analytics --type postgres \
  --connection-ref "cmd:$HOME/.config/qc-secrets/pg-analytics.sh"
```

`secret-tool` talks to a running D-Bus session keyring. From a bare SSH
session or a systemd unit there may be no unlocked keyring — unlock it, or
run the command under `dbus-run-session`. `pass` works the same way if you
prefer it: use `-- gpg -dq ~/.password-store/qc-analytics.gpg` as the helper
body.

### macOS — Keychain

> Not yet run on this machine — please confirm and send feedback.

```bash
# 1. store (-w with no value prompts, so nothing lands in history)
security add-generic-password -a analytics -s qc -U -w

# 2. helper — -w prints the password alone, without the `password: "…"` header
mkdir -p ~/.config/qc-secrets
printf '%s\n' '#!/bin/sh' \
  'exec /usr/bin/security find-generic-password -w -a analytics -s qc' \
  > ~/.config/qc-secrets/pg-analytics.sh
chmod 700 ~/.config/qc-secrets/pg-analytics.sh

# 3. reference it
qc source add analytics --type postgres \
  --connection-ref "cmd:$HOME/.config/qc-secrets/pg-analytics.sh"
```

macOS may show a one-time GUI prompt the first time a script reads an item.
If the lookup fails silently instead, the item's ACL does not trust the
reader; `security unlock-keychain` also clears a locked login keychain.

### Windows — DPAPI

> Not yet run on this machine — please confirm and send feedback.

Windows has no `chmod`; access is governed by NTFS ACLs. The portable
choice is a **DPAPI-encrypted** file, which only your Windows account on
that machine can decrypt.

```powershell
$dir = "$env:USERPROFILE\qc-secrets"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

# 1. store — prompts securely, then encrypts with your user key
Read-Host "DB password" -AsSecureString |
  ConvertFrom-SecureString |
  Set-Content "$dir\pg-analytics.txt"

# 2. read helper
@'
$sec = Get-Content "$PSScriptRoot\pg-analytics.txt" | ConvertTo-SecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
'@ | Set-Content "$dir\read-pg-analytics.ps1"

# 3. thin .cmd wrapper (QC runs this directly, no shell)
@'
@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0read-pg-analytics.ps1"
'@ | Set-Content "$dir\get-pg-analytics.cmd" -Encoding ascii

# 4. reference it — forward slashes, and no spaces in the path
qc source add analytics --type postgres `
  --connection-ref "cmd:C:/Users/$env:USERNAME/qc-secrets/get-pg-analytics.cmd"
```

If your profile path contains a space (`C:\Users\First Last\…`), move the
folder to `C:\qc-secrets\` — `cmd:` cannot quote it.

### Shorthand with `keyring:`

Works across all three platforms once installed — Credential Manager on
Windows, Keychain on macOS, Secret Service on Linux (which needs a Secret
Service backend present):

```bash
uv add keyring
qc source add analytics --type postgres --connection-ref keyring:qc/analytics
```

### Fallback — a protected file

```bash
mkdir -p ~/.config/qc && umask 077
printf '%s\n' 'postgres://user:pw@host:5432/db' > ~/.config/qc/analytics.dsn
chmod 600 ~/.config/qc/analytics.dsn
qc source add analytics --type postgres --connection-ref file:~/.config/qc/analytics.dsn
```

Keep it **outside the repository**, so a future `.gitignore` mistake cannot
expose it. (`file:` needs no extra dependency and works on Windows, where
NTFS ACLs stand in for POSIX modes.)

> The permission check runs when the secret is first **resolved**, not when
> the source is registered. A `chmod 644` file is accepted by
> `qc source add` and fails later, at connect time, with
> `secret file … is readable by group/others`. Set the mode up front.

### Verify before you trust it

```bash
qc source list                  # cmd:***   — scheme only, never the helper path or value
qc source inspect analytics     # "connection_ref": "cmd:***"

# confirm the password reached no file on disk (prints names only, not the value)
read -rsp "Password: " PW
hits=0
for d in workbench "$HOME/.local/share/qc"; do
  if [ -d "$d" ]; then
    grep -rIl -- "$PW" "$d" && hits=1
  else
    echo "skip (not present): $d"
  fi
done
[ "$hits" = 0 ] && echo "no leak"
unset PW
```

Run it from the directory holding your QC project. Missing directories are
reported rather than silently counted as clean — `grep` exits non-zero on
a missing path just as it does on no-match, which would otherwise print a
misleading "no leak".

A failing connection is the other leak-prone path, since driver exceptions
often embed the DSN. QC strips them, so only host/port should appear:

```bash
qc source test analytics
# error [INTERNAL]: SourceError: OperationalError: connection failed:
#   connection to server at "10.0.0.5", port 5432 failed …
```

### Troubleshooting

| Symptom | Cause |
|---|---|
| `secret file … is readable by group/others` | mode too open — `chmod 600` |
| `secret file not found` | bad path, or `~` not expanded |
| `secret helper failed to run` | helper path has a space, or is not executable (`chmod 700`) |
| `secret helper exited with N` | helper failed; run it by hand to see why |
| `secret helper produced no output` | empty store entry, or the keyring locked |
| `keyring support not installed` | `uv add keyring` |
| `environment variable … is not set` | `env:` reference with the variable unset |

Finally, connect with a **least-privilege read-only account**. QC opens
read-only sessions, but the account's grants are the real boundary.

## Share data with an AI, safely

QC never sends your data anywhere. It produces a **sanitized file** that *you*
then choose to upload, so disclosure stays an explicit, auditable step you
control. Two routes, both deny-by-default.

### Route 1 — export a sanitized file (recommended)

```bash
# 1. Inspect before you export anything
qc sanitize preview analytics public.orders --policy share-ai --limit 10

# 2. Write the sanitized file + provenance manifest
qc sanitize export analytics public.orders --policy share-ai \
  --output output/share.jsonl

# 3. Optional: CSV for a spreadsheet, with formula-injection escaping
qc sanitize export analytics public.orders --policy share-ai \
  --output output/share.csv --format csv --delimiter ';'

# 4. Confirm nothing raw left the process
grep -o 's3cr3t\|real@customer\.com' output/share.jsonl   # expect no hits
```

Every export writes a `.manifest.json` next to the file recording rows,
source, dataset, policy version, and — importantly —
`contains_raw_samples: false`.

### Route 2 — let an agent query through MCP

No file leaves your machine; the agent calls `preview_sanitized_data` and
receives only policy-permitted rows (see [MCP](#mcp) below). Prefer this
when you want an agent to *work with* the data rather than receive a
one-off extract.

### Choosing column actions

| Goal | Action | Notes |
|---|---|---|
| Keep non-sensitive fields | `keep` | only for fields you've judged safe |
| Keep shape, hide value | `mask` | `mask_keep_prefix/suffix` |
| Remove entirely | `redact` | value replaced with `[REDACTED]` |
| Reduce precision | `generalize` | `year`, `month`, `day`, `age_band_10`, `first_digit` |
| Keep rows joinable | `tokenize` | keyed HMAC — **linkable, not anonymous** |
| Replace with fake data | `synthetic` | best choice for anything identifying |
| Exclude | `drop` | |

A policy that shares data with an AI while keeping structure:

```yaml
sources:
  analytics:
    access: read
    datasets: [public.orders]
    columns:
      order_id:     {action: tokenize}   # stable, non-reversible
      customer_id:  {action: tokenize}   # keep joins working
      customer_email: {action: synthetic}
      customer_name:  {action: synthetic}
      created_at:   {action: generalize, generalize_format: month}
      total_amount: {action: keep}
```

Then allow the export destination explicitly — without this the export is
denied, by design:

```yaml
export_allowed: true
allowed_destinations: ["my-local-analysis"]
```

### Before you upload: four honest limits

1. **`tokenize` is pseudonymization, not anonymization.** It is a keyed
   HMAC, so it stays joinable *and* reverses if the key leaks. Use it for
   identifiers you need to correlate, not for names or emails.
2. **Synthetic data is not anonymous.** Generation learns from real
   distributions, which can leak rare combinations. Reports say so
   explicitly (ADR-005).
3. **Unlisted columns block export.** Anything not in `columns:` defaults
   to `manual_review` and is redacted — fail closed, never silent.
4. **A sanitized export is still your data.** Review it. `qc sanitize
   preview` shows exactly what would be shared, per row.

Also remember that **a spreadsheet will execute formulas**: QC escapes
leading `= + - @` by default (`--no-protect-formulas` disables it), and
JSONL is the safer format for anything you might open elsewhere.

### Sending it

```bash
# Prefer a local tool that never uploads, when one exists
jq -s 'length' output/share.jsonl

# Uploading is explicit and yours to make
#   - attach only the sanitized file, never the manifest's source path
#   - prefer a provider with "no training on my data" terms
#   - delete the export when finished; it is still sensitive
shred -u output/share.jsonl 2>/dev/null || rm -f output/share.jsonl
```

## MCP

QC exposes 11 bounded, policy-enforced tools to AI agents over **stdio**
(local) or **Streamable HTTP** (remote).

```bash
qc mcp                                   # stdio (local AI clients)
qc mcp --transport http                  # requires auth config (see below)
```

Tools: `list_sources`, `list_datasets`, `get_dataset_schema`,
`profile_dataset`, `get_profile`, `preview_sanitized_data`,
`generate_synthetic_data`, `validate_synthetic_data`, `get_job_status`,
`cancel_job`, `get_policy_summary`.

**Not exposed in v1:** arbitrary SQL/Python, policy mutation, source
writes, credential management, raw-row dumps.

### Connecting an AI agent

**stdio (recommended).** No port, no token, nothing listening. Point the
client at the `qc` binary and tell it which project directory to run in —
QC resolves `qc.toml`, policies, and sources relative to that working
directory.

```json
{
  "mcpServers": {
    "qc": {
      "command": "/absolute/path/to/qc/.venv/bin/qc",
      "args": ["mcp"],
      "env": { "SQLITE_QC": "/absolute/path/to/your/project/demo.db" }
    }
  }
}
```

- **Claude Desktop** — Settings → Developer → Local MCP servers → add the
  block above. `cwd` is not universally supported, so prefer an absolute
  `command` path and pass the project location via `--root`:

  ```json
  {"command": "/abs/path/.venv/bin/qc", "args": ["mcp", "--root", "/abs/path/project"]}
  ```

- **Claude Code / Codex / Cursor** — same JSON, usually under a project
  `.mcp.json` or a global MCP config file.
- **fish/zsh users** — the client spawns the binary directly, so the
  `source .venv/bin/activate` step does *not* apply. Use the absolute path
  to `qc` as above.

Because the secrets live in the environment, the child process inherits
them only if you pass them in `env` — which means **the env values you
write into a client config file are secret material**. Keep that file
out of version control.

**Streamable HTTP (remote/shared).** Refuses to start unless every guard
holds, all verified:

```toml
[mcp.http]
enabled = true
host = "127.0.0.1"
port = 8765
require_auth = true
auth_token_ref = "env:QC_MCP_HTTP_TOKEN"   # must resolve to >= 16 chars
rate_limit_per_minute = 120
max_request_bytes = 1048576
```

```bash
export QC_MCP_HTTP_TOKEN='…'      # >= 16 chars, keep it in your keyring
qc mcp --transport http
```

Unknown keys are **rejected**, not ignored — a typo such as
`max_request_body_size` fails startup rather than silently leaving you
without the limit you thought you set.

Startup refuses, by design: `require_auth = false`, a missing
`auth_token_ref`, a token shorter than 16 characters, or `enabled = false`.

```bash
# no token -> 401 ; wrong token -> 401 ; correct token -> 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8765/mcp \
  -H "Authorization: Bearer $QC_MCP_HTTP_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
       "protocolVersion":"2025-06-18","capabilities":{},
       "clientInfo":{"name":"t","version":"0"}}}'
```

Keep it bound to `127.0.0.1` unless you intend remote access, and put TLS
plus an OIDC gateway in front of it — the bearer token is a v1 stopgap
(ADR-003).

### What an agent can and cannot reach

Every call is authorized against the active policy before any source read,
so an agent cannot widen its own access:

- unlisted sources/datasets → `POLICY_DENIED`;
- unlisted columns → redacted;
- tool arguments can only **narrow** budgets, never raise them;
- inline rows are capped by `max_inline_mcp_rows` (default 20);
- long-running tools return a `job_id` you can poll or `cancel_job`.

Source text is treated as **data, never instructions** — a malicious value
in a cell cannot make the agent run SQL or call a tool it wasn't given.

## Security defaults

- Read-only source access; no source mutation.
- **Deny by default:** raw rows, external egress, writes, model calls,
  and exports are denied until a policy explicitly allows them.
- No credentials in config or artifacts — secret *references* only
  (`env:`, `file:` chmod-600, `keyring:`, `cmd:`).
- No raw values in logs, errors, reports, or profile artifacts.
- Budgets are ceilings: tool arguments can only narrow them.
- Full model: [SECURITY.md](SECURITY.md).

## Documentation

| Doc | Contents |
|---|---|
| [SPEC.md](SPEC.md) | full specification |
| [SECURITY.md](SECURITY.md) | threat model, data handling, reporting |
| [docs/connectors.md](docs/connectors.md) | permissions + config per source |
| [docs/profiling.md](docs/profiling.md) | algorithms, sampling limits, confidence |
| [docs/policies.md](docs/policies.md) | policy schema, semantics, actions |
| [docs/synthetic-data.md](docs/synthetic-data.md) | strategies, fidelity, privacy limits |
| [docs/mcp.md](docs/mcp.md) | transports, tools, auth, client setup |
| [docs/operations.md](docs/operations.md) | deploy, log, upgrade, retain |
| [tools/make_demo_db.py](tools/make_demo_db.py) | deterministic sample SQLite DB for trying the pipeline |
| [docs/gaps.md](docs/gaps.md) | development gap assessment and resolutions |
| [docs/adr/](docs/adr/) | architecture decision records |

## Development

```bash
uv sync
uv run ruff check src tests && uv run ruff format src tests
uv run mypy
uv run pytest tests/unit tests/property tests/security -q      # fast
uv run pytest tests -q -m "not integration"                    # full non-live
# live connector contract tests (docker):
export QC_TEST_POSTGRES_DSN='postgresql://…'
export QC_TEST_MYSQL_DSN='mysql://…'
uv run pytest tests/contract -m integration
```

Exit codes are stable (SPEC 11.3): `0` ok, `2` bad arguments, `4` policy
denied, `5` budget/cancelled, `6` source error, `7` validation failure,
`9` destination write failure.

## Known limitations (v1)

- Synthetic data is **not anonymous** and not differentially private
  (ADR-005); reports say so explicitly.
- Relational generation across multiple datasets is declarative, not yet
  scheduled (see docs/synthetic-data.md).
- MaxCompute requires a pinned JDBC driver + Java 17+; live driver/dialect
  verification is a release gate (ADR-006).
- HTTP bearer auth is a stopgap in front of an OIDC gateway (ADR-003).

## Releasing

Publishing to PyPI uses **Trusted Publishing** (OIDC) — no API token is stored
in the repo. `.github/workflows/release.yml` builds the sdist + wheel and
uploads it when a GitHub Release is published, or on a manual run (which
defaults to TestPyPI for a dry run).

One-time setup on PyPI — <https://pypi.org/manage/account/publishing/> — with
project `querycleaner`, owner `adaptivemcp`, repository `qc`, workflow
`release.yml`. After that, publishing a GitHub Release (`vX.Y.Z`) uploads to
PyPI.

## License

Apache-2.0 — see [LICENSE](LICENSE).
