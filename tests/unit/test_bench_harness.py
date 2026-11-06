"""The profiling benchmark harness itself (tools/bench/profile_bench.py).

The numbers are recorded in `docs/operations.md`; this only guards that the harness
still measures what it claims — it builds a real fixture, profiles it through the real
Profiler, and reports queries/rows/time. A broken harness would silently produce a
plausible table, which is worse than no table.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

BENCH = Path(__file__).resolve().parents[2] / "tools" / "bench" / "profile_bench.py"


def _bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("qc_profile_bench", BENCH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # A dataclass resolves its module through `sys.modules` at class-creation time,
    # so the module has to be registered before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_harness_profiles_a_real_fixture(tmp_path: Path) -> None:
    bench = _bench()
    db = tmp_path / "orders.db"

    bench.build_database(db, 500)
    measurement = bench.asyncio.run(bench.measure(db, 500, 100))

    assert measurement.columns == 8
    assert measurement.queries > 0
    assert measurement.sampled_rows > 0
    assert measurement.seconds >= 0
    # Nothing should have materialized the whole table.
    assert measurement.peak_mb < 50


def test_fixture_is_deterministic(tmp_path: Path) -> None:
    bench = _bench()
    first, second = tmp_path / "a.db", tmp_path / "b.db"

    bench.build_database(first, 200)
    bench.build_database(second, 200)

    def rows(path: Path) -> list[tuple]:
        conn = sqlite3.connect(path)
        try:
            return conn.execute("SELECT * FROM orders ORDER BY id").fetchall()
        finally:
            conn.close()

    assert rows(first) == rows(second)


def test_markdown_reports_every_measurement() -> None:
    bench = _bench()
    measurement = bench.Measurement(
        rows=1000, sample_size=100, seconds=0.5, queries=7, sampled_rows=100, peak_mb=0.4, columns=8
    )

    table = bench.markdown([measurement], repeat=1)

    assert "| 1,000 | 100 | 7 |" in table
    assert "Median of 1 runs" in table
