"""The security review record must stay tied to the code that enforces it.

`docs/security-review.md` is a table of claims: each row names the symbol that enforces a
mitigation and the test that proves it. That format is only worth the effort if it breaks
when the code moves, so this test parses the table and checks every reference:

- a cited module or symbol must exist (a rename cannot leave a claim behind),
- a cited test must exist in the file named,
- the recorded review version must be the version being released.

It deliberately does **not** re-run the mitigations: `tests/security/test_security.py`
already does that, and duplicating it here would let the two drift.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REVIEW = ROOT / "docs" / "security-review.md"
PYPROJECT = ROOT / "pyproject.toml"

#: `src/qc/x.py` or `src/qc/x.py::Symbol` — and the same shape for tests.
REFERENCE = re.compile(r"`((?:src|tests)/[\w/]+\.py)(?:::([\w.]+))?`")


def _rows() -> list[str]:
    text = REVIEW.read_text(encoding="utf-8")
    start = text.index("## What was checked")
    end = text.index("## Residual risks")
    return [line for line in text[start:end].splitlines() if line.startswith("| ")][2:]


def _references(cell: str) -> list[tuple[Path, str | None]]:
    found: list[tuple[Path, str | None]] = []
    for path, symbol in REFERENCE.findall(cell):
        found.append((ROOT / path, symbol or None))
    return found


def test_the_table_has_rows() -> None:
    rows = _rows()

    assert len(rows) >= 15, "a review that checks fewer than 15 threats is not a review"
    for row in rows:
        # Every row must name both the enforcement and its test, or the claim is prose.
        assert len(_references(row)) >= 2, row


def test_every_cited_symbol_still_exists() -> None:
    for row in _rows():
        for path, symbol in _references(row):
            assert path.is_file(), f"{row}\n  cites missing file {path.relative_to(ROOT)}"
            if symbol is None:
                continue
            body = path.read_text()
            # `Class.method` must have both halves somewhere in the module.
            missing = [part for part in symbol.split(".") if part not in body]
            assert not missing, (
                f"{row}\n  cites {symbol} but {path.relative_to(ROOT)} does not define "
                f"{', '.join(missing)}"
            )


def test_every_cited_test_exists() -> None:
    for row in _rows():
        for path, symbol in _references(row):
            if "tests" not in path.parts:
                continue
            if symbol is None:
                continue
            assert symbol in path.read_text(), (
                f"{row}\n  cites test {symbol} but {path.relative_to(ROOT)} does not contain it"
            )


def test_the_reviewed_version_matches_the_package() -> None:
    version = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    record = REVIEW.read_text(encoding="utf-8")

    assert f"**Version:** {version}" in record, (
        "docs/security-review.md records a different version than pyproject.toml; "
        "a review of an older version is history, not a review"
    )


def test_residual_risks_name_a_reason() -> None:
    text = REVIEW.read_text(encoding="utf-8")
    start = text.index("## Residual risks")
    end = text.index("## Re-running this review")
    rows = [line for line in text[start:end].splitlines() if line.startswith("| ")][2:]

    assert len(rows) >= 5
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        # Risk | why accepted | what would change it
        assert len(cells) == 3, row
        assert all(cells), f"an accepted risk without a reason or a trigger: {row}"
