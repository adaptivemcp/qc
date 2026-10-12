"""Type inference and falsy analysis tests (SPEC 7.5, 7.6)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from qc.domain.enums import Confidence, LogicalType
from qc.profiling.falsy import analyze_falsy
from qc.profiling.type_inference import (
    detect_format_patterns,
    infer_logical_type,
    normalize_pattern,
)


class TestFalsy:
    def test_all_distinguishable(self) -> None:
        values = [None, "", "   ", 0, False, "0", "false", "null", 1, "x"]
        a = analyze_falsy(values)
        s = a.stats
        assert s.null_count == 1
        assert s.empty_string_count == 1
        assert s.whitespace_only_count == 1
        assert s.zero_count == 1
        assert s.false_count == 1
        assert s.string_zero_count == 1
        assert s.string_false_count == 1
        assert s.string_null_count == 1

    def test_sentinels_not_missing(self) -> None:
        a = analyze_falsy([None, -1, "N/A", "unknown", 9999, 5])
        assert a.stats.null_count == 1
        assert a.stats.sentinel_candidates  # recorded
        assert a.raw_interpretation["sql_null"] == 1
        assert any("NOT counted as missing" in n for n in a.notes)
        # raw values never in keys (hashed)
        for k in a.stats.sentinel_candidates:
            assert "N/A" not in k and "unknown" not in k

    def test_nan_distinct_from_null(self) -> None:
        a = analyze_falsy([None, float("nan"), float("inf"), 1.0])
        assert a.stats.null_count == 1
        assert a.stats.nan_count == 2

    def test_estimated_measurement_from_sample(self) -> None:
        a = analyze_falsy([None, 1], row_count=1000, exact=False)
        assert a.stats.null_measurement.value == "estimated"

    def test_empty_column(self) -> None:
        a = analyze_falsy([])
        assert a.stats.null_count == 0

    def test_bool_false_vs_zero(self) -> None:
        a = analyze_falsy([False, 0, "0"])
        assert a.stats.false_count == 1
        assert a.stats.zero_count == 1
        assert a.stats.string_zero_count == 1


class TestTypeInference:
    def test_integers(self) -> None:
        r = infer_logical_type(list(range(50)))
        assert r.logical_type == LogicalType.INTEGER
        assert r.confidence == Confidence.HIGH

    def test_leading_zeros_kept_as_string(self) -> None:
        vals = [f"{i:05d}" for i in range(30)]
        r = infer_logical_type(vals)
        assert r.logical_type != LogicalType.INTEGER
        assert r.ambiguous or r.logical_type in (
            LogicalType.IDENTIFIER,
            LogicalType.STRING,
        )
        assert any("leading zero" in x for x in r.reasons)

    def test_decimals_no_float_loss(self) -> None:
        r = infer_logical_type([Decimal("10.50"), Decimal("0.01"), Decimal("-9999.99")])
        assert r.logical_type == LogicalType.DECIMAL

    def test_decimal_strings(self) -> None:
        r = infer_logical_type(["10.50", "0.01", "99.99"] * 10)
        assert r.logical_type in (LogicalType.DECIMAL, LogicalType.FLOAT)

    def test_emails(self) -> None:
        vals = [f"user{i}@example.com" for i in range(25)]
        r = infer_logical_type(vals)
        assert r.logical_type == LogicalType.EMAIL
        assert r.confidence in (Confidence.HIGH, Confidence.MEDIUM)

    def test_uuid(self) -> None:
        vals = [f"{i:08x}-1234-5678-9abc-def012345678" for i in range(25)]
        r = infer_logical_type(vals)
        assert r.logical_type == LogicalType.UUID

    def test_ambiguous_date(self) -> None:
        vals = [f"{i % 12 + 1:02d}/{i % 28 + 1:02d}/2025" for i in range(30)]
        r = infer_logical_type(vals)
        assert r.ambiguous
        assert r.locale_hint == "dayfirst_ambiguous"
        assert any("ambiguous" in n for n in r.notes)

    def test_iso_dates(self) -> None:
        vals = [f"2025-01-{i % 28 + 1:02d}" for i in range(30)]
        r = infer_logical_type(vals)
        assert r.logical_type == LogicalType.DATE

    def test_booleans(self) -> None:
        r = infer_logical_type([True, False] * 20)
        assert r.logical_type == LogicalType.BOOLEAN

    def test_free_text(self) -> None:
        vals = [
            f"this is a long piece of free text number {i} with many words indeed"
            for i in range(30)
        ]
        r = infer_logical_type(vals)
        assert r.logical_type == LogicalType.FREE_TEXT

    def test_categorical(self) -> None:
        vals = ["US", "CA", "GB"] * 20
        r = infer_logical_type(vals)
        assert r.logical_type == LogicalType.CATEGORICAL_STRING

    def test_mixed_is_ambiguous(self) -> None:
        vals: list[object] = [1, 2, 3, "abc", "def", "ghi", 4, 5, "jkl"]
        r = infer_logical_type(vals)
        assert r.ambiguous
        assert r.confidence in (Confidence.LOW, Confidence.UNKNOWN, Confidence.MEDIUM)

    def test_empty_input(self) -> None:
        r = infer_logical_type([])
        assert r.logical_type == LogicalType.UNSUPPORTED
        assert r.confidence == Confidence.UNKNOWN

    def test_none_only_input(self) -> None:
        r = infer_logical_type([None, None])
        assert r.logical_type == LogicalType.UNSUPPORTED

    def test_timestamps(self) -> None:
        vals = [datetime(2025, 1, 1, 12, 0, i) for i in range(30)]
        r = infer_logical_type(vals)
        assert r.logical_type in (LogicalType.TIMESTAMP, LogicalType.TIMESTAMP_TZ)

    def test_ips(self) -> None:
        vals = [f"192.168.{i}.{i}" for i in range(1, 30)]
        r = infer_logical_type(vals)
        assert r.logical_type == LogicalType.IP_ADDRESS

    def test_huge_int_string_stays_string(self) -> None:
        r = infer_logical_type(["99999999999999999999999999"] * 10)
        assert r.logical_type == LogicalType.STRING


class TestPatterns:
    def test_normalize(self) -> None:
        assert normalize_pattern("2025-01-05") == normalize_pattern("1999-12-31")
        assert normalize_pattern("INV-000123").startswith("inv-9")

    def test_detect(self) -> None:
        vals = ["INV-0001", "INV-0002", "INV-0003", "other"] * 5
        pats = detect_format_patterns(vals, min_occurrences=3, min_coverage=0.1)
        assert pats
        assert any("inv" in p[1] for p in pats)

    def test_min_coverage(self) -> None:
        vals = ["a1", "b2"] + ["x"] * 50
        pats = detect_format_patterns(vals, min_occurrences=2, min_coverage=0.2)
        # unique patterns must not appear
        assert all(p[2] >= 2 for p in pats)
