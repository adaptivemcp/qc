"""Cooperative cancellation token (SPEC 4.3, 6.1, 15).

Lives in the domain layer so both connectors and application services
can share it without a layering violation. The process-wide registry of
running jobs lives in :mod:`qc.application.cancellation`.
"""

from __future__ import annotations

import threading

from qc.domain.errors import CancelledError


class CancellationToken:
    """Thread-safe cancellation flag, safe to share across event loops.

    Cancellation is cooperative: work polls :meth:`raise_if_cancelled`
    between units of work, and owners close resources in ``finally``.
    """

    __slots__ = ("_event", "_reason")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = "cancelled by request"

    def cancel(self, reason: str | None = None) -> None:
        # first cancellation wins: the reason records why it was stopped
        if not self._event.is_set() and reason:
            self._reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def reset(self) -> None:
        self._event.clear()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledError(self._reason)


__all__ = ["CancellationToken"]
