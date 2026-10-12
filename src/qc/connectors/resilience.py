"""Transient-failure retry with bounded exponential backoff + jitter
(SPEC 6.1: "Retry only transient, idempotent operations").

Only reads are retried. The helper respects an overall deadline and a
cooperative cancellation token so retries never outlive the request.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from qc.domain.cancellation import CancellationToken
from qc.domain.errors import CancelledError


class TransientError(Exception):
    """Marker for retry-safe failures raised inside connectors.

    Carries an optional HTTP status so :func:`is_transient` can classify
    it even after wrapping.
    """

    retryable = True

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry parameters."""

    attempts: int = 3  # total tries, including the first
    base_delay: float = 0.2
    max_delay: float = 5.0
    jitter: float = 0.1  # fraction of the delay added as noise
    deadline_seconds: float | None = None

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff for a 0-based retry attempt."""
        raw: float = min(self.base_delay * (2.0**attempt), self.max_delay)
        noise: float = raw * self.jitter * random.random()
        return raw + noise


def is_transient(exc: BaseException) -> bool:
    """Heuristic classification of transient, retry-safe failures.

    Reads are idempotent, so connection resets, deadlocks, serialization
    failures, and explicit rate-limit/5xx signals are retried. Validation
    and policy errors never are.
    """
    # explicit marker set by connectors on rate limiting
    if getattr(exc, "retryable", False):
        return True
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    transient_markers = (
        "operationalerror",
        "interfaceerror",
        "timeout",
        "timed out",
        "connection reset",
        "connection refused",
        "connection aborted",
        "deadlock",
        "serialization",
        "could not serialize",
        "too many connections",
        "temporarily unavailable",
        "server closed the connection",
        "broken pipe",
        "eof occurred",
    )
    if any(m in name for m in transient_markers):
        return True
    if any(m in text for m in transient_markers):
        return True
    # HTTP 429 / 5xx mapped by sheet connectors
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    return bool(isinstance(status, int) and (status == 429 or 500 <= status < 600))


async def with_retry[T](
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    cancel: CancellationToken | None = None,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Run ``operation`` with bounded retries on transient failures.

    ``on_retry(attempt, exc)`` is invoked before each backoff sleep so
    callers can count retries for metrics (SPEC 15).
    """
    loop = asyncio.get_running_loop()
    start = loop.time()
    last_exc: BaseException | None = None
    for attempt in range(policy.attempts):
        if cancel is not None:
            cancel.raise_if_cancelled()
        try:
            return await operation()
        except CancelledError:
            raise
        except BaseException as exc:
            last_exc = exc
            exhausted = attempt >= policy.attempts - 1
            if exhausted or not is_transient(exc):
                raise
            delay = policy.delay_for(attempt)
            if policy.deadline_seconds is not None:
                remaining = policy.deadline_seconds - (loop.time() - start)
                if remaining <= delay:
                    raise
                delay = min(delay, remaining)
            if on_retry is not None:
                on_retry(attempt + 1, exc)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


__all__ = ["RetryPolicy", "is_transient", "with_retry"]
