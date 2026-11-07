"""The `state.db` upgrade path (SPEC 14).

`StateStore` had a version check and an empty migration registry, so the first real schema
change would have shipped with nothing proving it upgrades a file in place. These tests
build a v1 database by hand, open it with the current build, and check that the rows
survive, the migration runs once, and a newer file is still refused.

They also exercise the registry itself with test-only migrations, so the *mechanism* is
covered independently of whichever migration happens to be current.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qc.application.jobs import STATE_SCHEMA_VERSION, StateStore
from qc.domain.errors import ValidationError

# The v1 shape, frozen: a migration test must not follow the current `_SCHEMA`, or it
# would stop testing an upgrade the first time the schema changes again.
V1_SCHEMA = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    source_id TEXT,
    dataset_id TEXT,
    progress REAL NOT NULL DEFAULT 0,
    message TEXT,
    result_ref TEXT,
    error_code TEXT,
    correlation_id TEXT NOT NULL,
    cancellable INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE profiles (
    profile_id TEXT PRIMARY KEY,
    dataset TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    path TEXT NOT NULL,
    row_count INTEGER,
    warnings INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_profiles_dataset ON profiles(dataset);
CREATE TABLE schema_version (version INTEGER NOT NULL);
"""


def make_v1(path: Path) -> None:
    """A database as QC 0.2.x would have left it: v1 tables, a job and a profile row."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(V1_SCHEMA)
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
        conn.execute(
            "INSERT INTO jobs (id, kind, status, created_at, updated_at, actor, "
            "correlation_id) VALUES (?,?,?,?,?,?,?)",
            (
                "job_1",
                "profile",
                "completed",
                "2026-11-01T00:00:00Z",
                "2026-11-01T00:00:10Z",
                "local",
                "corr_1",
            ),
        )
        conn.execute(
            "INSERT INTO profiles (profile_id, dataset, source_id, source_fingerprint, "
            "created_at, tool_version, path, row_count, warnings) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "prof_1",
                "main.orders",
                "src_1",
                "fp",
                "2026-11-01T00:00:00Z",
                "0.2.1",
                "/tmp/prof_1.json",
                10,
                0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute("SELECT version FROM schema_version").fetchone()[0])
    finally:
        conn.close()


def indexes(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
        ).fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def test_a_v1_database_is_upgraded_in_place_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    make_v1(path)

    store = StateStore(path)
    try:
        job = store.get_job("job_1")
        assert job is not None
        assert job.status.value == "completed"
        assert store.find_profile_index("prof_1") is not None
        # The v2 index is what makes `job list` cheap on a long-lived state directory.
        assert "idx_jobs_created" in indexes(path)
    finally:
        store.close()

    assert version(path) == STATE_SCHEMA_VERSION


def test_reopening_does_not_reapply_the_migration(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    make_v1(path)

    for _ in range(3):
        store = StateStore(path)
        store.close()

    assert version(path) == STATE_SCHEMA_VERSION
    # Applied once, not once per open: the index exists exactly once, and re-running a
    # migration would show up here even though `IF NOT EXISTS` hides it from SQLite.
    conn = sqlite3.connect(path)
    try:
        created = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND name = 'idx_jobs_created'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert created == 1


def test_a_fresh_database_starts_at_the_current_version(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = StateStore(path)
    try:
        assert store._conn.execute("SELECT version FROM schema_version").fetchone()[0] == (
            STATE_SCHEMA_VERSION
        )
    finally:
        store.close()
    assert "idx_jobs_created" in indexes(path)


def test_a_newer_database_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    make_v1(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE schema_version SET version = ?", (STATE_SCHEMA_VERSION + 1,))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValidationError, match="upgrade QC"):
        StateStore(path)


def test_the_registry_runs_injected_migrations_in_version_order(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    make_v1(path)
    applied: list[int] = []

    def migration(version: int, sql: str) -> str:
        # Record the order the registry asked for, then hand back the SQL.
        applied.append(version)
        return sql

    store = StateStore(
        path,
        migrations={
            2: migration(2, "CREATE TABLE probe_two (id TEXT);"),
            3: migration(3, "ALTER TABLE probe_two ADD COLUMN note TEXT;"),
        },
    )
    store.close()

    assert applied == [2, 3]
    conn = sqlite3.connect(path)
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(probe_two)")]
    finally:
        conn.close()
    assert columns == ["id", "note"]
