# MCP SDK tiers: where we stand

[The MCP SDK tiering system](https://modelcontextprotocol.io/community/sdk-tiers)
grades SDKs on conformance, feature tracking, triage, releases, documentation,
dependency policy and roadmap, with three tiers and measurable thresholds.

**We are not an SDK.** QC is an MCP **server** (stdio and Streamable HTTP) plus a CLI;
it consumes the Python `mcp` SDK (`>=1.2,<2`) rather than publishing one. There is no
tier to apply for. The checklist still applies to the parts we do implement — the
server surface is protocol code, and the project-hygiene requirements are just good
practice — so this page grades us against it honestly.

| Requirement | Status | Where we stand |
| --- | --- | --- |
| **Conformance tests** | ✗ | We do not run [`@modelcontextprotocol/conformance`](https://github.com/modelcontextprotocol/conformance) (it targets the protocol, not a language). Our coverage is behavioural: `tests/e2e/test_mcp_stdio.py` drives the real server over stdio, and `tests/contract/` pins the tool contracts. Neither is protocol conformance. |
| **New protocol features** | ◐ | `mcp>=1.2,<2` is a wide range with a real ceiling, and `SPEC.md` fixes the behaviour we implement. There is no published timeline for adopting a protocol release, and no mapping from `mcp` SDK versions to spec versions we support. |
| **Issue triage** | ◐ | `bug`, `enhancement`, `question`, `good first issue` and `help wanted` exist. The required status labels `needs confirmation`, `needs repro` and `ready for work` do not, and time-to-first-label is not measured. |
| **Critical bug resolution** | ✗ | Priorities are `priority:high` / `priority:medium` / `priority:low`. Without a `P0` label the tier metric (from `P0` to close) cannot be computed. |
| **Stable release** | ◐ | `0.2.1`. The tiers define stable as `1.0.0`+ without pre-release identifiers, so this is pre-stable by that definition — appropriate for a project still implementing against `SPEC.md`. |
| **Documentation** | ✓ | `README.md`, `SPEC.md` (62 KB, the source of truth), `SECURITY.md`, and `docs/` (ADR, connectors, mcp, policies, operations, profiling, synthetic data, gaps). The gap document even states its own review cadence. |
| **Dependency policy** | ◐ | Dependencies use bounded ranges (`>=x,<y`), which is the right shape, and `pyproject.toml` is the single source. There is no published update cadence or advisory-response policy. |
| **Roadmap** | ✗ | `docs/gaps.md` is explicitly **point-in-time** ("not a live tracker"), so it is a review record rather than a forward-looking roadmap. There is no document saying what ships next. |
| **Conformance score / advancement** | N/A | Only applies to SDKs applying for a tier. |

## What we would change first

1. **Adopt the required labels** (`needs confirmation`, `needs repro`,
   `ready for work`, `P0`–`P3`). Minutes of work, and the prerequisite for the two
   measurable requirements.
2. **Add a short roadmap.** `docs/gaps.md` already knows how to be honest; a
   forward-looking companion with the next few milestones (and the spec sections they
   complete) is the missing half.
3. **Run the conformance suite against the server.** `tests/e2e/` already speaks MCP;
   pointing the official harness at the stdio server would turn behavioural confidence
   into protocol evidence, and the expected-failures baseline makes adopting it
   incremental.
