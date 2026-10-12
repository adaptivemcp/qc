# QueryCleaner/QC

**Working name — rename before public release** (ADR-001).

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

## MCP

```bash
qc mcp                                   # stdio (local AI clients)
qc mcp --transport http                  # requires auth config (see docs/mcp.md)
```

Tools: `list_sources`, `list_datasets`, `get_dataset_schema`,
`profile_dataset`, `get_profile`, `preview_sanitized_data`,
`generate_synthetic_data`, `validate_synthetic_data`, `get_job_status`,
`cancel_job`, `get_policy_summary`.

**Not exposed in v1:** arbitrary SQL/Python, policy mutation, source
writes, credential management, raw-row dumps.

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

## License

Apache-2.0 (pending dependency and organizational review — ADR-001).
