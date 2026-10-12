# Synthetic data

QC generates records that preserve selected statistical and structural
properties **without copying source rows**, then validates the result
against the learned profile (SPEC section 9).

> **Synthetic ≠ anonymous.** Empirical-distribution synthesis can leak
> information (membership inference, rare-combination replay). QC does
> **not** claim differential privacy (ADR-005). Every report carries this
> warning. Use policies, rare-category suppression, and review — not the
> word "anonymous" — to decide what is safe to share.

## Workflow

```bash
qc generate plan <profile-id> --rows 1000 --seed 42 --locale en_US
qc generate run reports/plan_....plan.json --output output/synth.jsonl \
  --report reports/gen.report.json [--strict] [--allow-fallbacks]
qc generate validate output/synth.jsonl --profile <profile-id>
```

`generate run` fails with exit code 7 if any **hard** constraint cannot
be satisfied; `--strict` also fails on soft distribution misses.

## Strategy registry (preference order)

1. **Explicit user strategy** (`--strategy` overrides in the plan)
2. **Declared schema constraints** (uniqueness, keys)
3. **Strongly inferred format generators** — email (`@example.invalid`),
   UUID, shape-preserving identifiers
4. **Semantic Faker providers** — names, addresses (locale from plan;
   inferred only when confidence is sufficient)
5. **Learned categorical/distribution samplers** — observed frequencies,
   numeric ranges, temporal ranges, boolean rates
6. **Generic type-aware generators** — string lengths, JSON shapes
7. **Fail closed** — `unsupported-field` error unless
   `--allow-fallbacks` is passed (recorded as a strategy fallback)

Every strategy implements `can_generate / plan / generate`. A unique
column never selects the bounded categorical pool (it cannot satisfy
uniqueness); uniqueness is enforced with bounded retries and then a
hard failure — **hard constraints are never silently weakened** (SPEC 9.6).

## Reproducibility

Same profile + same seed + same versions ⇒ byte-identical output
(`--seed`/plan `seed`, `Faker.seed`, and `random.Random(seed)` are all
derived from the plan). The generation report records `seed`,
`strategy_version`, and `tool_version`. Source-dependent variation can
still occur if the *profile itself* changes between runs.

## Fidelity validation (SPEC 9.5)

Metrics distinguish **hard constraints** from **soft tolerances**:

| Metric | Kind | Default tolerance |
|---|---|---|
| `type_validity` | hard | ≥0.95 of values type-correct |
| `uniqueness` | hard (when profile says exact-unique) | ≥0.999 |
| `referential_integrity` | hard | 0 violations |
| `null_rate` drift | soft | ±0.05 |
| numeric `range` out-of-bounds rate | soft | ≤0.10 |
| `categorical_tvd` (total variation, rare groups folded into `__other__`) | soft | ≤0.15 |
| `length_distance`, `format_validity` | soft/hard per type | see `DEFAULT_TOLERANCES` |

Report fields: `constraint_violations` (hard), `approximated_properties`
(soft), `preserved_properties`, `strategy_fallbacks`, `warnings`,
`runtime`, `seed`. Reports never contain raw source values.

## Null and falsy behavior

Observed null rates are reproduced with the seeded RNG (SPEC 9.2) and
validated within tolerance. Empty-string, whitespace-only, zero, and
false rates are tracked separately in the profile; categorical comparison
keeps `__null__` / `__false__` / `__zero__` / `__empty__` distinct.

## Privacy controls

- Rare categories below `rare_category_min` are suppressed at profile
  time; generated distributions include an `__other__` bucket rather
  than rare individuals.
- Whole source rows are never used as generation shortcuts.
- Free text defaults to redaction (`[synthetic free text]`); values are
  never sent to an LLM.
- Emails use the reserved `example.invalid` domain; phone-like values
  use fictional `555` ranges and are reported as *not guaranteed
  unassigned*.
- Identifiers are format-compatible but **not** the original values;
  tokenization (if used) is keyed HMAC, never a plain hash.
- Optional privacy review: flag near-duplicates and rare combinations
  (bounded by profile evidence).

## Relational consistency

- Primary-key uniqueness enforced; foreign keys reference generated
  parent keys when the relationship is declared/verified.
- Temporal ordering (e.g. `created_at <= updated_at`) and numeric
  constraints (`quantity >= 0`) supported via typed constraint specs —
  no arbitrary code execution in profiles or policies (SPEC 9.4).
- v1 generates single-dataset records; multi-dataset dependency ordering
  is declarative (recorded as a report warning) until the relational
  scheduler lands.
