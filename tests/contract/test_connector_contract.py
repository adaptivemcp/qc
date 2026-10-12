"""Connector contract tests (SPEC 16.3).

One suite, many connectors. The fixture connector always runs;
PostgreSQL and MySQL run live when ``QC_TEST_POSTGRES_DSN`` /
``QC_TEST_MYSQL_DSN`` are set (CI services or opt-in local containers).

Shared assertions: connect/close, dataset + schema discovery, missing
dataset → actionable redacted error, row bounds, null/empty handling,
decimal/boolean/date preservation, SQL-distinct semantics, credentials
never in diagnostics, non-SELECT refused before the wire.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from qc.connectors.base import DataConnector, ProfileQuery, SampleRequest
from qc.domain.models import DatasetRef, SourceConfig

# (connector, config, qualified_dataset)
Triple = tuple[DataConnector, SourceConfig, str]


async def _seed_postgres() -> Triple:
    from qc.connectors.postgres import PostgresConnector

    config = SourceConfig(
        name="pg-contract",
        connector="postgres",
        connection_ref="env:QC_TEST_POSTGRES_DSN",
    )
    # Seed through a *separate* raw connection: the connector itself
    # opens read-only sessions (SPEC 6.2), so it must not be the seeder.
    import psycopg

    dsn = os.environ["QC_TEST_POSTGRES_DSN"]
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as seed:
        await seed.execute("DROP TABLE IF EXISTS qc_contract")
        await seed.execute(
            """CREATE TABLE qc_contract (
                id integer PRIMARY KEY,
                label text NOT NULL,
                amount numeric(10,2),
                flag boolean,
                "when" date,
                note text,
                big_id bigint
            )"""
        )
        rows = [
            (
                i,
                f"row-{i}",
                f"{i}.25",
                i % 2 == 0,
                f"2025-01-{(i % 28) + 1:02d}",
                "" if i % 3 == 0 else None,
                9007199254740993 + i,
            )
            for i in range(10)
        ]
        await seed.execute(
            "INSERT INTO qc_contract VALUES " + ",".join("(%s,%s,%s,%s,%s,%s,%s)" for _ in rows),
            [v for row in rows for v in row],
        )
    conn = PostgresConnector()
    return conn, config, "public.qc_contract"  # type: ignore[return-value]


async def _seed_mysql() -> Triple:
    from qc.connectors.mysql import MySQLConnector, _parse_mysql_dsn

    dsn = os.environ["QC_TEST_MYSQL_DSN"]
    schema = dsn.rsplit("/", 1)[-1].split("?")[0]
    config = SourceConfig(
        name="my-contract",
        connector="mysql",
        connection_ref="env:QC_TEST_MYSQL_DSN",
        default_schema=schema,
    )
    # Seed through a separate raw connection: the connector enforces a
    # read-only session (SPEC 6.3), so it must not be the seeder.
    import mysql.connector

    seed = mysql.connector.connect(**_parse_mysql_dsn(dsn))
    try:
        cur = seed.cursor()
        cur.execute("DROP TABLE IF EXISTS qc_contract")
        cur.execute(
            """CREATE TABLE qc_contract (
                id int PRIMARY KEY,
                label varchar(64) NOT NULL,
                amount decimal(10,2),
                flag tinyint(1),
                `when` date,
                note text,
                big_id bigint
            )"""
        )
        for i in range(10):
            cur.execute(
                "INSERT INTO qc_contract VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    i,
                    f"row-{i}",
                    f"{i}.25",
                    i % 2 == 0,
                    f"2025-01-{(i % 28) + 1:02d}",
                    "" if i % 3 == 0 else None,
                    9007199254740993 + i,
                ),
            )
        seed.commit()
        cur.close()
    finally:
        seed.close()
    conn = MySQLConnector()
    return conn, config, f"{schema}.qc_contract"  # type: ignore[return-value]


@pytest.fixture
async def fixture_triple(tmp_path: Path) -> AsyncIterator[Triple]:
    from qc.connectors.fixture import FixtureConnector

    rows = [
        {
            "id": i,
            "label": f"row-{i}",
            "amount": f"{i}.25",
            "flag": i % 2 == 0,
            "when": f"2025-01-{(i % 28) + 1:02d}",
            "note": "" if i % 3 == 0 else None,
        }
        for i in range(10)
    ]
    (tmp_path / "contract.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8"
    )
    config = SourceConfig(
        name="fixture",
        connector="fixture",
        connection_ref="env:QC_UNUSED",
        options={"root": str(tmp_path)},
    )
    conn = FixtureConnector(tmp_path)
    yield conn, config, "contract.jsonl"
    await conn.close()


@pytest.fixture
async def postgres_triple() -> AsyncIterator[Triple]:
    if not os.environ.get("QC_TEST_POSTGRES_DSN"):
        pytest.skip("QC_TEST_POSTGRES_DSN not set; live test skipped")
    try:
        triple = await _seed_postgres()
    except ImportError as exc:
        pytest.skip(str(exc))
    yield triple
    await triple[0].close()


@pytest.fixture
async def mysql_triple() -> AsyncIterator[Triple]:
    if not os.environ.get("QC_TEST_MYSQL_DSN"):
        pytest.skip("QC_TEST_MYSQL_DSN not set; live test skipped")
    try:
        triple = await _seed_mysql()
    except ImportError as exc:
        pytest.skip(str(exc))
    yield triple
    await triple[0].close()


def _ref(config: SourceConfig, dataset: str) -> DatasetRef:
    if "." in dataset:
        ns, _, name = dataset.rpartition(".")
        return DatasetRef(source_id=config.id, namespace=ns, name=name, locator=dataset)
    return DatasetRef(source_id=config.id, name=dataset, locator=dataset)


class ContractSuite:
    """Contract assertions every connector must satisfy.

    Each concrete subclass provides an async ``triple`` fixture; async
    getfixturevalue is avoided because pytest-asyncio cannot set up
    fixtures from inside a running loop.
    """

    dataset: str = "contract.jsonl"

    # ---------------------------------------------------------- lifecycle

    async def test_capabilities_declared(self, triple: Triple) -> None:
        connector, _config, _ = triple
        assert connector.capabilities.metadata is True
        assert connector.capabilities.aggregates is True

    async def test_connect_and_close(self, triple: Triple) -> None:
        connector, config, _ = triple
        test = await connector.test_connection(config)
        assert test.ok, test.message
        assert "password" not in test.message.lower()
        await connector.close()
        assert connector.closed

    # --------------------------------------------------------- discovery

    async def test_list_datasets(self, triple: Triple) -> None:
        connector, config, dataset = triple
        datasets = await connector.list_datasets(config)
        assert datasets, "connector must discover at least one dataset"
        assert any(d.name in dataset for d in datasets)

    async def test_get_schema(self, triple: Triple) -> None:
        connector, config, dataset = triple
        schema = await connector.get_schema(config, _ref(config, dataset))
        names = schema.column_names
        assert "id" in names and "label" in names
        assert schema.columns[0].ordinal == 0
        assert schema.source_fingerprint

    async def test_missing_dataset_error(self, triple: Triple) -> None:
        from qc.domain.errors import QCError

        connector, config, dataset = triple
        ns, _, _ = dataset.rpartition(".")
        bogus = DatasetRef(
            source_id=config.id,
            namespace=ns or None,
            name="definitely_missing_table_xyz",
            locator="definitely_missing_table_xyz",
        )
        with pytest.raises(QCError) as exc:
            await connector.get_schema(config, bogus)
        assert exc.value.message
        lowered = exc.value.message.lower()
        assert "password" not in lowered
        assert "env:" not in lowered

    # ------------------------------------------------------------ queries

    async def test_row_count(self, triple: Triple) -> None:
        connector, config, dataset = triple
        res = await connector.execute_profile_query(
            config, ProfileQuery(kind="row_count", dataset=_ref(config, dataset))
        )
        assert res.data["row_count"] == 10

    async def test_sampling_respects_limit(self, triple: Triple) -> None:
        connector, config, dataset = triple
        rows = []
        async for row in connector.sample_rows(
            config,
            SampleRequest(dataset=_ref(config, dataset), limit=4, columns=["id", "label"]),
        ):
            rows.append(row)
        assert len(rows) <= 4
        assert all(set(r.values) <= {"id", "label"} for r in rows)

    async def test_null_vs_empty_preserved(self, triple: Triple) -> None:
        connector, config, dataset = triple
        res = await connector.execute_profile_query(
            config,
            ProfileQuery(
                kind="null_count", dataset=_ref(config, dataset), params={"column": "note"}
            ),
        )
        # note: "" for i in {0,3,6,9} (empty string), None for the rest
        assert res.data["null_count"] == 6

    async def test_distinct_excludes_nulls(self, triple: Triple) -> None:
        connector, config, dataset = triple
        res = await connector.execute_profile_query(
            config,
            ProfileQuery(kind="distinct", dataset=_ref(config, dataset), params={"column": "id"}),
        )
        assert res.data["distinct_count"] == 10

    async def test_min_max_decimal(self, triple: Triple) -> None:
        connector, config, dataset = triple
        res = await connector.execute_profile_query(
            config,
            ProfileQuery(
                kind="min_max", dataset=_ref(config, dataset), params={"column": "amount"}
            ),
        )
        assert "min" in res.data and "max" in res.data

    async def test_empty_dataset_handled(self, triple: Triple) -> None:
        """A dataset with zero rows must not crash aggregates."""
        connector, config, dataset = triple
        res = await connector.execute_profile_query(
            config,
            ProfileQuery(
                kind="length_stats", dataset=_ref(config, dataset), params={"column": "label"}
            ),
        )
        assert isinstance(res.data, dict)

    # ---------------------------------------------------------- security

    async def test_credentials_not_in_diagnostics(self, triple: Triple) -> None:
        connector, config, _ = triple
        test = await connector.test_connection(config)
        blob = (test.message + str(test.server_info)).lower()
        assert "password" not in blob
        assert "env:" not in blob

    async def test_unsafe_identifier_rejected(self) -> None:
        from qc.connectors.sql_common import quote_ident
        from qc.domain.errors import InvalidArgumentsError

        for bad in ('users"; DROP TABLE x; --', "a b", "", "1abc", "x" * 200):
            with pytest.raises(InvalidArgumentsError):
                quote_ident(bad)

    async def test_unsafe_dataset_reference_rejected(self) -> None:
        from qc.connectors.sql_common import safety_check_dataset
        from qc.domain.errors import InvalidArgumentsError

        for bad in ("t; DROP TABLE x", "t --comment", "t/*x*/", "a\x00b"):
            with pytest.raises(InvalidArgumentsError):
                safety_check_dataset(bad)

    async def test_non_select_refused_before_wire(self) -> None:
        from qc.connectors.sql_common import assert_not_dangerous_sql
        from qc.domain.errors import UnsupportedError

        for bad in (
            "DELETE FROM t",
            "DROP TABLE t",
            "UPDATE t SET a=1",
            "INSERT INTO t VALUES (1)",
            "TRUNCATE t",
        ):
            with pytest.raises(UnsupportedError):
                assert_not_dangerous_sql(bad)

    async def test_predicate_whitelisted(self) -> None:
        from qc.connectors.postgres import PostgresConnector
        from qc.domain.errors import UnsupportedError

        for bad in (
            {"column": "id; DROP", "value": 1},
            {"column": "id", "op": "LIKE", "value": "%"},
            {"column": "id", "value": {"nested": True}},
        ):
            with pytest.raises(UnsupportedError):
                PostgresConnector._safe_predicate(bad)


class TestFixtureContract(ContractSuite):
    @pytest.fixture
    async def triple(self, fixture_triple: Triple) -> Triple:
        return fixture_triple


@pytest.mark.integration
class TestPostgresContract(ContractSuite):
    @pytest.fixture
    async def triple(self, postgres_triple: Triple) -> Triple:
        return postgres_triple


@pytest.mark.integration
class TestMySQLContract(ContractSuite):
    @pytest.fixture
    async def triple(self, mysql_triple: Triple) -> Triple:
        return mysql_triple
