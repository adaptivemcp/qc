#!/usr/bin/env python3
"""Sanitization and generation throughput (SPEC 17 M7: "performance benchmarks").

The profiling half lives in `profile_bench.py` and answers "what does a profile cost".
This answers the other half: once a dataset is profiled, **how fast do the rows move** —
through per-column sanitization, and through synthesis + validation + write.

Both stages run the real application paths (`preview_rows`, `profile`, `create_plan`,
`run_generation`) against a real project root, a real policy file and a real SQLite
source. No mocks: a throughput number from a mocked writer would be a number about the
mock.

    uv run python tools/bench/throughput_bench.py
    uv run python tools/bench/throughput_bench.py --sizes 5000,20000 --repeat 5
    uv run python tools/bench/throughput_bench.py --json

Numbers are machine-specific; record them with the machine and the date. They are a
regression baseline, not a promise about any other host.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path

from qc.application.generation import create_plan, plan_path, run_generation
from qc.application.runtime import Runtime
from qc.application.sanitize import preview_rows
from qc.artifacts.profiles import save_profile
from qc.domain.models import DatasetRef, SourceConfig
from qc.profiling.engine import Profiler, ProfileSettings

# The fixture builder is shared with the profiling bench; loading it by path avoids
# turning `tools/bench` into a package (nothing else imports it).
_spec = importlib.util.spec_from_file_location(
    "profile_bench", Path(__file__).with_name("profile_bench.py")
)
assert _spec and _spec.loader
_fixtures = importlib.util.module_from_spec(_spec)
# A dataclass resolves its module through `sys.modules` at class-creation time.
sys.modules[_spec.name] = _fixtures
_spec.loader.exec_module(_fixtures)
build_database = _fixtures.build_database

#: Same shape as the profiling fixture, without a primary key and with deliberate
#: duplicate ids. `unique = distinct == row_count` in the profiler, so a sequential-key
#: table makes generation fail its uniqueness constraint rather than measure throughput —
#: and this bench is about rows per second, not about a hostile key.
TRANSFORM_SCHEMA = """
CREATE TABLE orders(
    id INTEGER,
    label TEXT NOT NULL,
    amount REAL,
    flag INTEGER,
    note TEXT,
    email TEXT,
    created_at TEXT,
    region TEXT
);
"""


def build_transform_database(path: Path, rows: int) -> None:
    """A table with the profiling shapes and values a sanitizer cares about."""
    import sqlite3

    regions = ("us-east-1", "eu-west-1", "ap-southeast-2")
    conn = sqlite3.connect(path)
    try:
        conn.executescript(TRANSFORM_SCHEMA)
        batch: list[tuple[object, ...]] = []
        distinct_ids = max(1, int(rows * 0.9))
        for i in range(rows):
            mixed = (i * 2654435761 + 7) % 2**32
            batch.append(
                (
                    i % distinct_ids,
                    f"row-{mixed % 100_000:05d}",
                    round((mixed % 1_000_000) / 100.0, 2),
                    mixed % 2,
                    None if mixed % 7 == 0 else "",
                    f"user{mixed % 50_000}@example.com",
                    f"2025-{(mixed % 12) + 1:02d}-{(mixed % 28) + 1:02d}T12:00:00",
                    regions[mixed % len(regions)],
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


POLICY = """
version: 1
name: strict-local
description: "benchmark policy: read, transform, generate"
budget:
  max_rows: 200000
  max_queries: 1000
  max_runtime_seconds: 300
sources:
  bench:
    access: read
    datasets: [main.orders]
    columns:
      email: mask
      note: redact
      label: keep
      amount: keep
"""


@dataclass
class Throughput:
    stage: str
    rows: int
    seconds: float
    rows_per_second: float
    peak_mb: float


def project(root: Path, db: Path) -> Runtime:
    """A real project: policy file, source registry, state DB — nothing stubbed."""
    rt = Runtime(root=root)
    # `run_generation` uses the project default policy, so the bench policy *is* the
    # default one — as a real project configures it.
    (rt.paths.policies / "strict-local.yaml").write_text(POLICY, encoding="utf-8")
    rt.sources.add(
        SourceConfig(
            name="bench",
            connector="sqlite",
            connection_ref="env:QC_BENCH_SQLITE_PATH",
            options={"path": str(db)},
        )
    )
    return rt


async def measure_sanitize(root: Path, db: Path, rows: int) -> Throughput:
    rt = project(root, db)
    tracemalloc.start()
    started = time.monotonic()
    result = await preview_rows(
        rt,
        source="bench",
        dataset="main.orders",
        policy_name="strict-local",
        limit=rows,
        inline=False,
    )
    seconds = time.monotonic() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    produced = len(result.get("rows", []))
    return Throughput(
        stage="sanitize",
        rows=produced,
        seconds=seconds,
        rows_per_second=produced / seconds if seconds else 0.0,
        peak_mb=peak / (1024 * 1024),
    )


async def measure_generate(root: Path, db: Path, rows: int) -> Throughput:
    rt = project(root, db)
    # Profile first: generation is planned from a saved profile, as it is in the CLI.
    from qc.connectors.registry import build_connector

    cfg = rt.source("bench")
    connector = build_connector(cfg, runtime=rt)
    try:
        result = await Profiler(connector, rt.engine(), ceiling=rt.ceiling).profile(
            cfg,
            DatasetRef(source_id=cfg.id, namespace="main", name="orders", locator="orders"),
            ProfileSettings(sample_size=1000),
            actor="bench",
            transport="bench",
        )
    finally:
        await connector.close()
    saved = save_profile(rt.paths.profiles, result.artifact)
    rt.state.index_profile(result.artifact, saved)
    plan = create_plan(rt, profile_ref=result.artifact.profile_id, rows=rows)

    output = rt.paths.output / "synthetic.jsonl"
    tracemalloc.start()
    started = time.monotonic()
    outcome = await run_generation(rt, plan_file=plan_path(rt, plan), output=output)
    seconds = time.monotonic() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    written = sum(1 for _ in output.open(encoding="utf-8")) if output.exists() else 0
    return Throughput(
        stage=f"generate ({outcome.get('status', 'ok')})",
        rows=written,
        seconds=seconds,
        rows_per_second=written / seconds if seconds and written else 0.0,
        peak_mb=peak / (1024 * 1024),
    )


def markdown(measurements: list[Throughput], repeat: int) -> str:
    lines = [
        "| stage | rows | seconds | rows/sec | peak MB |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for m in measurements:
        lines.append(
            f"| {m.stage} | {m.rows:,} | {m.seconds:.2f} | {m.rows_per_second:,.0f} "
            f"| {m.peak_mb:.1f} |"
        )
    lines.append("")
    lines.append(f"Median of {repeat} runs, one process per run, one SQLite fixture.")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> list[Throughput]:
    sizes = [int(size) for size in args.sizes.split(",") if size.strip()]
    results: list[Throughput] = []
    with tempfile.TemporaryDirectory(prefix="qc-throughput-") as tmp:
        tmp_path = Path(tmp)
        for rows in sizes:
            db = tmp_path / f"orders-{rows}.db"
            build_transform_database(db, rows)
            for stage, measure in (
                ("sanitize", measure_sanitize),
                ("generate", measure_generate),
            ):
                runs = [
                    await measure(tmp_path / f"run-{stage}-{rows}-{index}", db, rows)
                    for index in range(args.repeat)
                ]
                median = statistics.median(r.seconds for r in runs)
                results.append(
                    Throughput(
                        stage=f"{stage} {rows:,}",
                        rows=runs[0].rows,
                        seconds=median,
                        rows_per_second=runs[0].rows / median if median else 0.0,
                        peak_mb=max(r.peak_mb for r in runs),
                    )
                )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="5000,20000", help="comma-separated row counts")
    parser.add_argument("--repeat", type=int, default=3, help="runs per size (median reported)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="tiny sizes, for a smoke check rather than a baseline",
    )
    args = parser.parse_args()
    if args.quick:
        args.sizes = "500,1000"
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
            f"qc throughput benchmark — {platform.platform()}, Python {platform.python_version()}\n"
        )
        print(markdown(measurements, repeat=args.repeat))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
