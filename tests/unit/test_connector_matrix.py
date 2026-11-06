"""The documented server matrix must match the workflow that actually runs it.

A compatibility table is only worth its maintenance cost if it breaks when it goes
stale. `docs/connectors.md#server-compatibility` names the images the live database
tests use; the `integration-db` job in `.github/workflows/ci.yml` is the source of truth.
This test fails on either kind of drift:

- a server the workflow exercises but the table does not document, and
- a server tag the table promises that the workflow never runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
DOC = ROOT / "docs" / "connectors.md"

#: Only the live database job; other jobs run against fixtures.
JOB = "integration-db"


def _services() -> dict[str, str]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"][JOB]
    return {name: service["image"] for name, service in job["services"].items()}


def _compatibility_section() -> str:
    text = DOC.read_text(encoding="utf-8")
    start = text.index("## Server compatibility")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def _documented_images() -> set[str]:
    # Every container image tag named in the section, in any column.
    return set(re.findall(r"`([a-z][a-z0-9-]*:[0-9][\w.\-]*)`", _compatibility_section()))


def test_every_exercised_image_is_documented() -> None:
    documented = _documented_images()

    for name, image in _services().items():
        assert image in documented, (
            f"{JOB} exercises {name} via `{image}` but docs/connectors.md does not list it "
            "in the server compatibility table"
        )


def test_no_documented_image_is_unexercised() -> None:
    exercised = set(_services().values())

    for image in _documented_images() - exercised:
        raise AssertionError(
            f"docs/connectors.md claims `{image}` but {JOB} never runs it; either add the "
            "service to the workflow or drop the claim"
        )


def test_the_workflow_still_has_the_live_database_job() -> None:
    """A silent rename would make the two tests above vacuous."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"][JOB]

    services = job["services"]
    assert set(services) == {"postgres", "mysql"}
    assert services["postgres"]["ports"] == ["5432:5432"]
    assert services["mysql"]["ports"] == ["3306:3306"]


def test_the_documented_dsn_variables_match_the_workflow() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    env = workflow["jobs"][JOB]["env"]
    section = _compatibility_section()

    assert set(env) == {"QC_TEST_POSTGRES_DSN", "QC_TEST_MYSQL_DSN"}
    for name in env:
        # The contract suite's own docs name these; the matrix points at the same job.
        assert name in (ROOT / "docs" / "connectors.md").read_text(encoding="utf-8")
    assert "integration-db" in section
