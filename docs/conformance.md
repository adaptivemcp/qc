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
being reviewed. The proxy is the first task below; it is not written yet, and this page
does not pretend otherwise.

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

## Adoption phases

**Phase 1 — advisory (this plan).** The job runs, uploads `results/`, and never fails the
build. The report is how the baseline gets populated. Do not merge baseline entries
guessed from a red job log.

**Phase 2 — blocking.** Once the baseline contains only reviewed entries, drop
`continue-on-error`. From then on: a new failure fails CI, and a stale entry fails CI so it
cannot silently shrink.

## Probe: the proxy

`tools/conformance/bearer_proxy.py` (to be written as the first task):

- listen on `127.0.0.1:<port>`, forward to QC's HTTP port;
- add `Authorization: Bearer <token>` to every request;
- stream responses unchanged (Streamable HTTP may hold an SSE stream open, so the proxy
  must not buffer or rewrite bodies);
- no TLS, no logging of bodies, no listening on `0.0.0.0`;
- accept the token from the environment, never argv.

Acceptance: `curl -sf http://127.0.0.1:<port>/mcp` reaches QC, and the harness's
`server-initialize` scenario passes through it.

## The CI job

Documented here, **not wired yet** — it would be red until the baseline exists and the
proxy lands. It goes next to `integration-db` in `.github/workflows/ci.yml`:

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
      - name: Serve QC over HTTP, behind a token-injecting proxy
        run: |
          set -euo pipefail
          export QC_MCP_HTTP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
          uv run qc mcp --transport http &
          uv run python tools/conformance/bearer_proxy.py \
            --listen 3001 --upstream 8765 --token "$QC_MCP_HTTP_TOKEN" &
          timeout 30 bash -c 'until curl -sf http://127.0.0.1:3001/mcp >/dev/null; do sleep 0.5; done'
      - name: Conformance (advisory until the baseline is populated)
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

The project needs `[mcp.http] enabled = true`, `require_auth = true` and
`auth_token_ref = "env:QC_MCP_HTTP_TOKEN"` in the CI `qc.toml`; the token is generated per
run and never stored.

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
| Loopback bearer proxy | ☐ first task |
| Advisory CI job | ☐ after the proxy |
| Blocking CI job | ☐ after the baseline is reviewed |

Entry point for a local run once the proxy exists:

```bash
npx -y @modelcontextprotocol/conformance@0.1.11 list
npx -y @modelcontextprotocol/conformance@0.1.11 server \
  --url http://127.0.0.1:3001/mcp \
  --expected-failures tools/conformance/conformance-baseline.yml
```
