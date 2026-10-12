"""Shared test fixtures: edge-case datasets (SPEC 16.1) and live
database seeding.

Live seeds live here so any test module can use them as fixtures —
test modules never import each other (``tests`` is not an importable
package in CI).
"""

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from qc.connectors.base import DataConnector
    from qc.domain.models import SourceConfig

#: (connector, config, qualified dataset name)
Triple = tuple["DataConnector", "SourceConfig", str]

# --------------------------------------------------------- live DB seeding


async def _seed_postgres() -> Triple:
    """Seed the contract table through a *separate* raw connection.

    The connector opens read-only sessions (SPEC 6.2), so it must never
    be the seeder.
    """
    import psycopg

    from qc.connectors.postgres import PostgresConnector
    from qc.domain.models import SourceConfig

    config = SourceConfig(
        name="pg-contract",
        connector="postgres",
        connection_ref="env:QC_TEST_POSTGRES_DSN",
    )
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
    return PostgresConnector(), config, "public.qc_contract"  # type: ignore[return-value]


async def _seed_mysql() -> Triple:
    """Seed the contract table; schema is derived from the DSN.

    Hardcoding a schema name breaks CI, whose database differs from a
    local one (SPEC 16.3 connector contract).
    """
    import mysql.connector

    from qc.connectors.mysql import MySQLConnector, _parse_mysql_dsn
    from qc.domain.models import SourceConfig
    from qc.security.secrets import resolve_secret

    dsn = os.environ["QC_TEST_MYSQL_DSN"]
    schema = dsn.rsplit("/", 1)[-1].split("?")[0]
    config = SourceConfig(
        name="my-contract",
        connector="mysql",
        connection_ref="env:QC_TEST_MYSQL_DSN",
        default_schema=schema,
    )
    seed = mysql.connector.connect(**_parse_mysql_dsn(resolve_secret("env:QC_TEST_MYSQL_DSN")))
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
    return MySQLConnector(), config, f"{schema}.qc_contract"  # type: ignore[return-value]


async def _seed_sqlite() -> Triple:
    """Seed the contract table into a temporary SQLite file.

    SQLite needs no server, so this runs everywhere (no live marker).
    """
    import sqlite3
    import tempfile

    from qc.connectors.sqlite_conn import SQLiteConnector
    from qc.domain.models import SourceConfig

    path = Path(tempfile.mkdtemp()) / "contract.db"
    seed = sqlite3.connect(path)
    try:
        seed.execute(
            """CREATE TABLE qc_contract (
                id INTEGER PRIMARY KEY,
                label TEXT NOT NULL,
                amount REAL,
                flag INTEGER,
                "when" TEXT,
                note TEXT,
                big_id INTEGER
            )"""
        )
        for i in range(10):
            seed.execute(
                "INSERT INTO qc_contract VALUES (?,?,?,?,?,?,?)",
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
    finally:
        seed.close()
    config = SourceConfig(
        name="sqlite-contract",
        connector="sqlite",
        connection_ref="env:QC_TEST_SQLITE_PATH",
        options={"path": str(path)},
    )
    return SQLiteConnector(), config, "main.qc_contract"  # type: ignore[return-value]


@pytest.fixture
async def sqlite_triple() -> Triple:
    triple = await _seed_sqlite()
    yield triple
    await triple[0].close()


@pytest.fixture
async def postgres_triple() -> Triple:
    if not os.environ.get("QC_TEST_POSTGRES_DSN"):
        pytest.skip("QC_TEST_POSTGRES_DSN not set; live test skipped")
    try:
        triple = await _seed_postgres()
    except ImportError as exc:
        pytest.skip(str(exc))
    yield triple
    await triple[0].close()


@pytest.fixture
async def mysql_triple() -> Triple:
    if not os.environ.get("QC_TEST_MYSQL_DSN"):
        pytest.skip("QC_TEST_MYSQL_DSN not set; live test skipped")
    try:
        triple = await _seed_mysql()
    except ImportError as exc:
        pytest.skip(str(exc))
    yield triple
    await triple[0].close()


@pytest.fixture
def edge_rows() -> list[dict]:
    """Rows covering the null/falsy/type edge cases from SPEC 7.5."""
    return [
        # id: zero-padded identifier, unique
        {
            "id": "00123",
            "email": "alice@example.com",
            "age": 34,
            "score": 0.5,
            "active": True,
            "note": "hello world",
            "country": "US",
            "signup": "2025-01-05",
            "balance": "10.50",
            "ref_code": "A1",
            "phone": "+1 555 123 4567",
        },
        {
            "id": "00124",
            "email": "bob@example.com",
            "age": 0,
            "score": 0.0,
            "active": False,
            "note": "",
            "country": "US",
            "signup": "2025-02-10",
            "balance": "0.00",
            "ref_code": "A2",
            "phone": "+1 555 987 6543",
        },
        {
            "id": "00125",
            "email": "carol@example.com",
            "age": None,
            "score": None,
            "active": False,
            "note": "   ",
            "country": "CA",
            "signup": "2025-03-15",
            "balance": "-1",
            "ref_code": "A1",
            "phone": "+1 555 111 2222",
        },
        {
            "id": "00126",
            "email": "dave@example.com",
            "age": 29,
            "score": 0.9,
            "active": True,
            "note": "some longer free text here with many words",
            "country": "US",
            "signup": "2025-04-01",
            "balance": "99.99",
            "ref_code": "B1",
            "phone": "+44 20 7946 0958",
        },
        {
            "id": "00127",
            "email": "erin@example.com",
            "age": None,
            "score": 0.1,
            "active": True,
            "note": "null",
            "country": "GB",
            "signup": "2025-05-20",
            "balance": "1234.56",
            "ref_code": "B1",
            "phone": "+1 555 333 4444",
        },
        {
            "id": "00128",
            "email": "frank@example.com",
            "age": 41,
            "score": 0.75,
            "active": False,
            "note": "another note",
            "country": "US",
            "signup": "2025-06-30",
            "balance": "-9999",
            "ref_code": "C1",
            "phone": "+1 555 654 3210",
        },
        {
            "id": "00129",
            "email": "grace@example.com",
            "age": None,
            "score": None,
            "active": True,
            "note": "N/A",
            "country": "CA",
            "signup": "2025-07-04",
            "balance": "77.77",
            "ref_code": "C1",
            "phone": "+1 555 000 1111",
        },
        {
            "id": "00130",
            "email": "heidi@example.com",
            "age": 0,
            "score": 0.25,
            "active": False,
            "note": "free text again here okay",
            "country": "GB",
            "signup": "2025-08-15",
            "balance": "0.01",
            "ref_code": "A2",
            "phone": "+1 555 222 3333",
        },
    ]


@pytest.fixture
def fixture_root(tmp_path: Path, edge_rows: list[dict]) -> Path:
    root = tmp_path / "fixtures"
    root.mkdir()
    p = root / "people.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in edge_rows), encoding="utf-8")
    # null-heavy sparse dataset
    sparse = root / "sparse.jsonl"
    sparse.write_text(
        "\n".join(json.dumps({"a": None, "b": None, "c": "x"}) for _ in range(5)),
        encoding="utf-8",
    )
    # empty dataset
    (root / "empty.jsonl").write_text("", encoding="utf-8")
    return root
