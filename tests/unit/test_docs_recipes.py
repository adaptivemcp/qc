"""Documentation guards: the README/docs recipes must match the CLI.

Regression cover for the quickstart bug fixed in #23. QC is deny-by-default
(SPEC 5.7): a recipe that adds a source and then tests/profiles it without an
explicit ``qc policy grant`` fails with ``POLICY_DENIED`` — which is what a
first-time user hit because the README omitted the step. Static checks are
enough and run fast in the normal suite:

* every relative link in the docs resolves (nothing points at a file that
  nothing generates — e.g. the removed ``workbench/README-demo.md``);
* a recipe that adds a source *and* then uses it grants first.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCS = [REPO / "README.md", *sorted((REPO / "docs").rglob("*.md"))]

_FENCE = re.compile(r"^\s*```(\w*)")
_SHELL_LANGS = {"bash", "sh", "shell", "console"}

# A recipe = a shell block that adds a source...
_ADD_SOURCE = re.compile(r"\bqc\s+source\s+add\b")
# ...and then uses it (any of these needs the source authorized).
_USES_SOURCE = re.compile(
    r"\bqc\s+(?:source\s+test|source\s+inspect|dataset\s+list|profile\s+run|"
    r"profile\s+show|sanitize\s+\w+|generate\s+\w+)\b"
)
_GRANT = re.compile(r"\bqc\s+policy\s+grant\b")
_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def shell_blocks(text: str) -> list[str]:
    """Return the contents of every fenced shell block, as a joined string each."""
    blocks: list[str] = []
    lang: str | None = None
    current: list[str] = []
    for line in text.splitlines():
        fence = _FENCE.match(line)
        if fence:
            if lang is None:
                lang = fence.group(1).lower()
            else:
                if lang in _SHELL_LANGS:
                    blocks.append("\n".join(current))
                lang, current = None, []
            continue
        if lang is not None:
            current.append(line)
    return blocks


def grant_violations(block: str) -> list[str]:
    """Problems with one shell block: a recipe that uses a source without granting first."""
    lines = block.splitlines()
    if not any(_ADD_SOURCE.search(line) for line in lines):
        return []  # a standalone snippet, not a full recipe
    first_use = next((i for i, line in enumerate(lines) if _USES_SOURCE.search(line)), None)
    if first_use is None:
        return []
    grant = next((i for i, line in enumerate(lines) if _GRANT.search(line)), None)
    if grant is None:
        return ["recipe uses a source but never runs `qc policy grant`"]
    if grant > first_use:
        return ["recipe runs `qc policy grant` after it first uses the source"]
    return []


def test_recipes_grant_before_using_a_source() -> None:
    problems: list[str] = []
    for doc in DOCS:
        for block in shell_blocks(doc.read_text()):
            for message in grant_violations(block):
                problems.append(f"{doc.relative_to(REPO)}: {message}\n{block}")
    assert not problems, "documentation recipes drifted from deny-by-default:\n\n" + "\n\n".join(
        problems
    )


def test_the_grant_check_flags_a_missing_grant() -> None:
    # A recipe shaped exactly like the one that broke: add a source, then use it.
    bad = "qc init\nqc source add demo --type sqlite --connection-ref env:X\nqc source test demo\n"
    assert grant_violations(bad)

    late = "qc source add d --type sqlite --connection-ref env:X\nqc profile run d t\nqc policy grant p d\n"
    assert grant_violations(late)

    good = "qc source add d --type sqlite --connection-ref env:X\nqc policy grant p d\nqc profile run d t\n"
    assert not grant_violations(good)

    # A standalone snippet that only uses a source is not a recipe — not our business.
    assert not grant_violations("qc profile run analytics public.orders --max-queries 40\n")


def test_relative_links_resolve() -> None:
    broken: list[str] = []
    for doc in DOCS:
        for target in _LINK.findall(doc.read_text()):
            target = target.strip()
            if not target or target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path = (doc.parent / target.split("#", 1)[0]).resolve()
            if not path.exists():
                broken.append(f"{doc.relative_to(REPO)} -> {target}")
    assert not broken, "documentation links point at files that do not exist:\n" + "\n".join(broken)
