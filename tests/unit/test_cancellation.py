"""Cancellation and job-lifecycle tests (SPEC 4.3, 6.1, 12.2, 12.6, 15)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from qc.application.cancellation import REGISTRY, CancellationRegistry
from qc.application.job_runner import JobRunner, run_job
from qc.domain.cancellation import CancellationToken
from qc.domain.enums import JobStatus
from qc.domain.errors import CancelledError, InvalidArgumentsError
from qc.domain.predicate import parse_predicate, parse_predicates


class TestToken:
    def test_starts_uncancelled(self) -> None:
        t = CancellationToken()
        assert not t.cancelled
        t.raise_if_cancelled()  # no raise

    def test_cancel_sets_flag_and_raises(self) -> None:
        t = CancellationToken()
        t.cancel("user request")
        assert t.cancelled
        with pytest.raises(CancelledError) as exc:
            t.raise_if_cancelled()
        assert exc.value.code.value == "CANCELLED"
        assert "user request" in exc.value.message

    def test_reset_clears(self) -> None:
        t = CancellationToken()
        t.cancel()
        t.reset()
        assert not t.cancelled

    def test_idempotent_cancel(self) -> None:
        t = CancellationToken()
        t.cancel("first")
        t.cancel("second")
        assert t.reason == "first"  # first reason wins


class TestRegistry:
    def test_register_and_cancel(self) -> None:
        reg = CancellationRegistry()
        token = reg.register("job_1")
        assert reg.get("job_1") is token
        assert reg.cancel("job_1", "stop") is True
        assert token.cancelled
        assert reg.running() == []

    def test_cancel_unknown_is_false(self) -> None:
        reg = CancellationRegistry()
        assert reg.cancel("nope") is False

    def test_unregister(self) -> None:
        reg = CancellationRegistry()
        reg.register("j")
        reg.unregister("j")
        assert reg.get("j") is None
        assert reg.cancel("j") is False

    def test_running_lists_uncancelled(self) -> None:
        reg = CancellationRegistry()
        reg.register("a")
        reg.register("b")
        reg.cancel("b")
        assert reg.running() == ["a"]


class TestEngineCancellation:
    @pytest.fixture
    def project(self, tmp_path: Path, edge_rows: list[dict]) -> Path:
        root = tmp_path / "proj"
        (root / "data").mkdir(parents=True)
        (root / "data" / "people.jsonl").write_text(
            "\n".join(json.dumps(r) for r in edge_rows), encoding="utf-8"
        )
        (root / "policies").mkdir()
        (root / "policies" / "strict-local.yaml").write_text(
            """
name: strict-local
sources:
  demo:
    access: read
    datasets: [people.jsonl]
""",
            encoding="utf-8",
        )
        return root

    def _runtime(self, project: Path):  # type: ignore[no-untyped-def]
        from qc.application.runtime import Runtime
        from qc.application.sources import SourceRegistry
        from qc.domain.models import SourceConfig

        (project / "qc.toml").write_text("version = 1\n", encoding="utf-8")
        rt = Runtime(root=project)
        SourceRegistry(project / "sources.json").add(
            SourceConfig(
                name="demo",
                connector="fixture",
                connection_ref="env:QC_UNUSED",
                options={"root": str(project / "data")},
            )
        )
        return rt

    def test_cancelled_before_start_raises(self, project: Path) -> None:
        rt = self._runtime(project)
        from qc.cli.app import _parse_dataset_ref
        from qc.profiling.engine import Profiler, ProfileSettings

        token = CancellationToken()
        token.cancel("test")
        cfg = rt.source("demo")

        async def run() -> None:
            connector = rt.connector_for(cfg)
            try:
                profiler = Profiler(connector, rt.engine())
                settings = ProfileSettings(sample_size=10, cancel=token)
                with pytest.raises(CancelledError):
                    await profiler.profile(cfg, _parse_dataset_ref(cfg, "people.jsonl"), settings)
            finally:
                await connector.close()

        asyncio.run(run())

    def test_cancel_midway_stops_and_closes(self, project: Path) -> None:
        """A token cancelled after the first stage stops further probes."""
        rt = self._runtime(project)
        from qc.cli.app import _parse_dataset_ref
        from qc.profiling.engine import Profiler, ProfileSettings

        token = CancellationToken()
        cfg = rt.source("demo")

        async def run() -> None:
            connector = rt.connector_for(cfg)
            try:
                profiler = Profiler(connector, rt.engine())
                # cancel before profiling: engine checks per stage/column
                token.cancel("midway")
                settings = ProfileSettings(sample_size=50, cancel=token)
                with pytest.raises(CancelledError):
                    await profiler.profile(cfg, _parse_dataset_ref(cfg, "people.jsonl"), settings)
            finally:
                await connector.close()
            assert connector.closed

        asyncio.run(run())

    def test_cancel_between_sampled_rows(self, project: Path) -> None:
        """Token checked between yielded rows in the fixture connector."""
        rt = self._runtime(project)
        from qc.cli.app import _parse_dataset_ref
        from qc.connectors.base import SampleRequest

        cfg = rt.source("demo")
        token = CancellationToken()

        async def run() -> int:
            connector = rt.connector_for(cfg)
            seen = 0
            try:
                request = SampleRequest(
                    dataset=_parse_dataset_ref(cfg, "people.jsonl"),
                    limit=100,
                    cancel=token,
                )
                with pytest.raises(CancelledError):
                    async for _row in connector.sample_rows(cfg, request):
                        seen += 1
                        if seen == 2:
                            token.cancel("enough")
                return seen
            finally:
                await connector.close()

        seen = asyncio.run(run())
        assert seen == 2  # stopped right after cancellation


class TestJobRunner:
    @pytest.fixture
    def rt(self, tmp_path: Path):  # type: ignore[no-untyped-def]
        from qc.application.runtime import Runtime

        (tmp_path / "qc.toml").write_text("version = 1\n", encoding="utf-8")
        return Runtime(root=tmp_path)

    def test_success_records_completed(self, rt) -> None:  # type: ignore[no-untyped-def]
        runner = JobRunner(rt, kind="profile")

        async def op() -> str:
            return "done"

        outcome = asyncio.run(run_job(runner, op))
        assert outcome.result == "done"
        job = rt.state.get_job(runner.id)
        assert job is not None
        assert job.status == JobStatus.COMPLETED
        assert job.progress == 1.0
        assert REGISTRY.get(runner.id) is None  # released

    def test_failure_records_failed(self, rt) -> None:  # type: ignore[no-untyped-def]
        from qc.domain.errors import PolicyDeniedError

        runner = JobRunner(rt, kind="profile")

        async def op() -> None:
            raise PolicyDeniedError("nope")

        with pytest.raises(PolicyDeniedError):
            asyncio.run(run_job(runner, op))
        job = rt.state.get_job(runner.id)
        assert job is not None
        assert job.status == JobStatus.FAILED
        assert job.error_code == "POLICY_DENIED"

    def test_cancellation_records_cancelled(self, rt) -> None:  # type: ignore[no-untyped-def]
        runner = JobRunner(rt, kind="profile")

        async def op() -> None:
            runner.token.raise_if_cancelled()

        runner.token.cancel("stop")
        with pytest.raises(CancelledError):
            asyncio.run(run_job(runner, op))
        job = rt.state.get_job(runner.id)
        assert job is not None
        assert job.status == JobStatus.CANCELLED
        assert job.error_code == "CANCELLED"

    def test_registry_cancel_while_running(self, rt) -> None:  # type: ignore[no-untyped-def]
        runner = JobRunner(rt, kind="profile")
        assert REGISTRY.get(runner.id) is not None
        assert runner.cancel_from_registry("external") is True
        assert runner.token.cancelled


class TestPredicateParsing:
    def test_equality(self) -> None:
        p = parse_predicate("age=30")
        assert p == {"column": "age", "op": "=", "value": 30}

    def test_typed_coercions(self) -> None:
        assert parse_predicate("x=1.5")["value"] == 1.5
        assert parse_predicate("flag=true")["value"] is True
        assert parse_predicate("y=null")["value"] is None
        assert parse_predicate("name=abc")["value"] == "abc"

    def test_operators(self) -> None:
        assert parse_predicate("age:>=:18")["op"] == ">="
        assert parse_predicate("city:like:New%")["value"] == "New%"
        p = parse_predicate("id:in:1,2,3")
        assert p["op"] == "in"
        assert p["values"] == [1, 2, 3]

    def test_rejects_sql(self) -> None:
        for bad in ("age=1; DROP TABLE t", "x=1' OR '1'='1", "y=`boom`"):
            with pytest.raises(InvalidArgumentsError):
                parse_predicate(bad)

    def test_rejects_bad_operator(self) -> None:
        with pytest.raises(InvalidArgumentsError):
            parse_predicate("age:evil:1")

    def test_rejects_bad_column(self) -> None:
        with pytest.raises(InvalidArgumentsError):
            parse_predicate("a b=1")
        with pytest.raises(InvalidArgumentsError):
            parse_predicate("badcol=1", allowed_columns={"age"})

    def test_multiple_become_and(self) -> None:
        p = parse_predicates(["a=1", "b=2"])
        assert p["op"] == "and"
        assert len(p["clauses"]) == 2

    def test_single_stays_unwrapped(self) -> None:
        p = parse_predicates(["a=1"])
        assert p["column"] == "a"
