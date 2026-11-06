"""The conformance adoption plan must stay executable, not decorative.

`docs/conformance.md` commits to a harness invocation, a baseline path and a CI job. The
plan is only worth writing down if the pieces it names exist and still parse, so this
checks:

- the baseline file is where the plan says, and is valid in the harness's schema;
- the plan's documented invocation names the harness and the baseline;
- the CI snippet in the plan is real YAML with a step that runs the harness;
- the pin is explicit (an unpinned `npx` is not reproducible evidence);
- the roadmap still points at the plan.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "conformance.md"
BASELINE = ROOT / "tools" / "conformance" / "conformance-baseline.yml"
ROADMAP = ROOT / "docs" / "roadmap.md"

HARNESS = "@modelcontextprotocol/conformance"


def _plan() -> str:
    return PLAN.read_text(encoding="utf-8")


def _yaml_blocks(text: str) -> list[object]:
    """Every fenced yaml block in the plan, parsed — a snippet that does not parse is a lie."""
    blocks = re.findall(r"```yaml\n(.*?)```", text, re.DOTALL)
    return [yaml.safe_load(block) for block in blocks]


def test_the_baseline_exists_where_the_plan_says() -> None:
    assert BASELINE.is_file()
    assert "tools/conformance/conformance-baseline.yml" in _plan()


def test_the_baseline_uses_the_harness_schema() -> None:
    baseline = yaml.safe_load(BASELINE.read_text(encoding="utf-8"))

    assert isinstance(baseline, dict)
    assert set(baseline) <= {"server", "client"}
    for mode, entries in baseline.items():
        assert isinstance(entries, list), mode
        for entry in entries:
            assert isinstance(entry, str)
            # `<scenario>` or `<scenario>:<check-id>`; a space after the colon is YAML
            # for a mapping and is rejected by the harness.
            assert " " not in entry, entry


def test_the_documented_invocation_names_the_harness_and_baseline() -> None:
    plan = _plan()

    assert f"{HARNESS} server" in plan
    assert "--url" in plan
    assert "--expected-failures tools/conformance/conformance-baseline.yml" in plan


def test_the_harness_is_pinned_to_a_version() -> None:
    plan = _plan()

    pins = re.findall(rf"{re.escape(HARNESS)}@(\d+\.\d+\.\d+)", plan)
    assert pins, "the plan must pin a harness version; a floating npx is not evidence"
    assert len(set(pins)) == 1, f"one pin, not {sorted(set(pins))}"


def test_the_ci_snippet_is_real_yaml_with_a_conformance_step() -> None:
    blocks = _yaml_blocks(_plan())
    jobs = [block for block in blocks if isinstance(block, dict) and "conformance" in block]

    assert jobs, "the plan must include a CI job snippet"
    job = jobs[0]["conformance"]
    steps = job["steps"]
    assert isinstance(steps, list)
    commands = [step.get("run", "") for step in steps if isinstance(step, dict) and "run" in step]
    assert any(f"{HARNESS}" in command for command in commands)
    # Advisory until the baseline is populated; a blocking job here would be red on day one.
    assert any(isinstance(step, dict) and step.get("continue-on-error") is True for step in steps)


def test_the_plan_says_the_proxy_is_not_written_yet() -> None:
    plan = _plan()

    # The honest status: QC's HTTP transport requires auth and the harness has no header
    # option, so the proxy is a prerequisite and must not be described as existing.
    assert "require_auth" in plan
    assert "not written yet" in plan


def test_the_roadmap_points_at_the_plan() -> None:
    roadmap = ROADMAP.read_text(encoding="utf-8")

    assert "conformance.md" in roadmap
