# Conformance evidence

The 1.0 roadmap asks for protocol conformance evidence. Today the evidence is
*behavioural* — `tests/e2e/` drives the real server over stdio and `tests/contract/` pins
the tool contracts — and neither is protocol conformance. This page is the adoption plan
for the official harness, including the one thing that genuinely blocks it today.

Harness: [`@modelcontextprotocol/conformance`](https://github.com/modelcontextprotocol/conformance).

## Why it is not one command

QC's HTTP transport **requires authentication by design** (`SPEC 12.1`: remote mode must
require a token; the server refuses to start with `require_auth = false`). The harness's
server mode takes only `--url` — it has no header or token option:

```
$ npx @modelcontextprotocol/conformance server --help
  --url <url>                 URL of the server to test
  --scenario <scenario>       Scenario to test
  --suite <suite>             "active" (default), "all", "pending"
  --expected-failures <path>  Path to YAML file listing expected failures
  -o, --output-dir <path>     Save results to this directory
  --spec-version <version>    Filter scenarios by spec version
```

So the adoption path is: **a loopback token-injecting proxy** in front of QC, and the
harness talks to the proxy. That keeps the security stance honest — QC still refuses
unauthenticated HTTP — instead of adding a "conformance mode" that disables the guarantee
being reviewed. The proxy is `tools/conformance/bearer_proxy.py`.

Stdio is not an option for server mode: the harness connects over HTTP. QC's stdio
transport stays the recommended local mode and is covered by `tests/e2e/`.

## The baseline

`tools/conformance/conformance-baseline.yml` is the harness's expected-failures file:

```yaml
server:
  - tools-call-with-progress            # whole scenario may fail
  - server-stateless:sep-2575-server-implements-discover   # one check may fail
client: []
```

It is empty on purpose. The first run *is* how it gets content, and every entry after
that is a reviewed decision. The four rules the harness applies:

| Scenario result | In baseline | Outcome |
| --- | --- | --- |
| fails | yes | exit 0 — expected |
| fails | no | exit 1 — regression |
| passes | yes | exit 1 — stale entry, remove it |
| passes | no | exit 0 |

Entries naming a single check (`<scenario>:<check-id>`) are preferred over whole
scenarios: baselining a scenario to excuse one check stops enforcing its siblings.

## The token proxy

`tools/conformance/bearer_proxy.py` — ASGI, loopback-only, token from the environment:

- refuses any `--listen` host that is not loopback (it forwards a bearer token, so
  `0.0.0.0` would expose an unauthenticated surface);
- reads the token from `QC_MCP_HTTP_TOKEN` (a name you choose with `--token-env`) and never
  from argv, so it does not appear in the process list;
- replaces any caller-supplied `Authorization` header rather than forwarding it;
- strips hop-by-hop headers and streams response chunks, because Streamable HTTP may hold
  an SSE stream open and buffering would turn a working server into a timeout;
- logs no request paths (they can name datasets).

## First run — 2026-11-06

Through the proxy, `conformance@0.1.11 server --url http://127.0.0.1:3001/mcp`:

```
Running active suite (30 scenarios) against http://127.0.0.1:3001/mcp
Total: 11 passed, 21 failed
```

Passing: `server-initialize`, `ping`, `tools-list`, `tools-call-simple-text`,
`tools-call-error`, `resources-list`, `prompts-list`, and `dns-rebinding-protection`
(2 checks). The DNS-rebinding pass is worth noting: it is a security check the server
passes on its own, without configuration.

The 19 baseline entries fall into three honest groups — resources/prompts (declined by
SPEC 12.5), tool result shapes (text-only, which is legal MCP), and client-side
capabilities QC has no reason to use (sampling, elicitation, logging, progress).

**The first run also found a defect, now fixed (#46).** `initialize` advertised `prompts`,
`resources`, `completion` and `logging` that the server does not implement — FastMCP
registers those handlers unconditionally, and the SDK derives the capability block from
them. The server is now built with an explicit set:

```json
"capabilities": { "resources": { "subscribe": false, "listChanged": false },
                  "tools": { "listChanged": false } }
```

`resources` is advertised because `dev.adaptivemcp://tools-metadata` really is listable;
prompts, logging and completions are absent because they are not implemented (prompts are
declined by SPEC 12.5). A test compares the advertised set with what `resources/list` and
`tools/list` return, in both directions, so the block cannot drift again.

With the advertisement honest, `completion-complete` and `logging-set-level` are baselined
as *accepted omissions* — MCP logging and completions are optional, and QC's tools never
ask the client for anything. A full run now has **zero unexpected failures**.

## Adoption phases

**Phase 1 — advisory (done).** The job ran, uploaded `results/`, and never failed the
build while the baseline was being populated.

**Phase 2 — blocking (current).** #46 is fixed, the baseline holds 21 reviewed entries and
a run has zero unexpected failures, so `continue-on-error` is gone. From here: a new
failure fails CI, and a stale baseline entry fails CI (the harness exits 1 when a baselined
scenario starts passing), so the baseline cannot silently shrink. A harness upgrade that
adds scenarios will fail the job — that is the signal to review the new scenario, not to
re-pin.

## The CI job

Wired in `.github/workflows/ci.yml` as the `conformance` job, **advisory**
(`continue-on-error`) until #46 lands. It stands the server up behind the proxy and runs
the harness:

```yaml
  conformance:
    runs-on: ubuntu-latest
    needs: checks
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.12"
      - run: uv sync --locked
      - name: Serve QC over HTTP behind the token proxy
        run: |
          set -euo pipefail
          uv run qc init --root "$RUNNER_TEMP/qc-project"
          # enable [mcp.http] and point it at env:QC_MCP_HTTP_TOKEN (see qc.toml notes)
          export QC_MCP_HTTP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
          echo "QC_MCP_HTTP_TOKEN=$QC_MCP_HTTP_TOKEN" >> "$GITHUB_ENV"
          uv run qc mcp --transport http --root "$RUNNER_TEMP/qc-project" &
          uv run python tools/conformance/bearer_proxy.py \
            --listen 127.0.0.1:3001 --upstream http://127.0.0.1:8765 &
          timeout 90 bash -c 'until curl -sf -X POST http://127.0.0.1:3001/mcp ... >/dev/null; do sleep 1; done'
      - name: Conformance (advisory)
        continue-on-error: true
        run: |
          npx -y @modelcontextprotocol/conformance@0.1.11 server \
            --url http://127.0.0.1:3001/mcp \
            --expected-failures tools/conformance/conformance-baseline.yml \
            --output-dir conformance-results
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: conformance-results
          path: conformance-results
```

The workflow itself is the source of truth for the exact `qc.toml` patch; the token is
generated per run and never stored.

## What this buys, and what it does not

- **Buys**: protocol evidence per scenario, a baseline that cannot go stale silently, and
  a CI job that fails on regressions once phase 2 lands.
- **Does not buy**: an SDK tier claim. QC is a server, not an SDK, and
  [`sdk-tiers.md`](sdk-tiers.md) already says so. `tier-check` is for SDK repositories;
  the server suite is the part that applies here.
- **Does not** make the behavioural tests redundant: conformance covers the wire, the
  contract suite covers QC's own tool semantics.

## Status

| Step | State |
| --- | --- |
| Baseline file with the harness's schema | ✅ `tools/conformance/conformance-baseline.yml` |
| Plan, invocation and CI snippet | ✅ this page |
| Loopback bearer proxy + tests | ✅ `tools/conformance/bearer_proxy.py` |
| First real run through the proxy | ✅ 11 passed / 21 failed, 2026-11-06 |
| Fix advertised-but-unimplemented capabilities | ✅ [#46](https://github.com/adaptivemcp/qc/issues/46) fixed |
| Advisory CI job | ✅ ran while the baseline was populated |
| Blocking CI job | ✅ `continue-on-error` removed; 0 unexpected failures |

Entry point for a local run once the proxy exists:

```bash
npx -y @modelcontextprotocol/conformance@0.1.11 list
npx -y @modelcontextprotocol/conformance@0.1.11 server \
  --url http://127.0.0.1:3001/mcp \
  --expected-failures tools/conformance/conformance-baseline.yml
```
