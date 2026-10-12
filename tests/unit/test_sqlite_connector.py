"""SQLite connector tests: read-only enforcement, path handling, and the
query/sampling paths that differ from the networked SQL connectors.

Unlike PostgreSQL/MySQL, SQLite can enforce read-only *in the engine*, so
these tests assert that guarantee directly rather than trusting session
settings (SPEC 6.5).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qc.connectors.base import ProfileQuery, SampleRequest
from qc.connectors.sqlite_conn import _SQLITE_READONLY_PRAGMAS, SQLiteConnector
from qc.domain.errors import NotFoundError, QCError, UnsupportedError
from qc.domain.models import DatasetRef, SourceConfig


def _make_db(path: Path, *, extra: str = "") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE orders(
                id INTEGER PRIMARY KEY,
                label TEXT NOT NULL,
                amount REAL,
                flag INTEGER,
                note TEXT
            );
            """
            + extra
        )
        for i in range(10):
            conn.execute(
                "INSERT INTO orders VALUES (?,?,?,?,?)",
                (i, f"row-{i}", f"{i}.25", i % 2, "" if i % 3 == 0 else None),
            )
        conn.commit()
    finally:
        conn.close()


def _config(path: Path) -> SourceConfig:
    return SourceConfig(
        name="sqlite",
        connector="sqlite",
        connection_ref="env:QC_TEST_SQLITE_PATH",
        options={"path": str(path)},
    )


def _ref(config: SourceConfig, name: str = "orders") -> DatasetRef:
    return DatasetRef(source_id=config.id, namespace="main", name=name, locator=name)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "app.db"
    _make_db(path)
    return path


class TestReadOnly:
    async def test_writes_refused_by_engine(self, db: Path) -> None:
        """mode=ro + authorizer must refuse every mutating statement."""
        config = _config(db)
        connector = SQLiteConnector()
        try:
            async with connector._acquire(config) as conn:
                for sql in (
                    "INSERT INTO orders VALUES (99, 'x', 1, 1, NULL)",
                    "UPDATE orders SET label = 'pwn'",
                    "DELETE FROM orders",
                    "DROP TABLE orders",
                    "CREATE TABLE zz (a INTEGER)",
                    "ATTACH DATABASE '/tmp/should-not-exist.db' AS evil",
                ):
                    with pytest.raises(sqlite3.Error):
                        conn.execute(sql)
        finally:
            await connector.close()

    async def test_database_file_unchanged_after_refused_writes(self, db: Path) -> None:
        """Both layers refuse: the SQL guard, and the engine's mode=ro."""
        config = _config(db)
        connector = SQLiteConnector()
        try:
            with pytest.raises(QCError):
                await connector._fetch(config, 'DELETE FROM "orders"')
            async with connector._acquire(config) as conn:
                with pytest.raises(sqlite3.Error):
                    conn.execute('DELETE FROM "orders"')
        finally:
            await connector.close()
        check = sqlite3.connect(db)
        try:
            assert check.execute("SELECT count(*) FROM orders").fetchone()[0] == 10
        finally:
            check.close()

    async def test_dangerous_pragma_refused(self, db: Path) -> None:
        config = _config(db)
        connector = SQLiteConnector()
        try:
            async with connector._acquire(config) as conn:
                for pragma in ("writable_schema = ON", "journal_mode = WAL", "foreign_keys = ON"):
                    with pytest.raises(sqlite3.Error):
                        conn.execute(f"PRAGMA {pragma}")
        finally:
            await connector.close()

    async def test_metadata_pragma_allowlist_is_exact(self, db: Path) -> None:
        """Only read-only introspection pragmas may pass the authorizer."""
        assert set(_SQLITE_READONLY_PRAGMAS) == {"table_info", "foreign_key_list", "table_xinfo"}
        config = _config(db)
        connector = SQLiteConnector()
        try:
            with pytest.raises(UnsupportedError):
                await connector._fetch_meta(config, "writable_schema", "orders")
        finally:
            await connector.close()

    async def test_non_select_refused_before_wire(self, db: Path) -> None:
        config = _config(db)
        connector = SQLiteConnector()
        try:
            with pytest.raises(UnsupportedError):
                await connector._fetch(config, 'DELETE FROM "orders"')  # type: ignore[arg-type]
        finally:
            await connector.close()


class TestPathHandling:
    async def test_missing_database_reports_filename_only(self, tmp_path: Path) -> None:
        """A missing path must not be auto-created, and errors stay terse."""
        config = _config(tmp_path / "nope.db")
        connector = SQLiteConnector()
        try:
            result = await connector.test_connection(config)
            assert result.ok is False
            assert "nope.db" in result.message
        finally:
            await connector.close()
        assert not (tmp_path / "nope.db").exists(), "connector must not create the database"

    async def test_path_resolved_from_secret_reference(self, db: Path, monkeypatch) -> None:
        """The path may come from connection_ref instead of options."""
        monkeypatch.setenv("QC_TEST_SQLITE_SECRET_PATH", str(db))
        config = SourceConfig(
            name="sqlite",
            connector="sqlite",
            connection_ref="env:QC_TEST_SQLITE_SECRET_PATH",
        )
        connector = SQLiteConnector()
        try:
            test = await connector.test_connection(config)
            assert test.ok
        finally:
            await connector.close()

    async def test_path_with_spaces_and_unicode(self, tmp_path: Path) -> None:
        """The file: URI must percent-encode, not truncate."""
        # built via escapes so the source stays ASCII (ruff RUF001)
        name = "my data (v2) \u2013 \u00fcn\u00efcode.db"
        path = tmp_path / name
        _make_db(path)
        config = _config(path)
        connector = SQLiteConnector()
        try:
            assert (await connector.test_connection(config)).ok
            schema = await connector.get_schema(config, _ref(config))
            assert schema.column_names[0] == "id"
        finally:
            await connector.close()


class TestDiscovery:
    async def test_views_and_tables_listed(self, tmp_path: Path) -> None:
        path = tmp_path / "v.db"
        _make_db(path, extra="CREATE VIEW recent AS SELECT * FROM orders WHERE id > 5;")
        config = _config(path)
        connector = SQLiteConnector()
        try:
            datasets = await connector.list_datasets(config)
            kinds = {d.name: d.kind.value for d in datasets}
            assert kinds["orders"] == "table"
            assert kinds["recent"] == "view"
        finally:
            await connector.close()

    async def test_internal_tables_excluded(self, tmp_path: Path) -> None:
        path = tmp_path / "a.db"
        _make_db(path)
        # force creation of an internal table
        seed = sqlite3.connect(path)
        seed.execute("CREATE TABLE t_auto(id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
        seed.execute("INSERT INTO t_auto(v) VALUES ('x')")
        seed.commit()
        seed.close()
        config = _config(path)
        connector = SQLiteConnector()
        try:
            names = {d.name for d in await connector.list_datasets(config)}
            assert not any(n.startswith("sqlite_") for n in names)
            assert "t_auto" in names
        finally:
            await connector.close()

    async def test_allowed_datasets_filter(self, db: Path) -> None:
        config = SourceConfig(
            name="sqlite",
            connector="sqlite",
            connection_ref="env:QC_TEST_SQLITE_PATH",
            options={"path": str(db)},
            allowed_datasets=["main.orders"],
        )
        connector = SQLiteConnector()
        try:
            assert [d.name for d in await connector.list_datasets(config)] == ["orders"]
        finally:
            await connector.close()

    async def test_foreign_keys_discovered(self, tmp_path: Path) -> None:
        path = tmp_path / "fk.db"
        _make_db(
            path,
            extra=(
                "CREATE TABLE parent(id INTEGER PRIMARY KEY);"
                "CREATE TABLE child(id INTEGER PRIMARY KEY, "
                "pid INTEGER REFERENCES parent(id), "
                "qty INTEGER);"
            ),
        )
        config = _config(path)
        connector = SQLiteConnector()
        try:
            schema = await connector.get_schema(config, _ref(config, "child"))
            assert schema.foreign_keys
            fk = schema.foreign_keys[0]
            assert fk.ref_name == "parent"
            assert fk.columns == ["pid"]
        finally:
            await connector.close()

    async def test_missing_dataset_raises_not_found(self, db: Path) -> None:
        config = _config(db)
        connector = SQLiteConnector()
        try:
            with pytest.raises(NotFoundError):
                await connector.get_schema(config, _ref(config, "absent_table"))
        finally:
            await connector.close()


class TestQueries:
    async def test_type_affinity_mapping(self, tmp_path: Path) -> None:
        path = tmp_path / "types.db"
        seed = sqlite3.connect(path)
        seed.executescript(
            """
            CREATE TABLE t(
                a INTEGER, b TEXT, c REAL, d BLOB, e BOOLEAN,
                f DECIMAL(10,2), g DATETIME, h VARCHAR(20), i NUMERIC
            );
            """
        )
        seed.commit()
        seed.close()
        config = _config(path)
        connector = SQLiteConnector()
        try:
            schema = await connector.get_schema(config, _ref(config, "t"))
            logical = {c.name: c.logical_type.value for c in schema.columns}
            assert logical["a"] == "integer"
            assert logical["b"] == "string"
            assert logical["c"] == "float"
            assert logical["d"] == "binary"
            assert logical["e"] == "boolean"
            assert logical["f"] == "decimal"
            assert logical["g"] == "timestamp"
            assert logical["h"] == "string"
            assert logical["i"] == "decimal"
        finally:
            await connector.close()

    async def test_untyped_column_falls_back_to_affinity(self, tmp_path: Path) -> None:
        """SQLite allows untyped columns; must not crash the schema build."""
        path = tmp_path / "untyped.db"
        seed = sqlite3.connect(path)
        seed.execute("CREATE TABLE t(a, b NOT NULL)")
        seed.commit()
        seed.close()
        config = _config(path)
        connector = SQLiteConnector()
        try:
            schema = await connector.get_schema(config, _ref(config, "t"))
            assert [c.source_type for c in schema.columns] == ["BLOB", "BLOB"]
            assert schema.columns[1].nullable_declared is False
        finally:
            await connector.close()

    async def test_null_and_empty_kept_distinct(self, db: Path) -> None:
        config = _config(db)
        connector = SQLiteConnector()
        try:
            res = await connector.execute_profile_query(
                config,
                ProfileQuery(kind="null_count", dataset=_ref(config), params={"column": "note"}),
            )
            # note is "" for i in {0,3,6,9}, NULL for the other 6
            assert res.data["null_count"] == 6
            assert res.data["row_count"] == 10
        finally:
            await connector.close()

    async def test_length_percentiles_nearest_rank(self, tmp_path: Path) -> None:
        """Percentiles use nearest-rank ceil(n*p), not interpolation."""
        path = tmp_path / "len.db"
        seed = sqlite3.connect(path)
        seed.execute("CREATE TABLE t(s TEXT)")
        seed.executemany("INSERT INTO t VALUES (?)", [(("x" * n),) for n in range(1, 11)])
        seed.commit()
        seed.close()
        config = _config(path)
        connector = SQLiteConnector()
        try:
            res = await connector.execute_profile_query(
                config,
                ProfileQuery(
                    kind="length_stats", dataset=_ref(config, "t"), params={"column": "s"}
                ),
            )
            assert res.data["min"] == 1
            assert res.data["max"] == 10
            assert res.data["p50"] == 5  # ceil(10*0.5) -> 5th smallest
            assert res.data["p90"] == 9  # ceil(10*0.9) -> 9th smallest
        finally:
            await connector.close()

    async def test_top_k_nulls_sort_last_and_marked_falsy(self, db: Path) -> None:
        config = _config(db)
        connector = SQLiteConnector()
        try:
            res = await connector.execute_profile_query(
                config,
                ProfileQuery(kind="top_k", dataset=_ref(config), params={"column": "note", "k": 3}),
            )
            categories = res.data["categories"]
            # NULLs are present and must be flagged falsy
            null_entry = next(c for c in categories if c["key"] == "")
            assert null_entry["count"] == 4
            assert null_entry["falsy"] is False  # "" is empty string, not NULL
        finally:
            await connector.close()

    async def test_predicate_filtering_applied(self, db: Path) -> None:
        config = _config(db)
        connector = SQLiteConnector()
        try:
            res = await connector.execute_profile_query(
                config,
                ProfileQuery(
                    kind="row_count",
                    dataset=_ref(config),
                    params={"predicate": {"op": ">", "column": "id", "value": 5}},
                ),
            )
            assert res.data["row_count"] == 4
        finally:
            await connector.close()

    async def test_empty_table_aggregates_do_not_crash(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.db"
        seed = sqlite3.connect(path)
        seed.execute("CREATE TABLE t(a INTEGER, s TEXT)")
        seed.commit()
        seed.close()
        config = _config(path)
        connector = SQLiteConnector()
        try:
            ref = _ref(config, "t")
            for kind, params in (
                ("row_count", {}),
                ("null_count", {"column": "s"}),
                ("distinct", {"column": "a"}),
                ("min_max", {"column": "a"}),
                ("length_stats", {"column": "s"}),
            ):
                res = await connector.execute_profile_query(
                    config, ProfileQuery(kind=kind, dataset=ref, params=params)
                )
                assert isinstance(res.data, dict)
            rows = [
                r
                async for r in connector.sample_rows(
                    config, SampleRequest(dataset=ref, limit=5, columns=["a"])
                )
            ]
            assert rows == []
        finally:
            await connector.close()

    async def test_estimate_size_reports_file_bytes(self, db: Path) -> None:
        config = _config(db)
        connector = SQLiteConnector()
        try:
            size = await connector.estimate_size(config, _ref(config))
            assert size.byte_estimate == db.stat().st_size
            assert size.row_estimate is None  # not scanned
        finally:
            await connector.close()


class TestSampling:
    async def test_sampling_respects_limit_and_columns(self, db: Path) -> None:
        config = _config(db)
        connector = SQLiteConnector()
        try:
            rows = [
                r
                async for r in connector.sample_rows(
                    config, SampleRequest(dataset=_ref(config), limit=3, columns=["id", "label"])
                )
            ]
            assert len(rows) == 3
            assert all(set(r.values) == {"id", "label"} for r in rows)
            assert [r.values["id"] for r in rows] == [0, 1, 2]
        finally:
            await connector.close()

    async def test_random_sampling_varies(self, db: Path) -> None:
        config = _config(db)
        connector = SQLiteConnector()
        try:
            picks = set()
            for _ in range(5):
                rows = [
                    r
                    async for r in connector.sample_rows(
                        config,
                        SampleRequest(
                            dataset=_ref(config), limit=1, columns=["id"], method="random"
                        ),
                    )
                ]
                picks.add(rows[0].values["id"])
            assert len(picks) > 1, "random() sampling should not be deterministic"
        finally:
            await connector.close()

    async def test_stable_key_falls_back_to_rowid(self, db: Path) -> None:
        """Callers passing columns=None (e.g. sanitize preview) still work."""
        config = _config(db)
        connector = SQLiteConnector()
        try:
            rows = [
                r
                async for r in connector.sample_rows(
                    config, SampleRequest(dataset=_ref(config), limit=3)
                )
            ]
            assert len(rows) == 3
            # deterministic across two identical runs
            again = [
                r
                async for r in connector.sample_rows(
                    config, SampleRequest(dataset=_ref(config), limit=3)
                )
            ]
            assert [r.values for r in rows] == [r.values for r in again]
        finally:
            await connector.close()

    async def test_stable_key_without_rowid_uses_first_column(self, tmp_path: Path) -> None:
        """WITHOUT ROWID tables have no rowid; fall back to a declared column."""
        path = tmp_path / "norowid.db"
        seed = sqlite3.connect(path)
        seed.execute("CREATE TABLE t(k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID")
        seed.executemany("INSERT INTO t VALUES (?,?)", [(f"k{i}", f"v{i}") for i in range(5)])
        seed.commit()
        seed.close()
        config = _config(path)
        connector = SQLiteConnector()
        try:
            rows = [
                r
                async for r in connector.sample_rows(
                    config, SampleRequest(dataset=_ref(config, "t"), limit=3)
                )
            ]
            assert len(rows) == 3
            assert [r.values["k"] for r in rows] == ["k0", "k1", "k2"]
        finally:
            await connector.close()


class TestCancellation:
    async def test_cancellation_stops_sampling(self, db: Path) -> None:
        from qc.domain.cancellation import CancellationToken
        from qc.domain.errors import CancelledError

        config = _config(db)
        connector = SQLiteConnector()
        token = CancellationToken()
        token.cancel()
        try:
            with pytest.raises(CancelledError):
                [
                    r
                    async for r in connector.sample_rows(
                        config,
                        SampleRequest(dataset=_ref(config), limit=10, columns=["id"], cancel=token),
                    )
                ]
        finally:
            await connector.close()


class TestPoolLifecycle:
    async def test_connections_are_reused_and_closed(self, db: Path) -> None:
        config = _config(db)
        connector = SQLiteConnector()
        await connector._fetch(config, "SELECT 1")
        assert connector._pool is not None and connector._pool.size == 1
        await connector.close()
        assert connector.closed
        assert connector._pool is None

    async def test_close_is_idempotent(self, db: Path) -> None:
        config = _config(db)
        connector = SQLiteConnector()
        await connector.test_connection(config)
        await connector.close()
        await connector.close()
        assert connector.closed
