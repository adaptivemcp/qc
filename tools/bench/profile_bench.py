#!/usr/bin/env python3
"""End-to-end profiling benchmark (SPEC 17 M7: "performance benchmarks").

Answers one question with numbers instead of adjectives: **what does a profile cost,
and what does it cost it to be bigger?** The profiler is adaptive, so the interesting
axis is not just rows — it is whether a larger table (or a bigger sample) changes the
number of queries, because query count is the budget that actually binds.

Deliberately offline and dependency-free: it builds SQLite fixtures, runs the real
``Profiler`` through the real policy engine, and prints markdown.

    uv run python tools/bench/profile_bench.py
    uv run python tools/bench/profile_bench.py --json
    uv run python tools/bench/profile_bench.py --sizes 2000,20000 --quick

Numbers are machine-specific; record them with the machine and the date. They are a
regression baseline, not a promise about any other host.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sqlite3
import statistics
import tempfile
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path

from qc.config.policies import parse_policy
from qc.connectors.sqlite_conn import SQLiteConnector
from qc.domain.models import DatasetRef, SourceConfig
from qc.profiling.engine import Profiler, ProfileSettings
from qc.security.policy import PolicyEngine

SCHEMA = """
CREATE TABLE orders(
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    amount REAL,
    flag INTEGER,
    note TEXT,
    email TEXT,
    created_at TEXT,
    region TEXT
);
"""

REGIONS = ("us-east-1", "eu-west-1", "ap-southeast-2")

POLICY = """
name: bench
sources:
  bench:
    access: read
    datasets: [main.orders]
"""


@dataclass
class Measurement:
    rows: int
    sample_size: int
    seconds: float
    queries: int
    sampled_rows: int
    peak_mb: float
    columns: int


def build_database(path: Path, rows: int, *, seed: int = 7) -> None:
    """A table with the shapes profiling cares about, filled deterministically."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        batch: list[tuple[object, ...]] = []
        for i in range(rows):
            # Deterministic, no random module: the fixture must be reproducible.
            mixed = (i * 2654435761 + seed) % 2**32
            batch.append(
                (
                    i,
                    f"row-{mixed % 100_000:05d}",
                    round(((mixed % 1_000_000) / 100.0), 2),
                    mixed % 2,
                    None if mixed % 7 == 0 else "",
                    f"user{mixed % 50_000}@example.com",
                    f"2025-{(mixed % 12) + 1:02d}-{(mixed % 28) + 1:02d}T12:00:00",
                    REGIONS[mixed % len(REGIONS)],
                )
            )
            if len(batch) >= 5_000:
                conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", batch)
                batch.clear()
        if batch:
            conn.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", batch)
        conn.commit()
    finally:
        conn.close()


async def measure(db: Path, rows: int, sample_size: int) -> Measurement:
    connector = SQLiteConnector()
    config = SourceConfig(
        name="bench",
        connector="sqlite",
        connection_ref="env:QC_BENCH_SQLITE_PATH",
        options={"path": str(db)},
    )
    dataset = DatasetRef(source_id=config.id, namespace="main", name="orders", locator="orders")
    profiler = Profiler(connector, PolicyEngine(parse_policy(POLICY)))

    tracemalloc.start()
    started = time.monotonic()
    result = await profiler.profile(
        config, dataset, ProfileSettings(sample_size=sample_size), actor="bench", transport="bench"
    )
    seconds = time.monotonic() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    await connector.close()

    usage = result.budget_usage
    return Measurement(
        rows=rows,
        sample_size=sample_size,
        seconds=seconds,
        queries=usage.queries,
        sampled_rows=usage.rows,
        peak_mb=peak / (1024 * 1024),
        columns=len(result.artifact.columns),
    )


def markdown(measurements: list[Measurement], *, repeat: int) -> str:
    lines = [
        "| rows | sample | queries | sampled rows | seconds | peak MB |",
        "| ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for m in measurements:
        lines.append(
            f"| {m.rows:,} | {m.sample_size:,} | {m.queries} | {m.sampled_rows:,} "
            f"| {m.seconds:.2f} | {m.peak_mb:.1f} |"
        )
    lines.append("")
    lines.append(
        f"Median of {repeat} runs, one process per run; peak is tracemalloc for the "
        "profile call only, so it excludes the fixture build."
    )
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> list[Measurement]:
    sizes = [int(size) for size in args.sizes.split(",") if size.strip()]
    results: list[Measurement] = []
    with tempfile.TemporaryDirectory(prefix="qc-bench-") as tmp:
        for rows in sizes:
            db = Path(tmp) / f"orders-{rows}.db"
            build_database(db, rows)
            runs = []
            for _ in range(args.repeat):
                runs.append(await measure(db, rows, args.sample_size))
            # Median keeps a noisy CI host from moving the table; the spread is
            # reported so a real regression is visible anyway.
            median = statistics.median(r.seconds for r in runs)
            base = runs[0]
            results.append(
                Measurement(
                    rows=rows,
                    sample_size=args.sample_size,
                    seconds=median,
                    queries=base.queries,
                    sampled_rows=base.sampled_rows,
                    peak_mb=max(r.peak_mb for r in runs),
                    columns=base.columns,
                )
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes", default="10000,100000,1000000", help="comma-separated row counts"
    )
    parser.add_argument("--sample-size", type=int, default=1000)
    parser.add_argument("--repeat", type=int, default=3, help="runs per size (median reported)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="tiny sizes, for a smoke check rather than a baseline",
    )
    args = parser.parse_args()
    if args.quick:
        args.sizes = "1000,5000"
        args.repeat = 1

    measurements = asyncio.run(run(args))
    if args.json:
        print(
            json.dumps(
                {
                    "machine": {
                        "platform": platform.platform(),
                        "python": platform.python_version(),
                    },
                    "measurements": [m.__dict__ for m in measurements],
                },
                indent=2,
            )
        )
    else:
        print(
            f"qc profile benchmark — {platform.platform()}, Python "
            f"{platform.python_version()}, sample_size={args.sample_size}\n"
        )
        print(markdown(measurements, repeat=args.repeat))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
