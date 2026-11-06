"""The sanitize/generate throughput harness (`tools/bench/throughput_bench.py`).

The numbers are recorded in `docs/operations.md`; this only guards that the harness still
measures what it claims — real policy, real project root, real SQLite source, real
application paths. A bench that silently measured a mock would produce a plausible table,
which is worse than no table.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

BENCH = Path(__file__).resolve().parents[2] / "tools" / "bench" / "throughput_bench.py"


def _bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("qc_throughput_bench", BENCH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # A dataclass resolves its module through `sys.modules` at class-creation time.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_sanitize_measures_the_real_transform_path(tmp_path: Path) -> None:
    bench = _bench()
    db = tmp_path / "orders.db"
    bench.build_transform_database(db, 200)

    measurement = bench.asyncio.run(bench.measure_sanitize(tmp_path / "project", db, 200))

    assert measurement.stage == "sanitize"
    assert measurement.rows == 200
    assert measurement.seconds >= 0
    assert measurement.rows_per_second > 0


def test_generate_measures_synthesis_and_write(tmp_path: Path) -> None:
    bench = _bench()
    db = tmp_path / "orders.db"
    bench.build_transform_database(db, 100)

    measurement = bench.asyncio.run(bench.measure_generate(tmp_path / "project", db, 100))

    assert measurement.stage.startswith("generate")
    assert measurement.rows > 0
    assert measurement.rows_per_second > 0


def test_fixture_has_duplicate_ids_so_generation_is_not_a_uniqueness_test(
    tmp_path: Path,
) -> None:
    import sqlite3

    bench = _bench()
    db = tmp_path / "orders.db"
    bench.build_transform_database(db, 100)

    conn = sqlite3.connect(db)
    try:
        total, distinct = conn.execute("SELECT COUNT(*), COUNT(DISTINCT id) FROM orders").fetchone()
    finally:
        conn.close()

    assert total == 100
    assert distinct < total


def test_markdown_lists_every_stage() -> None:
    bench = _bench()
    rows = [
        bench.Throughput("sanitize 10", 10, 0.5, 20.0, 0.2),
        bench.Throughput("generate 10", 10, 1.0, 10.0, 1.5),
    ]

    table = bench.markdown(rows, repeat=3)

    assert "| sanitize 10 | 10 |" in table
    assert "| generate 10 | 10 |" in table
    assert "Median of 3 runs" in table
