"""Typed predicate filtering (SPEC 11.2, 16.5) and connection pool (6.1)."""

from __future__ import annotations

import asyncio

import pytest

from qc.connectors.pool import AsyncConnectionPool, PoolClosedError
from qc.domain.predicate import matches_predicate, matches_row, parse_predicates


class TestInMemoryMatching:
    def test_equality_numeric_coercion(self) -> None:
        assert matches_predicate(30, {"column": "age", "op": "=", "value": 30})
        assert matches_predicate(30.0, {"column": "age", "op": "=", "value": 30})
        assert matches_predicate("30", {"column": "age", "op": "=", "value": 30})

    def test_null_equality(self) -> None:
        assert matches_predicate(None, {"column": "c", "op": "=", "value": None})
        assert not matches_predicate(0, {"column": "c", "op": "=", "value": None})

    def test_ordering(self) -> None:
        assert matches_predicate(18, {"column": "age", "op": ">=", "value": 18})
        assert not matches_predicate(17, {"column": "age", "op": ">=", "value": 18})

    def test_in(self) -> None:
        p = {"column": "r", "op": "in", "values": ["US", "EU"]}
        assert matches_predicate("US", p)
        assert not matches_predicate("APAC", p)

    def test_like(self) -> None:
        p = {"column": "n", "op": "like", "value": "Al%"}
        assert matches_predicate("Alice", p)
        assert not matches_predicate("Bob", p)

    def test_row_and(self) -> None:
        pred = parse_predicates(["region=US", "age:>=:18"])
        assert matches_row({"region": "US", "age": 20}, pred)
        assert not matches_row({"region": "US", "age": 10}, pred)

    def test_type_mismatch_is_false(self) -> None:
        assert not matches_predicate("abc", {"column": "x", "op": ">", "value": 5})

    def test_empty_predicate_matches(self) -> None:
        assert matches_row({"a": 1}, None)
        assert matches_predicate(1, {})


class TestPool:
    def test_reuses_connection(self) -> None:
        created = 0

        async def factory() -> dict:
            nonlocal created
            created += 1
            return {"id": created}

        async def closer(conn: dict) -> None:
            pass

        async def run() -> None:
            pool = AsyncConnectionPool(factory, closer, max_size=2)
            async with pool.connection() as a:
                assert a["id"] == 1
            async with pool.connection() as b:
                assert b["id"] == 1  # reused
            assert created == 1
            await pool.close()

        asyncio.run(run())

    def test_discards_failed_connection(self) -> None:
        created = 0

        async def factory() -> dict:
            nonlocal created
            created += 1
            return {"id": created}

        async def closer(conn: dict) -> None:
            pass

        async def run() -> None:
            pool = AsyncConnectionPool(factory, closer, max_size=1)
            with pytest.raises(RuntimeError):
                async with pool.connection():
                    raise RuntimeError("boom")
            # next checkout must be a fresh connection
            async with pool.connection() as c:
                assert c["id"] == 2
            await pool.close()

        asyncio.run(run())

    def test_max_size_bounds_concurrency(self) -> None:
        concurrent = 0
        peak = 0

        async def factory() -> dict:
            return {}

        async def closer(conn: dict) -> None:
            pass

        async def run() -> None:
            nonlocal concurrent, peak
            pool = AsyncConnectionPool(factory, closer, max_size=2)

            async def worker() -> None:
                nonlocal concurrent, peak
                async with pool.connection():
                    concurrent += 1
                    peak = max(peak, concurrent)
                    await asyncio.sleep(0.02)
                    concurrent -= 1

            await asyncio.gather(*(worker() for _ in range(6)))
            assert peak <= 2  # never more than max_size at once
            await pool.close()

        asyncio.run(run())

    def test_close_prevents_checkout(self) -> None:
        async def factory() -> dict:
            return {}

        async def closer(conn: dict) -> None:
            pass

        async def run() -> None:
            pool = AsyncConnectionPool(factory, closer, max_size=1)
            await pool.close()
            with pytest.raises(PoolClosedError):
                async with pool.connection():
                    pass

        asyncio.run(run())

    def test_invalid_sizes(self) -> None:
        async def factory() -> dict:
            return {}

        async def closer(conn: dict) -> None:
            pass

        with pytest.raises(ValueError):
            AsyncConnectionPool(factory, closer, max_size=0)
        with pytest.raises(ValueError):
            AsyncConnectionPool(factory, closer, min_size=5, max_size=2)
