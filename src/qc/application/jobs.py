"""Job registry: SQLite job metadata + profile index (SPEC 15, ADR-004).

No raw source rows ever enter this database. Profile bodies live as
JSON files; SQLite stores only index metadata and job state.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path

from qc.domain.enums import JobStatus
from qc.domain.models import JobRecord, ProfileArtifact

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
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
CREATE TABLE IF NOT EXISTS profiles (
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
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_profiles_dataset ON profiles(dataset);
"""

_MIGRATIONS: dict[int, str] = {}


class StateStore:
    """SQLite-backed job/profile index with 0600 file permissions."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        if cur.fetchone() is None:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            self._conn.execute("INSERT INTO schema_version (version) VALUES (1)")
            return
        row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        version = int(row["version"]) if row else 1
        for target in sorted(_MIGRATIONS):
            if target > version:
                self._conn.executescript(_MIGRATIONS[target])
                self._conn.execute("UPDATE schema_version SET version = ?", (target,))
                version = target

    # ------------------------------------------------------------------ jobs

    def upsert_job(self, job: JobRecord) -> None:
        self._conn.execute(
            """INSERT INTO jobs (id, kind, status, created_at, updated_at, actor,
                   source_id, dataset_id, progress, message, result_ref,
                   error_code, correlation_id, cancellable)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   status=excluded.status, updated_at=excluded.updated_at,
                   progress=excluded.progress, message=excluded.message,
                   result_ref=excluded.result_ref, error_code=excluded.error_code""",
            (
                job.id,
                job.kind,
                job.status.value,
                job.created_at.isoformat(),
                job.updated_at.isoformat(),
                job.actor,
                job.source_id,
                job.dataset_id,
                job.progress,
                job.message,
                job.result_ref,
                job.error_code,
                job.correlation_id,
                int(job.cancellable),
            ),
        )
        self._conn.commit()

    def get_job(self, job_id: str) -> JobRecord | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return JobRecord(
            id=row["id"],
            kind=row["kind"],
            status=JobStatus(row["status"]),
            created_at=__import__("datetime").datetime.fromisoformat(row["created_at"]),
            updated_at=__import__("datetime").datetime.fromisoformat(row["updated_at"]),
            actor=row["actor"],
            source_id=row["source_id"],
            dataset_id=row["dataset_id"],
            progress=row["progress"],
            message=row["message"],
            result_ref=row["result_ref"],
            error_code=row["error_code"],
            correlation_id=row["correlation_id"],
            cancellable=bool(row["cancellable"]),
        )

    def list_jobs(self, limit: int = 100) -> list[JobRecord]:
        rows = self._conn.execute(
            "SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            job = self.get_job(r["id"])
            if job:
                out.append(job)
        return out

    def set_job_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        message: str | None = None,
        progress: float | None = None,
        result_ref: str | None = None,
        error_code: str | None = None,
    ) -> None:
        from qc.domain.enums import utc_now

        job = self.get_job(job_id)
        if job is None:
            return
        job.status = status
        job.updated_at = utc_now()
        if message is not None:
            job.message = message
        if progress is not None:
            job.progress = progress
        if result_ref is not None:
            job.result_ref = result_ref
        if error_code is not None:
            job.error_code = error_code
        self.upsert_job(job)

    # --------------------------------------------------------------- profiles

    def index_profile(self, artifact: ProfileArtifact, path: Path) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO profiles
               (profile_id, dataset, source_id, source_fingerprint, created_at,
                tool_version, path, row_count, warnings)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                artifact.profile_id,
                artifact.dataset.qualified_name,
                artifact.source_id,
                artifact.source_fingerprint,
                artifact.created_at.isoformat(),
                artifact.tool_version,
                str(path),
                artifact.row_count,
                len(artifact.warnings),
            ),
        )
        self._conn.commit()

    def find_profile_index(self, profile_ref: str) -> Path | None:
        row = self._conn.execute(
            "SELECT path FROM profiles WHERE profile_id = ?", (profile_ref,)
        ).fetchone()
        if row is not None:
            return Path(row["path"])
        rows = self._conn.execute(
            "SELECT path FROM profiles WHERE profile_id LIKE ? LIMIT 2",
            (f"{profile_ref}%",),
        ).fetchall()
        if len(rows) == 1:
            return Path(rows[0]["path"])
        return None

    def list_profiles(self, limit: int = 100) -> list[dict[str, str]]:
        rows = self._conn.execute(
            """SELECT profile_id, dataset, created_at, tool_version, row_count, warnings
               FROM profiles ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
