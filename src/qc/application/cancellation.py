"""Process-wide registry of cancellable jobs (SPEC 12.6, 15).

Maps job ids to :class:`~qc.domain.cancellation.CancellationToken` so
MCP ``cancel_job`` and CLI signal handlers can stop a running operation
without holding a reference to its coroutine.
"""

from __future__ import annotations

import threading

from qc.domain.cancellation import CancellationToken


class CancellationRegistry:
    """Thread-safe registry; a cancellation from any thread is observed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, CancellationToken] = {}

    def register(self, job_id: str) -> CancellationToken:
        token = CancellationToken()
        with self._lock:
            self._tokens[job_id] = token
        return token

    def get(self, job_id: str) -> CancellationToken | None:
        with self._lock:
            return self._tokens.get(job_id)

    def cancel(self, job_id: str, reason: str | None = None) -> bool:
        """Cancel a registered job; returns False if it is not running."""
        with self._lock:
            token = self._tokens.get(job_id)
        if token is None:
            return False
        token.cancel(reason)
        return True

    def unregister(self, job_id: str) -> None:
        with self._lock:
            self._tokens.pop(job_id, None)

    def running(self) -> list[str]:
        with self._lock:
            return [jid for jid, tok in self._tokens.items() if not tok.cancelled]


#: Process-wide registry shared by the CLI and MCP server.
REGISTRY = CancellationRegistry()


__all__ = ["REGISTRY", "CancellationRegistry"]
