# Roadmap

What ships next, and what would make `1.0.0` honest. `SPEC.md` is the source of truth
for behaviour; [`gaps.md`](gaps.md) is the point-in-time review of what was missing and
how it was fixed; this page is the forward-looking companion it deliberately is not.

Status: `0.2.1`, pre-stable by definition (a stable release is `1.0.0`+ without a
pre-release identifier). SPEC §17 milestones M0–M7 are delivered; what remains is the
work M7's exit criteria name plus the deferrals recorded in `gaps.md`.

## 1. Before `1.0.0`

| Item | Why it blocks 1.0 | Where it lands |
| --- | --- | --- |
| **Protocol conformance evidence** | **Done 2026-11-06.** `conformance@0.1.11` runs in CI against the real HTTP server behind `tools/conformance/bearer_proxy.py`, blocking, with a reviewed 21-entry baseline and zero unexpected failures. The run found and fixed one defect: `initialize` advertised prompts/logging/completions the server does not implement (#46). Plan and evidence: [`conformance.md`](conformance.md). | `docs/conformance.md`, `tests/unit/test_mcp_capabilities.py` |
| **Published triage + resolution commitments** | **Proposal filed, awaiting owner approval: [#44](https://github.com/adaptivemcp/qc/issues/44)** — 2 business days to first label, 1 to acknowledge a P0, 7 to fix one, 30 for a P1, with the measurement method and a "reported, not hidden" clause. Nothing is published until the numbers are approved. | `CONTRIBUTING.md` after approval |
| **Connector compatibility matrix** | **Done 2026-11-06.** `connectors.md#server-compatibility` names what the live CI job actually runs (PostgreSQL 16, MySQL 8.4), what is protocol-only (MaxCompute, #21) or mocked (Sheets), and what is deliberately *not* claimed. `tests/unit/test_connector_matrix.py` parses `ci.yml` and fails if the table and the workflow drift apart. | `docs/connectors.md`, `tests/unit/test_connector_matrix.py` |
| **Upgrade and migration policy** | **Done 2026-11-06**, extended to generation reports and `state.db` migrations 2026-11-07. `operations.md` states the one rule (same reads / older migrates / newer refuses), the per-artifact version table, and what patch/minor/major guarantee. Implemented once in `qc.artifacts.versions`, with tests for the profile, plan and `state.db` loaders. | `docs/operations.md`, `tests/unit/test_artifact_versions.py` |
| **Performance benchmarks** | **Done 2026-11-06.** `tools/bench/profile_bench.py` (30 queries flat across 10k→1M rows, 0.94s→6.89s, <5 MB peak) and `tools/bench/throughput_bench.py` (sanitize ~34–36k rows/s, generate ~1.5k rows/s), both recorded in `operations.md` with method and host. | `tools/bench/`, `docs/operations.md#measured-baselines` |
| **OIDC bearer verification** | **Done 2026-11-07.** `mode = "oidc"` validates a JWT against the provider's JWKS with signature, `iss`/`aud`/`exp`/scope checks and audience binding (`validate_token_resource=True`), replacing "an OIDC gateway is required" with "either the static bearer or a verified token". Evidence: `operations.md#deployable-today--oidc-gateway-in-front` and `tests/unit/test_mcp_oidc.py`. | `src/qc/mcp/oidc.py`, `tests/unit/test_mcp_oidc.py` |
| **Security review record** | **Done 2026-11-06.** `security-review.md` maps 17 threats to the symbol that enforces each and the test that proves it, lists six accepted risks with their triggers, and records the reviewed version. `tests/security/test_security_review.py` parses the table and fails when a cited symbol, test or version goes stale. | `docs/security-review.md`, `tests/security/test_security_review.py` |

Deliberately **not** scheduled for 1.0: anything in `gaps.md`'s "Deliberately out of
scope" table (differential privacy, source DDL, `qc://` resources, prompt templates).
Each is declined with a reason, and a concrete caller retires the deferral, not a date.

## 2. Carried over from `0.2.x`

| Item | Issue | State |
| --- | --- | --- |
| Live MaxCompute driver/dialect verification | #21 | Release gate: the bridge protocol is tested, live confirmation needs a project |
| ~~Country-code and postal-code inference~~ | #37 | **Done 2026-11-06** — hint-gated, ambiguity reported, measured sensitivity/precision in `profiling.md` |

## 3. Next after the above

Ordered by evidence, not ambition — each is only worth doing if the thing before it
shows a gap:

1. ~~**Sensitivity precision on free text.**~~ **Done 2026-11-07** — a 40-column labelled
   fixture measuring precision/recall, which has produced five fixed defects
   (`profiling.md#sensitivity-detector-accuracy`).
2. ~~**Sampling representativeness reporting.**~~ **Done 2026-11-07** — every profile
   artifact states sampled rows, total, coverage, method, confidence and the limitation
   ("a sample cannot establish the absence of a rare value"). Deliberately coverage
   disclosure, not a confidence interval
   (`profiling.md#sampling-representativeness`).
3. **Relational generation scheduling** (declarative only today) — the first caller that
   needs a multi-table order retires its "out of scope" entry.

## How this list changes

At each milestone and before any release (the cadence in `gaps.md`): re-read this page,
move what shipped, and delete what no longer matters. A roadmap that accumulates is a
wish list; this one is meant to shrink.
