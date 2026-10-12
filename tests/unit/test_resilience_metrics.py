"""Transient retry/backoff tests (SPEC 6.1) and metrics tests (SPEC 15)."""

from __future__ import annotations

import asyncio
import time

import pytest

from qc.connectors.resilience import RetryPolicy, TransientError, is_transient, with_retry
from qc.domain.cancellation import CancellationToken
from qc.domain.errors import CancelledError, NotFoundError, SourceError
from qc.observability.metrics import MetricsRecorder, OperationMetrics


class TestClassification:
    def test_transient_error_marker(self) -> None:
        assert is_transient(TransientError("blip")) is True

    def test_network_and_deadlock_messages(self) -> None:
        assert is_transient(ConnectionResetError("connection reset by peer"))
        assert is_transient(RuntimeError("deadlock detected"))
        assert is_transient(RuntimeError("could not serialize access"))

    def test_http_status(self) -> None:
        e = TransientError("throttled", status_code=429)
        assert is_transient(e)
        e5 = TransientError("boom", status_code=503)
        assert is_transient(e5)

    def test_non_transient(self) -> None:
        assert is_transient(NotFoundError("missing")) is False
        assert is_transient(ValueError("bad input")) is False
        assert is_transient(SourceError("permission denied")) is False


class TestRetryPolicy:
    def test_backoff_is_bounded_and_increasing(self) -> None:
        policy = RetryPolicy(base_delay=0.1, max_delay=1.0, jitter=0.0)
        assert policy.delay_for(0) == pytest.approx(0.1)
        assert policy.delay_for(1) == pytest.approx(0.2)
        assert policy.delay_for(2) == pytest.approx(0.4)
        assert policy.delay_for(10) == pytest.approx(1.0)  # capped

    def test_jitter_adds_noise(self) -> None:
        policy = RetryPolicy(base_delay=1.0, max_delay=10.0, jitter=0.5)
        d = policy.delay_for(0)
        assert 1.0 <= d <= 1.5


class TestWithRetry:
    def test_succeeds_first_try(self) -> None:
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        policy = RetryPolicy(attempts=3, base_delay=0.0)
        assert asyncio.run(with_retry(op, policy)) == "ok"
        assert calls == 1

    def test_retries_transient_until_success(self) -> None:
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise TransientError("blip")
            return "ok"

        retries: list[int] = []
        policy = RetryPolicy(attempts=3, base_delay=0.0)
        result = asyncio.run(with_retry(op, policy, on_retry=lambda n, e: retries.append(n)))
        assert result == "ok"
        assert calls == 3
        assert retries == [1, 2]

    def test_gives_up_after_attempts(self) -> None:
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            raise TransientError("always")

        policy = RetryPolicy(attempts=3, base_delay=0.0)
        with pytest.raises(TransientError):
            asyncio.run(with_retry(op, policy))
        assert calls == 3

    def test_does_not_retry_non_transient(self) -> None:
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            raise NotFoundError("missing")

        policy = RetryPolicy(attempts=5, base_delay=0.0)
        with pytest.raises(NotFoundError):
            asyncio.run(with_retry(op, policy))
        assert calls == 1  # no wasted retries

    def test_cancellation_not_retried(self) -> None:
        calls = 0
        token = CancellationToken()

        async def op() -> str:
            nonlocal calls
            calls += 1
            token.cancel("stop")
            raise CancelledError("stop")

        policy = RetryPolicy(attempts=5, base_delay=0.0)
        with pytest.raises(CancelledError):
            asyncio.run(with_retry(op, policy, cancel=token))
        assert calls == 1

    def test_cancel_token_checked_before_attempt(self) -> None:
        token = CancellationToken()
        token.cancel("already")

        async def op() -> str:
            raise AssertionError("should not run")

        with pytest.raises(CancelledError):
            asyncio.run(with_retry(op, RetryPolicy(attempts=3, base_delay=0.0), cancel=token))

    def test_deadline_stops_retrying(self) -> None:
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            raise TransientError("blip")

        # deadline shorter than the first backoff window => give up fast
        policy = RetryPolicy(attempts=5, base_delay=1.0, max_delay=5.0, deadline_seconds=0.01)
        with pytest.raises(TransientError):
            asyncio.run(with_retry(op, policy))
        assert calls == 1


class TestMetrics:
    def test_counters_and_health(self) -> None:
        m = OperationMetrics(operation="profile")
        m.queries = 5
        m.rows_returned = 100
        m.bytes_processed = 2048
        m.retries = 2
        d = m.as_dict()
        assert d["queries"] == 5
        assert d["rows_returned"] == 100
        assert d["retries"] == 2

    def test_budget_utilisation(self) -> None:
        m = OperationMetrics(operation="profile")
        m.queries = 50
        m.rows_scanned = 100
        util = m.budget_utilisation(
            {"max_queries": 100, "max_rows": 200, "max_bytes": None, "max_runtime_seconds": None}
        )
        assert util["queries"] == 0.5
        assert util["rows"] == 0.5
        assert util["bytes"] == 0.0

    def test_recorder_totals_and_ring(self) -> None:
        rec = MetricsRecorder(keep=2)
        for i in range(3):
            m = OperationMetrics(operation="profile")
            m.queries = i + 1
            rec.record(m)
        assert len(rec.recent()) == 2  # ring keeps last 2
        totals = rec.totals()
        assert totals["queries"] == 2 + 3  # last two m.queries = 2 and 3

    def test_elapsed_measured(self) -> None:
        m = OperationMetrics(operation="profile")
        time.sleep(0.01)
        m.stop()
        assert m.elapsed_seconds >= 0.01
