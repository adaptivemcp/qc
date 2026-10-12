# ADR-005: Differential privacy out of scope for v1

**Status:** accepted (default) · **Date:** 2026-10-12

## Context

SPEC §19.6 keeps differential privacy out of v1 until a mechanism,
threat model, budget accounting, and validation plan exist. §8.5 forbids
claiming DP without a reviewed implementation.

## Decision

- v1 synthetic generation is **empirical-distribution based** and reports
  this explicitly in `GenerationReport` warnings.
- Privacy mitigations in v1: rare-category suppression
  (`rare_category_min_count`), no full-row copying, keyed HMAC
  tokenization (never plain hashes), and a near-duplicate/rare-combination
  review pass.
- The words "anonymous" / "differentially private" are never asserted by
  the tool.

## Consequences

- Reports carry a standing warning that synthesis can leak information.
- A future ADR must specify ε/δ accounting before any DP claim.
