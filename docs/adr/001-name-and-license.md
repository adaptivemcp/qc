# ADR-001: Project name and license

**Status:** accepted · **Date:** 2026-10-12 · **Finalized:** 2026-10-16

## Context

SPEC §19.1 requires a name and license decision before public release.

## Decision

- **Name — QueryCleaner**, distributed on PyPI as **`querycleaner`**. The
  bare `qc` distribution name is already taken on PyPI (verified
  2026-10-16), so the distribution name differs from the product name. The
  **import package and the CLI executable stay `qc`** — nothing internal
  changes.
- **License — Apache-2.0.** `LICENSE` is checked in, and the wheel
  declares it (PEP 639 `License-Expression: Apache-2.0` +
  `License-File: LICENSE`).

## Dependency review

Done 2026-10-16, before finalizing (this was the "pending dependency
review" from the original decision):

| Dependency | License | Role |
|---|---|---|
| typer, pydantic, mcp, faker, pyyaml | MIT | core |
| httpx | BSD-3-Clause | core |
| typing-extensions | PSF-2.0 | core |
| psycopg | LGPL-3.0-only | optional `postgres` extra |
| mysql-connector-python | GPLv2 + FOSS License Exception | optional `mysql` extra |

The two copyleft entries are **optional extras**, consumed as dependencies
rather than vendored, and MySQL's FOSS License Exception explicitly
permits use from OSI-approved-licensed projects (Apache-2.0 qualifies).
Apache-2.0 is therefore the correct project license.

## Consequences

- Product/CLI name (`qc`) and distribution name (`querycleaner`) differ;
  install docs say `uv tool install querycleaner`, then run `qc`.
- Internal imports (`qc.*`) never needed to change, so the earlier worry
  about a rename cost did not materialize.
- **Residual:** organizational sign-off on Apache-2.0 remains a human step;
  the dependency review that gated it is complete.
