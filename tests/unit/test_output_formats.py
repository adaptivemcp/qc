"""Output-format tests: CSV options, JSONL precision, null patterns
(SPEC 10.1, 10.2, 11.2)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from qc.application.sanitize import _null_patterns
from qc.exports.csv_writer import csv_safe, write_csv_rows
from qc.exports.jsonl import json_scalar, to_json_line, write_jsonl


class TestJsonlPrecision:
    def test_exact_integer_decimal_becomes_number(self) -> None:
        assert json_scalar(Decimal("42")) == 42

    def test_round_tripping_decimal_becomes_number(self) -> None:
        assert json_scalar(Decimal("1.5")) == 1.5

    def test_unrepresentable_decimal_stays_string(self) -> None:
        # many decimals cannot round-trip through float; keep lossless string
        value = Decimal("0.123456789012345678901234567890")
        assert json_scalar(value) == str(value)

    def test_bytes_base64(self) -> None:
        assert json_scalar(b"\x00\x01") == "AAE="

    def test_nested_scalars(self) -> None:
        out = json_scalar({"a": [Decimal("1"), Decimal("2.25")]})
        assert out == {"a": [1, 2.25]}

    def test_write_jsonl_stable_order(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        write_jsonl(
            path,
            [{"b": 2, "a": 1, "c": None}],
            field_order=["a", "b", "c"],
        )
        line = path.read_text().strip()
        assert line.index('"a"') < line.index('"b"') < line.index('"c"')
        assert json.loads(line)["c"] is None  # JSON null preserved

    def test_to_json_line_utf8(self) -> None:
        line = to_json_line({"name": "café"})
        assert "café" in line


class TestCsvOptions:
    def test_custom_delimiter(self, tmp_path: Path) -> None:
        path = tmp_path / "out.csv"
        write_csv_rows(path, [{"a": 1, "b": 2}], delimiter=";")
        content = path.read_text()
        assert "a;b" in content
        assert "1;2" in content

    def test_no_header(self, tmp_path: Path) -> None:
        path = tmp_path / "out.csv"
        write_csv_rows(path, [{"a": 1, "b": 2}], header=False)
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        assert "a" not in lines[0]

    def test_formula_protection_toggle(self) -> None:
        assert csv_safe("=SUM(A1)", protect_formulas=True).startswith("'")
        assert csv_safe("=SUM(A1)", protect_formulas=False) == "=SUM(A1)"

    def test_null_sentinel(self) -> None:
        assert csv_safe(None) == "\\N"
        assert csv_safe("") == ""


class TestNullPatterns:
    def test_counts_distinct_representations(self) -> None:
        rows = [
            {"c": None},
            {"c": ""},
            {"c": "   "},
            {"c": "null"},
            {"c": "N/A"},
            {"c": "real"},
        ]
        out = _null_patterns(rows, ["c"])
        p = out["c"]
        assert p["sql_null"] == 1
        assert p["empty_string"] == 1
        assert p["whitespace_only"] == 1
        assert p["string_null"] == 1
        assert p["literal_na"] == 1
        # no raw values leaked
        assert "real" not in json.dumps(out)

    def test_multiple_columns(self) -> None:
        rows = [{"a": None, "b": "x"}, {"a": 1, "b": None}]
        out = _null_patterns(rows, ["a", "b"])
        assert out["a"]["sql_null"] == 1
        assert out["b"]["sql_null"] == 1
