# Profiling

QC learns data behavior through **bounded, inspectable evidence**. This
document covers the adaptive engine's algorithms, budgets, sampling
limitations, and how to read confidence labels (SPEC section 7).

## Pipeline stages

1. **Preflight** — policy check (`DATASET_READ`), dataset allowlist,
   budget resolution (user budget ⨀ server ceiling), full-scan gate.
2. **Metadata** — schema, constraints, row estimates, provider metadata.
3. **Cheap aggregates** — exact row count, per-column exact null counts,
   distinct counts, min/max, length stats, top-k categories.
4. **Representative sampling** — bounded rows via stable-key ordering
   (deterministic) or connector-provided sampling.
5. **Pattern inference** — logical type, format skeletons, category
   distributions, identifier/leading-zero handling, date ambiguity.
6. **Edge-case probes** — sentinels, rare patterns, malformed values.
   Full-table scans run **only** with explicit `--allow-full-scan` +
   policy approval; the skip is recorded as a probe decision.
7. **Relationship probes** — declared FKs first; inferred key candidates
   from sampled uniqueness/nullability; functional-dependency hints.
8. **Sensitivity detection** — layered name/format/entropy detectors.
9. **Confidence & stopping** — budget gates and utility thresholds.
10. **Finalization** — evidence summaries, budget usage, uncertainty,
    profile version saved as JSON.

## Query planner

Each candidate probe carries expected information gain, estimated cost,
privacy risk, required capabilities, and dependencies. Probes are ranked
by:

```
priority = expected_information_gain * uncertainty_weight / max(estimated_cost, cost_floor)
```

This is a **heuristic, not a statistical guarantee**. Privacy and budget
gates run *before* ranking. Every selected and skipped probe is recorded
(`ProbeDecision`) so you can see why profiling stopped. Stopping occurs
when any budget is exhausted, remaining probes fall below the utility
threshold, confidence targets are met, the user cancels, or repeated
transient failures occur.

## Budgets

Set per-run with `qc profile run`:

```bash
qc profile run analytics public.orders \
  --max-queries 40 --max-rows 50000 --max-bytes 8388608 \
  --max-runtime 60 --sample-size 2000
```

The effective budget is `min(user value, policy budget, server ceiling)`.
Zero is a real limit ("nothing allowed"), not "unset". Exceeding the
budget mid-run does not corrupt the profile: completed evidence is kept,
blocked probes are listed under `incomplete_checks`, and the run fails
with exit code 5 only if *no* evidence could be collected.

`--allow-full-scan` is required for probes that must read every row, and
is denied unless the policy sets `budget.allow_full_scan: true`.

## Sampling and its limitations

- **Stable-key ordered sampling** — deterministic, cheap, but biased if
  the sort key correlates with content.
- **Random sampling** (`ORDER BY random()`) — uniform but can be
  expensive on large tables; opt-in.
- **Stratified/partition-aware** — capability-dependent.
- **Time-window** — via user predicates.

> A sample **cannot establish the absence of rare events**. Every
> profile records its sampling method and marks sample-derived numbers
> as `estimated`. If the source cannot guarantee representativeness,
> the profile says so.

## Null and falsy analysis

For every column, QC keeps these **distinct** (never collapsed):

SQL `NULL` · empty string `""` · whitespace-only · numeric `0` ·
boolean `false` · strings `"0"`/`"false"`/`"null"`/`"None"` ·
spreadsheet missing cell · formula cell with empty result ·
NaN/±Infinity · sentinel *candidates* (`-1`, `9999`, `N/A`, `unknown`).

Sentinels are **not** treated as missing without evidence or explicit
policy; they appear as hashed keys with counts. Raw and normalized
interpretations are both reported. Exact aggregate null counts (when the
connector supports them) override sample-derived counts and are marked
`exact`.

## Type inference

Logical type is inferred *independently* of the declared source type and
**ambiguity is retained**:

- `"00123"` never becomes an integer — leading zeros are meaningful;
  it becomes an identifier-like string.
- `03/04/2025` records day/month ambiguity (`locale_hint`) instead of
  guessing; confidence drops to `low`.
- Integers beyond 18+ digits stay strings (precision safety).
- Decimals are handled with `Decimal`, never float round-trips.
- Long, highly-distinct strings → free text; low-cardinality strings →
  categorical; format detectors upgrade to `email`, `uuid`, `url`,
  `ip_address`, `phone` only with sufficient coverage.

## Distributions and disclosure controls

Exact counts where feasible; documented approximations otherwise
(algorithm + error characteristics recorded in the artifact). Collected
summaries: null/falsy rates, distinct counts, top-k categories with
**rare-group suppression**, length/numeric histograms, quantiles,
min/max, pattern frequencies, duplicate candidates, cross-column
dependency candidates, referential-integrity estimates.

Top-k category values are suppressed below a minimum group size
(scaled down for tiny fixtures; sensitivity-flagged columns keep the
strict minimum) so small groups cannot identify individuals. MCP output
additionally strips category values unless `profile_disclosure: full`.

## Confidence labels

`high` / `medium` / `low` / `unknown` — **calibrated evidence strength,
not a mathematical probability.** Rough mapping (type inference):

| Condition | Label |
|---|---|
| ≥99% agreement, ≥20 values | high |
| ≥95% agreement, ≥10 values | medium |
| ≥80% agreement, ≥5 values | low (+ ambiguous) |
| less evidence | unknown |

Each detector documents its own mapping. Every profile states: what was
measured exactly, what was estimated, what was inferred, what was not
checked, and which budgets limited coverage (`incomplete_checks`,
`warnings`, `budget_usage`).

## Relationships

Declared constraints are trusted first. Inferred keys require sampled
uniqueness + nullability and are marked `medium`/`low` confidence with
`verified: false`. FK candidates need compatible types and sampled
overlap. **Inferred relationships are hypotheses until verified**;
correlation is not causation.

## Inspecting a profile

```bash
qc profile show <profile-id>            # human summary
qc profile show <profile-id> --json     # full artifact
qc profile diff <a> <b>                 # type/null-rate/row changes
```

Look for: `confidence_summary`, `budget_usage`, `incomplete_checks`,
column `notes` (inference reasons), and `warnings`.
