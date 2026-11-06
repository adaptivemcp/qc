"""Artifact schema-version policy (SPEC 14).

The rule is the same for every artifact: same version reads, older migrates, newer is
refused with an upgrade instruction. These tests pin it once, at the one place all
loaders call, plus each loader that used to miss it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from qc.application.generation import load_plan
from qc.application.jobs import STATE_SCHEMA_VERSION, StateStore
from qc.artifacts.profiles import load_profile
from qc.artifacts.versions import check_supported, read_schema_version
from qc.domain.errors import NotFoundError, ValidationError
from qc.domain.models import (
    SCHEMA_VERSION_PLAN,
    SCHEMA_VERSION_PROFILE,
    DatasetRef,
    GenerationPlan,
    new_id,
)

# ------------------------------------------------------------------ the helper


def test_reads_an_integer_version() -> None:
    assert read_schema_version("profile", {"schema_version": 3}) == 3


@pytest.mark.parametrize("raw", [{}, {"schema_version": "1"}, {"schema_version": True}, [], "x"])
def test_rejects_a_missing_or_non_integer_version(raw: object) -> None:
    with pytest.raises(ValidationError):
        read_schema_version("profile", raw)


def test_a_newer_version_names_the_remedy() -> None:
    with pytest.raises(ValidationError) as raised:
        check_supported("profile", {"schema_version": 9}, 1)

    message = str(raised.value)
    assert "newer than supported" in message
    assert "upgrade QC" in message


def test_an_older_version_is_returned_so_the_owner_can_migrate() -> None:
    # The helper must not migrate: only the owning module knows what its versions mean.
    assert check_supported("profile", {"schema_version": 0}, 1) == 0


# ---------------------------------------------------------------- profile loader


def _profile_payload(version: int) -> dict[str, object]:
    return {
        "schema_version": version,
        "profile_id": new_id("prof"),
        "tool_version": "test",
        "source_id": "src_test",
        "source_fingerprint": "fp",
        "schema_fingerprint": "fp",
        "dataset": DatasetRef(source_id="src_test", name="t").model_dump(mode="json"),
        "dataset_schema": {
            "dataset": DatasetRef(source_id="src_test", name="t").model_dump(mode="json"),
            "source_fingerprint": "fp",
            "columns": [],
        },
        "columns": [],
        "row_count": 1,
    }


def test_profile_of_the_supported_version_loads(tmp_path: Path) -> None:
    path = tmp_path / "p.profile.json"
    path.write_text(json.dumps(_profile_payload(SCHEMA_VERSION_PROFILE)), encoding="utf-8")

    assert load_profile(path).schema_version == SCHEMA_VERSION_PROFILE


def test_profile_from_the_future_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "p.profile.json"
    path.write_text(json.dumps(_profile_payload(SCHEMA_VERSION_PROFILE + 1)), encoding="utf-8")

    with pytest.raises(ValidationError, match="upgrade QC"):
        load_profile(path)


# ------------------------------------------------------------------- plan loader


def _plan_payload(version: int) -> dict[str, object]:
    return {
        "schema_version": version,
        "plan_id": new_id("plan"),
        "profile_ids": ["prof_x"],
        "dataset": DatasetRef(source_id="src_test", name="t").model_dump(mode="json"),
        "rows": 10,
    }


def test_plan_of_the_supported_version_loads(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(_plan_payload(SCHEMA_VERSION_PLAN)), encoding="utf-8")

    plan = load_plan(path)
    assert isinstance(plan, GenerationPlan)
    assert plan.rows == 10


def test_plan_from_the_future_is_refused(tmp_path: Path) -> None:
    """The loader this guarded nothing on before: a newer plan was accepted silently."""
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(_plan_payload(SCHEMA_VERSION_PLAN + 1)), encoding="utf-8")

    with pytest.raises(ValidationError, match="upgrade QC"):
        load_plan(path)


def test_corrupt_and_missing_plans_are_reported_not_raised_raw(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValidationError, match="corrupt plan"):
        load_plan(corrupt)
    with pytest.raises(NotFoundError):
        load_plan(tmp_path / "absent.json")


def test_a_plan_missing_the_version_is_refused(tmp_path: Path) -> None:
    payload = _plan_payload(SCHEMA_VERSION_PLAN)
    del payload["schema_version"]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="schema_version"):
        load_plan(path)


# ------------------------------------------------------------------- state store


def test_state_store_reports_a_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = StateStore(path)
    store.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE schema_version SET version = ?", (STATE_SCHEMA_VERSION + 1,))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValidationError, match="upgrade QC"):
        StateStore(path)


def test_state_store_opens_and_migrates_its_own_version(tmp_path: Path) -> None:
    path = tmp_path / "state.db"

    store = StateStore(path)
    try:
        version = store._conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == STATE_SCHEMA_VERSION
    finally:
        store.close()

    # Reopening at the same version is not a migration.
    StateStore(path).close()
