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
| **Protocol conformance evidence** | `tests/e2e/` proves behaviour over stdio, not protocol conformance. Point the official conformance harness at the server with an expected-failures baseline so adoption is incremental. | `tests/conformance/`, an `expected-failures` file, one CI job |
| **Published triage + resolution commitments** | The required labels exist, so time-to-first-label and critical-resolution are measurable; no window is claimed. Claim one, then hold it. | `CONTRIBUTING.md` (windows), the issue templates |
| **Connector compatibility matrix** | M7 asks for one. Today the contract suite proves shape, not supported server versions. | `docs/connectors.md` table + the live CI matrix |
| **Upgrade and migration policy** | Artifact and config schemas are versioned (§14) but nothing says what a version bump guarantees. | `docs/operations.md` section + a schema-version test |
| **Performance benchmarks** | M7 asks for them. Profiling cost per probe is measured; end-to-end latency and row throughput are not. | `tools/bench/`, results in `docs/operations.md` |
| **Security review note** | `SECURITY.md` states the stance; a review record of the threat model (sources, MCP surface, secrets, generated data) is missing. | `docs/security-review.md` |

Deliberately **not** scheduled for 1.0: anything in `gaps.md`'s "Deliberately out of
scope" table (differential privacy, source DDL, `qc://` resources, prompt templates).
Each is declined with a reason, and a concrete caller retires the deferral, not a date.

## 2. Carried over from `0.2.x`

| Item | Issue | State |
| --- | --- | --- |
| Live MaxCompute driver/dialect verification | #21 | Release gate: the bridge protocol is tested, live confirmation needs a project |
| Country-code and postal-code inference | #37 | Deferred with the evidence they would need; shape alone is ambiguous |

## 3. Next after the above

Ordered by evidence, not ambition — each is only worth doing if the thing before it
shows a gap:

1. **Sensitivity precision on free text.** Detectors are regex/checksum based; a
   measured false-positive rate on a labelled corpus would say whether that is enough.
2. **Sampling representativeness reporting.** The spec requires disclosing that a sample
   cannot establish absence; the report states it, but nothing measures how far off the
   sample was.
3. **Relational generation scheduling** (declarative only today) — the first caller that
   needs a multi-table order retires its "out of scope" entry.

## How this list changes

At each milestone and before any release (the cadence in `gaps.md`): re-read this page,
move what shipped, and delete what no longer matters. A roadmap that accumulates is a
wish list; this one is meant to shrink.
