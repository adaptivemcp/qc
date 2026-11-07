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
  --max-runtime 60 --max-cost 5.0 --sample-size 2000
```

The effective budget is `min(user value, policy budget, server ceiling)`.
Zero is a real limit ("nothing allowed"), not "unset". Exceeding the
budget mid-run does not corrupt the profile: completed evidence is kept,
blocked probes are listed under `incomplete_checks`, and the run fails
with exit code 5 only if *no* evidence could be collected.

`--allow-full-scan` is required for probes that must read every row, and
is denied unless the policy sets `budget.allow_full_scan: true`.

## Filtered profiles with `--where`

Restrict the profiled population with a **typed predicate** — never raw
SQL (SPEC 11.2). Forms:

```
--where column=value          equality
--where column:op:value       op ∈ {=, !=, <, <=, >, >=, like, in}
--where region:in:US,EU       membership
--where age:>=:18             ordering
```

Values are coerced to int/float/bool/null where unambiguous; string
values stay strings. Multiple `--where` flags combine with AND. The
predicate is validated, its column quoted, and its values bound as query
parameters in every aggregate and sample — SQL fragments (`;`, quotes,
comments) are rejected with exit code 2. A filtered profile's row count
and all aggregates are computed over the filtered set.

```bash
qc profile run analytics public.orders --where region=US --where age:>=:18
```

## Cancellation

Profiling is cancellable (SPEC 4.3, 6.1, 12.6):

- **CLI:** `Ctrl-C` cancels cooperatively; the connector closes its
  cursor/connection and the profile exits with code 5. Completed evidence
  is discarded and the job is marked `cancelled`.
- **MCP:** `cancel_job` signals the running job; it reports `cancelling`
  when the work was still in flight, else the terminal status.
- Cancellation is checked before each probe, per column, and between
  fetched/sampled batches. It is never swallowed by error handling.


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

## Country and postal codes (qc#37)

Both are **ambiguity problems**, so neither is inferred from shape alone:

- `US` is also a state code, a status code and a unit; `90210` is also a number, and
  `00123` must stay a string (SPEC 7.6).
- Inference therefore needs a **name hint** (`country`, `country_code`, `iso_country`;
  `postal`, `postcode`, `zip`, `zip_code`, `pincode`) — or a declared type carrying the
  same word. With the hint:
  - every value must be in the ISO 3166-1 alpha-2 set for `country_code` (a closed code
    set is the corroboration a name buys you);
  - every value must match one of the known postal shapes for `postal_code`, and the
    result is `low` confidence with `locale_hint: postal_shape_only`;
  - both are reported `ambiguous` with a note naming the alternative reading.

Without the hint the column keeps its shape-based type (`categorical_string` for `US`,
`integer`/`string` for a five-digit column), which is the honest answer. Generation
preserves the shape: `country_code` emits a real code and `postal_code` emits a
five-digit form.

```python
infer_logical_type(["US", "CA", "GB"], column_hint="country_code")  # country_code, ambiguous
infer_logical_type(["US", "CA", "GB"])                              # categorical_string
```

## Sensitivity-detector accuracy

The detectors are layered (name → format → entropy → org hooks) and their error rate was
previously unknown. `tools/bench/sensitivity_bench.py` scores them on 23 labelled columns:
13 sensitive (some with bland names, so the value layer is exercised) and 10 controls,
three of which carry a sensitive *word* in the name on purpose.

```bash
uv run python tools/bench/sensitivity_bench.py
```

Recorded 2026-11-07, **35 columns** — realistic formats, embedded identifiers and
near-miss controls:

| label | expected | predicted | TP | precision | recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| address | 2 | 2 | 2 | 1.00 | 1.00 |
| credential | 4 | 4 | 4 | 1.00 | 1.00 |
| date_of_birth | 1 | 1 | 1 | 1.00 | 1.00 |
| email | 5 | 5 | 5 | 1.00 | 1.00 |
| financial | 1 | 1 | 1 | 1.00 | 1.00 |
| free_text | 3 | 3 | 3 | 1.00 | 1.00 |
| government_id | 1 | 1 | 1 | 1.00 | 1.00 |
| health | 1 | 1 | 1 | 1.00 | 1.00 |
| ip_address | 1 | 1 | 1 | 1.00 | 1.00 |
| name | 1 | 1 | 1 | 1.00 | 1.00 |
| phone | 4 | 4 | 4 | 1.00 | 1.00 |

What the fixture covers, and what the numbers mean:

- **Realistic sensitive shapes**: masked addresses (`u***@example.com`, which the format
  detector misses, so the name layer carries them), E.164 (`+14155550123`), spaced
  international numbers (`+44 20 7946 0958`), JWTs, base32 webhook secrets, and free text
  with a contact address and phone inside it.
- **Embedded identifiers are extracted**, not just labelled: a free-text column now reports
  `free_text` *and* `email` / `phone` with `evidence: embedded:email_in_text` /
  `embedded:phone_in_text`, because those are what make the disclosure risk concrete.
- **Noise guards for that extraction**, measured by two controls: eight bare digits in prose
  (`order 12345678`) are an order number, not a phone, and a schedule
  (`2025-05-01T12:00:00Z`, `03:00 UTC`) is not a phone either — the first version of the
  extraction did flag the schedule, which is why date/time shapes are excluded and a
  separator/country code is required.
- **Near-miss controls**: SKUs, a realistic 40-hex **git SHA**, UUID-shaped request ids and
  ISO timestamps — the opposite error, a detector that flags anything structured.
- **Four defects the fixture has now caught, all fixed:**
  1. `123-45-6789` matched the loose phone pattern → SSN columns reported as *both*
     `government_id` and `phone`;
  2. a sensitive word was treated as proof → `email_verified` (boolean), `token_count`
     (count) and `phone_model` (product names) were flagged (precision 0.75/0.67/0.50);
  3. a **git SHA** was flagged as a credential by the hex/entropy layer;
  4. the first embedded extraction read a **maintenance schedule** as a phone column.
- **Known limitation, measured**: an embedded phone with no separator or country code
  (`4155550100` inside prose) is not extracted. That is the deliberate trade behind the
  noise guard, and it is the next thing to measure rather than assume.
- **Recall is 1.00 on this fixture.** Treat it as "the detectors find what they are
  designed to find", not a real-world rate: the columns are curated to be separable, and
  the value is in the regression baseline and the defects it produces, not in the perfect
  score.

## Inspecting a profile

```bash
qc profile show <profile-id>            # human summary
qc profile show <profile-id> --json     # full artifact
qc profile diff <a> <b>                 # type/null-rate/row changes
```

Look for: `confidence_summary`, `budget_usage`, `incomplete_checks`,
column `notes` (inference reasons), and `warnings`.
