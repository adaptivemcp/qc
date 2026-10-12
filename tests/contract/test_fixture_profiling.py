"""End-to-end profiling tests over the fixture connector (M1 exit criteria)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qc.artifacts.profiles import load_profile, save_profile
from qc.config.policies import parse_policy
from qc.connectors.fixture import FixtureConnector
from qc.domain.enums import Budget, LogicalType, Measurement
from qc.domain.errors import BudgetExceededError, NotFoundError, PolicyDeniedError
from qc.domain.models import DatasetRef, SourceConfig
from qc.profiling.engine import Profiler, ProfileSettings
from qc.security.policy import PolicyEngine


def policy_for(root: Path) -> PolicyEngine:
    text = """
name: test
sources:
  fixtures:
    access: read
    datasets: [people.jsonl, sparse.jsonl, empty.jsonl]
"""
    return PolicyEngine(parse_policy(text))


def source_config() -> SourceConfig:
    return SourceConfig(
        name="fixtures",
        connector="fixture",
        connection_ref="env:QC_FIXTURE_ROOT_NOT_USED",
    )


def dataset(name: str) -> DatasetRef:
    return DatasetRef(source_id="src", name=name, locator=name)


async def run_profile(
    root: Path,
    name: str,
    settings: ProfileSettings | None = None,
    engine: PolicyEngine | None = None,
):
    connector = FixtureConnector(root)
    engine = engine or policy_for(root)
    profiler = Profiler(connector, engine)
    result = await profiler.profile(
        source_config(),
        dataset(name),
        settings or ProfileSettings(sample_size=1000),
    )
    await connector.close()
    return result


@pytest.mark.asyncio
class TestProfileRun:
    async def test_basic_profile(self, fixture_root: Path) -> None:
        result = await run_profile(fixture_root, "people.jsonl")
        art = result.artifact
        assert art.row_count == 8
        assert art.row_count_measurement == Measurement.EXACT
        assert len(art.columns) == 11
        assert result.budget_usage.queries > 0
        assert art.schema_version == 1
        assert art.profile_id.startswith("prof_")

    async def test_null_falsy_preserved(self, fixture_root: Path) -> None:
        result = await run_profile(fixture_root, "people.jsonl")
        age = next(c for c in result.artifact.columns if c.name == "age")
        assert age.falsy.null_count == 3  # None, None, None
        assert age.falsy.zero_count == 2  # 0, 0
        note = next(c for c in result.artifact.columns if c.name == "note")
        assert note.falsy.empty_string_count == 1
        assert note.falsy.whitespace_only_count == 1
        assert note.falsy.string_null_count == 1  # "null"
        assert note.falsy.string_null_count != note.falsy.null_count

    async def test_type_inference(self, fixture_root: Path) -> None:
        result = await run_profile(fixture_root, "people.jsonl")
        cols = {c.name: c for c in result.artifact.columns}
        assert cols["email"].logical_type == LogicalType.EMAIL
        assert cols["id"].logical_type in (
            LogicalType.IDENTIFIER,
            LogicalType.STRING,
        )  # leading zeros preserved
        assert cols["active"].logical_type == LogicalType.BOOLEAN
        assert cols["signup"].logical_type == LogicalType.DATE
        assert cols["age"].logical_type == LogicalType.INTEGER
        assert cols["age"].numeric is not None
        assert cols["age"].numeric.minimum == "0"
        assert cols["age"].numeric.maximum == "41"

    async def test_measured_vs_inferred_labels(self, fixture_root: Path) -> None:
        result = await run_profile(fixture_root, "people.jsonl")
        cols = {c.name: c for c in result.artifact.columns}
        assert cols["age"].distinct_measurement == Measurement.EXACT
        assert cols["age"].distinct_count == 4  # 34, 0, 29, 41 (SQL: NULLs excluded)
        assert cols["age"].notes  # inference reasons recorded

    async def test_sensitivity_detection(self, fixture_root: Path) -> None:
        result = await run_profile(fixture_root, "people.jsonl")
        email = next(c for c in result.artifact.columns if c.name == "email")
        labels = {f.label.value for f in email.sensitivity}
        assert "email" in labels
        assert all(f.evidence for f in email.sensitivity)
        # no raw values in evidence
        assert not any("@" in f.evidence for f in email.sensitivity)

    async def test_warnings_and_confidence(self, fixture_root: Path) -> None:
        result = await run_profile(fixture_root, "people.jsonl")
        art = result.artifact
        assert "high" in art.confidence_summary
        assert art.deterministic_settings["algorithm_version"]

    async def test_sparse_dataset(self, fixture_root: Path) -> None:
        result = await run_profile(fixture_root, "sparse.jsonl")
        a = next(c for c in result.artifact.columns if c.name == "a")
        assert a.falsy.null_count == 5

    async def test_empty_dataset(self, fixture_root: Path) -> None:
        result = await run_profile(fixture_root, "empty.jsonl")
        art = result.artifact
        assert art.row_count == 0
        assert art.columns == []


class TestBudgets:
    async def test_zero_query_budget_fails_closed(self, fixture_root: Path) -> None:
        settings = ProfileSettings(budget=Budget(max_queries=0, max_rows=0))
        with pytest.raises(BudgetExceededError):
            await run_profile(fixture_root, "people.jsonl", settings)

    async def test_budget_exhausted_mid_run_records_incomplete(self, fixture_root: Path) -> None:
        # enough budget for row count + sampling, not for every column probe
        settings = ProfileSettings(budget=Budget(max_queries=6))
        result = await run_profile(fixture_root, "people.jsonl", settings)
        assert result.artifact.status == "completed"
        assert result.budget_usage.queries <= 6
        assert result.artifact.incomplete_checks  # blocked probes recorded
        # every column still reports what was measurable from metadata/sample
        assert len(result.artifact.columns) == 11


class TestPolicyGates:
    async def test_denied_dataset_raises(self, fixture_root: Path) -> None:
        engine = PolicyEngine(
            parse_policy("name: empty\nsources:\n  fixtures:\n    access: deny\n")
        )
        with pytest.raises(PolicyDeniedError):
            await run_profile(fixture_root, "people.jsonl", engine=engine)

    async def test_full_scan_probe_skipped_without_flag(self, fixture_root: Path) -> None:
        result = await run_profile(fixture_root, "people.jsonl")
        skipped = [d for d in result.decisions if d.probe == "edge_rare_values"]
        assert skipped
        assert all(not d.selected for d in skipped)
        assert all(d.reason == "full_scan_not_allowed" for d in skipped)

    async def test_full_scan_flag_requires_policy_approval(self, fixture_root: Path) -> None:
        # policy has no allow_full_scan → asking for a full scan is denied
        settings = ProfileSettings(allow_full_scan=True)
        with pytest.raises(PolicyDeniedError):
            await run_profile(fixture_root, "people.jsonl", settings)

    async def test_full_scan_approved_by_policy(self, fixture_root: Path) -> None:
        engine = PolicyEngine(
            parse_policy(
                """
name: scan
allow_raw_samples: false
budget:
  allow_full_scan: true
sources:
  fixtures:
    access: read
    datasets: [people.jsonl]
"""
            )
        )
        settings = ProfileSettings(allow_full_scan=True)
        result = await run_profile(fixture_root, "people.jsonl", settings, engine=engine)
        selected = [d for d in result.decisions if d.probe == "edge_rare_values"]
        assert selected and all(d.selected for d in selected)


class TestArtifacts:
    async def test_roundtrip(self, fixture_root: Path, tmp_path: Path) -> None:
        result = await run_profile(fixture_root, "people.jsonl")
        out = save_profile(tmp_path / "profiles", result.artifact)
        assert out.is_file()
        loaded = load_profile(out)
        assert loaded.profile_id == result.artifact.profile_id
        assert len(loaded.columns) == len(result.artifact.columns)
        # restrictive permissions
        assert oct(out.stat().st_mode)[-3:] in ("600", "644")

    async def test_no_raw_values_in_artifact(self, fixture_root: Path, tmp_path: Path) -> None:
        result = await run_profile(fixture_root, "people.jsonl")
        out = save_profile(tmp_path / "profiles", result.artifact)
        text = out.read_text(encoding="utf-8")
        for secret in ("alice@example.com", "bob@example.com", "+1 555 123 4567"):
            assert secret not in text

    async def test_future_version_fails_safe(self, fixture_root: Path, tmp_path: Path) -> None:
        result = await run_profile(fixture_root, "people.jsonl")
        out = save_profile(tmp_path / "profiles", result.artifact)
        raw = json.loads(out.read_text(encoding="utf-8"))
        raw["schema_version"] = 99
        out.write_text(json.dumps(raw), encoding="utf-8")
        from qc.domain.errors import ValidationError

        with pytest.raises(ValidationError) as exc:
            load_profile(out)
        assert "upgrade" in str(exc.value)

    async def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        from qc.artifacts.profiles import artifact_path
        from qc.domain.errors import ValidationError

        with pytest.raises(ValidationError):
            artifact_path(tmp_path, "../../etc/passwd", None)


class TestConnectorErrors:
    async def test_missing_dataset(self, fixture_root: Path) -> None:
        from qc.connectors.base import SampleRequest

        connector = FixtureConnector(fixture_root)
        with pytest.raises(NotFoundError):
            async for _ in connector.sample_rows(
                source_config(),
                SampleRequest(dataset=dataset("nope.jsonl"), limit=10),
            ):
                pass

    async def test_path_escape_rejected(self, fixture_root: Path) -> None:
        from qc.domain.errors import SourceError

        connector = FixtureConnector(fixture_root)
        with pytest.raises(SourceError):
            await connector.get_schema(source_config(), dataset("/etc/passwd"))
