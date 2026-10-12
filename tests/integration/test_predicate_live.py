"""Live typed-predicate tests against PostgreSQL and MySQL (SPEC 11.2, 16.5).

Verifies the ``--where`` typed predicate actually filters aggregates and
samples, and that no raw SQL can leak through the predicate path.

Seeding comes from the shared ``postgres_triple`` / ``mysql_triple``
fixtures in ``tests/conftest.py`` — test modules never import each other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from qc.connectors.base import ProfileQuery, SampleRequest
from qc.domain.models import DatasetRef

if TYPE_CHECKING:
    from qc.domain.models import SourceConfig

pytestmark = pytest.mark.integration


def _ref_from(config: SourceConfig, dataset: str) -> DatasetRef:
    ns, _, name = dataset.rpartition(".")
    return DatasetRef(source_id=config.id, namespace=ns or None, name=name, locator=dataset)


class LivePredicateCases:
    """Contract assertions applied to whichever live backend is bound."""

    async def test_row_count_filtered(self, seeded) -> None:  # type: ignore[no-untyped-def]
        connector, config, ref = seeded
        # qc_contract has ids 0..9; filter id < 5 -> 5 rows
        predicate = {"column": "id", "op": "<", "value": 5}
        res = await connector.execute_profile_query(
            config,
            ProfileQuery(kind="row_count", dataset=ref, params={"predicate": predicate}),
        )
        assert res.data["row_count"] == 5

    async def test_equality_predicate(self, seeded) -> None:  # type: ignore[no-untyped-def]
        connector, config, ref = seeded
        predicate = {"column": "id", "op": "=", "value": 3}
        res = await connector.execute_profile_query(
            config,
            ProfileQuery(kind="row_count", dataset=ref, params={"predicate": predicate}),
        )
        assert res.data["row_count"] == 1

    async def test_in_predicate(self, seeded) -> None:  # type: ignore[no-untyped-def]
        connector, config, ref = seeded
        predicate = {"column": "id", "op": "in", "values": [1, 2, 3]}
        res = await connector.execute_profile_query(
            config,
            ProfileQuery(kind="row_count", dataset=ref, params={"predicate": predicate}),
        )
        assert res.data["row_count"] == 3

    async def test_and_predicate(self, seeded) -> None:  # type: ignore[no-untyped-def]
        connector, config, ref = seeded
        predicate = {
            "op": "and",
            "clauses": [
                {"column": "id", "op": ">=", "value": 2},
                {"column": "id", "op": "<", "value": 6},
            ],
        }
        res = await connector.execute_profile_query(
            config,
            ProfileQuery(kind="row_count", dataset=ref, params={"predicate": predicate}),
        )
        assert res.data["row_count"] == 4

    async def test_sampling_respects_predicate(self, seeded) -> None:  # type: ignore[no-untyped-def]
        connector, config, ref = seeded
        predicate = {"column": "id", "op": "<", "value": 3}
        rows = []
        async for row in connector.sample_rows(
            config,
            SampleRequest(dataset=ref, limit=100, columns=["id", "label"], predicate=predicate),
        ):
            rows.append(row)
        assert rows
        assert all(r.values["id"] in (0, 1, 2) for r in rows)

    async def test_unsafe_predicate_column_rejected(self, seeded) -> None:  # type: ignore[no-untyped-def]
        from qc.domain.errors import InvalidArgumentsError

        connector, config, ref = seeded
        predicate = {"column": "id; DROP TABLE x", "op": "=", "value": 1}
        with pytest.raises(InvalidArgumentsError):
            await connector.execute_profile_query(
                config,
                ProfileQuery(kind="row_count", dataset=ref, params={"predicate": predicate}),
            )


class TestPostgresPredicate(LivePredicateCases):
    """Live predicate behavior on PostgreSQL."""

    @pytest.fixture
    async def seeded(self, postgres_triple):  # type: ignore[no-untyped-def]
        connector, config, dataset = postgres_triple
        yield connector, config, _ref_from(config, dataset)


class TestMySQLPredicate(LivePredicateCases):
    """Live predicate behavior on MySQL."""

    @pytest.fixture
    async def seeded(self, mysql_triple):  # type: ignore[no-untyped-def]
        connector, config, dataset = mysql_triple
        yield connector, config, _ref_from(config, dataset)
