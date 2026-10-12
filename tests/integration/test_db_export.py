"""Database destination export tests (SPEC 10.3, acceptance criteria).

Live: writes an approved synthetic file into disposable Postgres/MySQL
tables, verifies create/append/replace modes, rollback on failure, and
that manifests carry no credentials. Skipped without DSNs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from qc.domain.errors import PolicyDeniedError, ValidationError, WriteError
from qc.domain.models import SourceConfig

pytestmark = pytest.mark.integration


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    out = tmp_path / "synth.jsonl"
    rows = [
        {
            "id": i,
            "label": f"row-{i}",
            "amount": i * 1.5,
            "flag": i % 2 == 0,
            "note": None if i % 3 == 0 else "x",
        }
        for i in range(12)
    ]
    out.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return out


class TestPolicyGates:
    def test_write_denied_without_policy(self, sample_file: Path, tmp_path: Path) -> None:
        import asyncio

        from qc.application.exports import export_to_database
        from qc.application.runtime import Runtime

        rt = Runtime(root=tmp_path)
        # strict default: write_allowed false + ceiling off
        with pytest.raises(PolicyDeniedError):
            asyncio.run(
                export_to_database(
                    rt,
                    output=sample_file,
                    destination="pg_dest",
                    table="public.export_probe",
                )
            )

    def test_invalid_mode_rejected(self, sample_file: Path, tmp_path: Path) -> None:
        import asyncio

        from qc.application.exports import export_to_database
        from qc.application.runtime import Runtime

        rt = Runtime(root=tmp_path)
        with pytest.raises(ValidationError):
            asyncio.run(
                export_to_database(
                    rt,
                    output=sample_file,
                    destination="pg_dest",
                    table="t",
                    mode="overwrite_everything",
                )
            )

    def test_unsafe_table_name_rejected(self, sample_file: Path, tmp_path: Path) -> None:
        import asyncio

        from qc.application.exports import export_to_database
        from qc.application.runtime import Runtime

        rt = Runtime(root=tmp_path)
        with pytest.raises(ValidationError):
            asyncio.run(
                export_to_database(
                    rt,
                    output=sample_file,
                    destination="pg_dest",
                    table="t; DROP TABLE users",
                )
            )


class TestPostgresDestination:
    @pytest.fixture
    def dest_config(self) -> SourceConfig:
        return SourceConfig(
            name="pg_dest",
            connector="postgres",
            connection_ref="env:QC_TEST_POSTGRES_DSN",
            default_schema="public",
        )

    @pytest.fixture
    def rows(self) -> list[dict]:
        return [
            {
                "id": i,
                "label": f"r{i}",
                "amount": i + 0.5,
                "flag": bool(i % 2),
                "note": None if i % 2 else "v",
            }
            for i in range(5)
        ]

    async def test_create_and_append_and_replace(self, dest_config, rows) -> None:
        if not os.environ.get("QC_TEST_POSTGRES_DSN"):
            pytest.skip("no postgres dsn")
        from qc.exports.db_writer import PostgresDestination

        w = PostgresDestination()
        try:
            n = await w.write_rows(
                dest_config,
                "public.export_probe",
                ["id", "label", "amount", "flag", "note"],
                rows,
                mode="create",
            )
            assert n == 5
            cols, got, _ = await _pg_select(w, "public.export_probe")
            assert len(got) == 5
            assert "amount" in cols

            n = await w.write_rows(
                dest_config,
                "public.export_probe",
                ["id", "label", "amount", "flag", "note"],
                rows[:2],
                mode="append",
            )
            assert n == 2
            _cols, got, _ = await _pg_select(w, "public.export_probe")
            assert len(got) == 7

            n = await w.write_rows(
                dest_config,
                "public.export_probe",
                ["id", "label", "amount", "flag", "note"],
                rows,
                mode="replace",
            )
            assert n == 5
            _cols, got, _ = await _pg_select(w, "public.export_probe")
            assert len(got) == 5
        finally:
            await w.close()
            await _pg_drop("public.export_probe")

    async def test_rollback_on_bad_write(self, dest_config) -> None:
        if not os.environ.get("QC_TEST_POSTGRES_DSN"):
            pytest.skip("no postgres dsn")
        from qc.exports.db_writer import PostgresDestination

        w = PostgresDestination()
        try:
            # seed one good row
            await w.write_rows(
                dest_config,
                "public.export_probe",
                ["id", "label"],
                [{"id": 1, "label": "keep"}],
                mode="create",
            )
            # second write with a type-breaking value after valid rows
            with pytest.raises(WriteError):
                await w.write_rows(
                    dest_config,
                    "public.export_probe",
                    ["id", "label"],
                    [{"id": 2, "label": "ok"}, {"id": object(), "label": "boom"}],  # unbindable
                    mode="append",
                )
            # first write remains (its transaction committed)
            _cols, got, _ = await _pg_select(w, "public.export_probe")
            ids = [r[0] for r in got]
            assert 1 in ids
            assert 2 not in ids  # rolled back
        finally:
            await w.close()
            await _pg_drop("public.export_probe")


async def _pg_select(writer, table: str):  # type: ignore[no-untyped-def]
    conn = await writer._connect(
        SourceConfig(
            name="pg_dest", connector="postgres", connection_ref="env:QC_TEST_POSTGRES_DSN"
        )
    )
    cur = await conn.execute(f"SELECT * FROM {table} ORDER BY id")
    cols = [d.name for d in cur.description]
    rows = await cur.fetchall()
    return cols, [list(r) for r in rows], None


async def _pg_drop(table: str) -> None:
    import psycopg

    dsn = os.environ.get("QC_TEST_POSTGRES_DSN")
    if not dsn:
        return
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as c:
        await c.execute(f"DROP TABLE IF EXISTS {table}")


def _mysql_schema() -> str:
    """Schema comes from the DSN; a hardcoded name breaks CI (SPEC 16.3)."""
    dsn = os.environ["QC_TEST_MYSQL_DSN"]
    return dsn.rsplit("/", 1)[-1].split("?")[0]


class TestMySQLDestination:
    @pytest.fixture
    def dest_config(self) -> SourceConfig:
        return SourceConfig(
            name="my_dest",
            connector="mysql",
            connection_ref="env:QC_TEST_MYSQL_DSN",
            default_schema=_mysql_schema(),
        )

    async def test_create_and_select(self, dest_config) -> None:
        if not os.environ.get("QC_TEST_MYSQL_DSN"):
            pytest.skip("no mysql dsn")
        from qc.connectors.mysql import _parse_mysql_dsn
        from qc.exports.db_writer import MySQLDestination
        from qc.security.secrets import resolve_secret

        schema = _mysql_schema()
        table = f"{schema}.export_probe"
        rows = [{"id": i, "label": f"r{i}", "flag": bool(i % 2)} for i in range(4)]
        w = MySQLDestination()
        try:
            n = await w.write_rows(dest_config, table, ["id", "label", "flag"], rows, mode="create")
            assert n == 4
        finally:
            await w.close()
        # verify via raw connection, fully qualified
        import mysql.connector

        conn = mysql.connector.connect(**_parse_mysql_dsn(resolve_secret("env:QC_TEST_MYSQL_DSN")))
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            assert cur.fetchone()[0] == 4
            cur.execute(f"DROP TABLE {table}")
            conn.commit()
            cur.close()
        finally:
            conn.close()

    async def test_unsupported_destination_type(self) -> None:
        from qc.application.exports import _destination_writer

        cfg = SourceConfig(name="x", connector="google-sheets", connection_ref="env:X")
        with pytest.raises(WriteError):
            _destination_writer(cfg)


class TestManifestSafety:
    def test_manifest_has_no_credentials(self, tmp_path: Path) -> None:
        """Export manifests record provenance but never secrets (SPEC 10.3)."""
        from qc.application.sanitize import _write_manifest

        target = tmp_path / "out.jsonl"
        target.write_text('{"a":1}\n')
        mpath = _write_manifest(
            target,
            source="pg",
            dataset="public.t",
            policy="p",
            fmt="jsonl",
            rows=1,
            blocked=[],
        )
        text = mpath.read_text().lower()
        for banned in ("password", "secret=", "://", "token="):
            assert banned not in text
        payload = json.loads(mpath.read_text())
        assert payload["secrets"] == "none"
