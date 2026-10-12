"""Bounded async connection pool (SPEC 6.1).

A small, dependency-free pool used by SQL connectors so
"connection pooling with configurable limits" is a real, configurable
capability rather than a single reused connection.

Design:
- ``min_size`` connections may be opened eagerly; ``max_size`` bounds
  concurrency.
- checkout blocks on a semaphore; ``release`` returns the connection.
- ``close()`` drains and closes every pooled connection.
- A connection that raises while checked out is discarded, not reused.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any


class PoolClosedError(RuntimeError):
    """Raised when a connection is requested from a closed pool."""


class AsyncConnectionPool[ConnT]:
    """A bounded pool of async connections."""

    def __init__(
        self,
        factory: Callable[[], Awaitable[ConnT]],
        closer: Callable[[ConnT], Awaitable[None]],
        *,
        min_size: int = 0,
        max_size: int = 4,
    ) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        if min_size < 0 or min_size > max_size:
            raise ValueError("min_size must be within [0, max_size]")
        self._factory = factory
        self._closer = closer
        self.min_size = min_size
        self.max_size = max_size
        self._sem = asyncio.Semaphore(max_size)
        self._free: list[ConnT] = []
        self._all: set[int] = set()  # ids of live connections
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def size(self) -> int:
        """Number of live connections currently pooled or checked out."""
        return len(self._all)

    async def _open(self) -> ConnT:
        conn = await self._factory()
        self._all.add(id(conn))
        return conn

    @asynccontextmanager
    async def connection(self) -> Any:
        """Check out a connection; discards it if the body raises."""
        if self._closed:
            raise PoolClosedError("pool is closed")
        await self._sem.acquire()
        conn: ConnT | None = None
        try:
            async with self._lock:
                if self._free:
                    conn = self._free.pop()
            if conn is None:
                try:
                    conn = await self._open()
                except Exception:
                    self._sem.release()
                    raise
            yield conn
        except BaseException:
            # a connection that errored may be in an unknown state: drop it
            if conn is not None:
                await self._discard(conn)
                conn = None
            raise
        finally:
            if conn is not None:
                async with self._lock:
                    self._free.append(conn)
            self._sem.release()

    async def _discard(self, conn: ConnT) -> None:
        self._all.discard(id(conn))
        with contextlib.suppress(Exception):
            await self._closer(conn)

    async def warmup(self) -> None:
        """Open ``min_size`` connections eagerly."""
        while self.size < self.min_size:
            conn = await self._open()
            async with self._lock:
                self._free.append(conn)

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            pending, self._free = self._free, []
        for conn in pending:
            await self._discard(conn)


__all__ = ["AsyncConnectionPool", "PoolClosedError"]
