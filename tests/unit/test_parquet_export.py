"""Parquet export (optional ``parquet`` extra).

Skipped when pyarrow is absent, so the core suite stays dependency-light.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq  # noqa: E402

from qc.exports.parquet import rows_to_table, write_parquet  # noqa: E402


def test_column_types_are_mapped_explicitly() -> None:
    rows = [
        {
            "id": 1,
            "amount": Decimal("10.25"),
            "day": date(2026, 1, 2),
            "at": datetime(2026, 1, 2, 3, 4, 5),
            "ok": True,
            "blob": b"\x00\x01",
            "name": "a",
            "nothing": None,
        }
    ]
    schema = rows_to_table(rows).schema
    assert schema.field("id").type == pa.int64()
    assert pa.types.is_decimal(schema.field("amount").type)
    assert schema.field("day").type == pa.date32()
    assert pa.types.is_timestamp(schema.field("at").type)
    assert schema.field("ok").type == pa.bool_()
    assert pa.types.is_binary(schema.field("blob").type)
    assert schema.field("name").type == pa.string()
    assert pa.types.is_null(schema.field("nothing").type)


def test_field_order_is_stable() -> None:
    table = rows_to_table([{"b": 1, "a": 2}], field_order=["a", "b"])
    assert table.column_names == ["a", "b"]


def test_decimal_precision_survives_the_round_trip(tmp_path: Path) -> None:
    value = Decimal("1234567890.123456")
    path = tmp_path / "out.parquet"
    assert write_parquet(path, [{"d": value}]) == 1
    arrow_type = pq.read_schema(path).field("d").type
    assert arrow_type.scale == 6
    assert arrow_type.precision >= 16
    assert pq.read_table(path).column("d").to_pylist() == [value]


def test_nulls_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "out.parquet"
    write_parquet(path, [{"a": 1, "b": None}, {"a": 2, "b": None}])
    back = pq.read_table(path).to_pylist()
    assert [row["b"] for row in back] == [None, None]
    assert [row["a"] for row in back] == [1, 2]


def test_a_mixed_column_falls_back_to_text_rather_than_guessing() -> None:
    table = rows_to_table([{"x": 1}, {"x": "two"}])
    assert table.schema.field("x").type == pa.string()
    assert table.column("x").to_pylist() == ["1", "two"]
