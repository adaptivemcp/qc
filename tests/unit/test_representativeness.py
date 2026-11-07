"""The profile's sampling claim (SPEC 7.4, 7.7).

Two numbers are not a claim. These pin the arithmetic, the confidence rule and the
sentence that ends up in an artifact, because a report a user reads must say what the
sample supports and what it does not.
"""

from __future__ import annotations

from qc.domain.enums import Confidence, Measurement, SamplingMethod
from qc.profiling.representativeness import summarize_sampling


def test_a_sample_states_its_coverage_exactly() -> None:
    record = summarize_sampling(
        method=SamplingMethod.STABLE_KEY,
        sampled_rows=1000,
        total_rows=100_000,
        total_measurement=Measurement.EXACT,
        requested_sample_size=1000,
    )

    assert record.coverage == 0.01
    assert record.confidence == Confidence.LOW
    assert record.statement == (
        "sampled 1,000 of 100,000 rows (1.00% coverage) by stable_key — a sample of the "
        "dataset, so rare values may be missing"
    )
    assert any("absence in the sample" in limitation for limitation in record.limitations)


def test_a_useful_sample_of_an_exactly_counted_table_is_medium_confidence() -> None:
    record = summarize_sampling(
        method=SamplingMethod.STABLE_KEY,
        sampled_rows=25_000,
        total_rows=100_000,
        total_measurement=Measurement.EXACT,
        requested_sample_size=25_000,
    )

    assert record.coverage == 0.25
    assert record.confidence == Confidence.MEDIUM


def test_an_estimated_total_downgrades_the_confidence() -> None:
    exact = summarize_sampling(
        method=SamplingMethod.STABLE_KEY,
        sampled_rows=25_000,
        total_rows=100_000,
        total_measurement=Measurement.EXACT,
    )
    estimated = summarize_sampling(
        method=SamplingMethod.STABLE_KEY,
        sampled_rows=25_000,
        total_rows=100_000,
        total_measurement=Measurement.ESTIMATED,
    )

    assert exact.confidence == Confidence.MEDIUM
    assert estimated.confidence == Confidence.LOW
    assert any("metadata estimate" in limitation for limitation in estimated.limitations)


def test_an_unknown_total_says_coverage_cannot_be_stated() -> None:
    record = summarize_sampling(
        method=SamplingMethod.STABLE_KEY,
        sampled_rows=500,
        total_rows=None,
        total_measurement=Measurement.UNKNOWN,
    )

    assert record.coverage is None
    assert record.confidence == Confidence.LOW
    assert "the dataset's total is unknown" in record.statement
    assert "coverage cannot be stated" in record.limitations[0]


def test_a_full_scan_is_high_confidence_and_says_so() -> None:
    record = summarize_sampling(
        method=SamplingMethod.FULL_SCAN,
        sampled_rows=100_000,
        total_rows=100_000,
        total_measurement=Measurement.EXACT,
    )

    assert record.coverage == 1.0
    assert record.confidence == Confidence.HIGH
    assert record.statement == "full scan of 100,000 rows; every row was seen"
    # A full scan has no sampling limitation to disclose.
    assert not any("absence" in limitation for limitation in record.limitations)


def test_no_rows_states_that_no_sampling_happened() -> None:
    record = summarize_sampling(
        method=SamplingMethod.NONE,
        sampled_rows=0,
        total_rows=None,
    )

    assert record.coverage is None
    assert record.confidence == Confidence.UNKNOWN
    assert "no sampling was performed" in record.statement


def test_a_short_sample_names_the_shortfall() -> None:
    record = summarize_sampling(
        method=SamplingMethod.STABLE_KEY,
        sampled_rows=400,
        total_rows=100_000,
        total_measurement=Measurement.EXACT,
        requested_sample_size=1000,
    )

    assert any("400 of the requested 1,000 rows" in limitation for limitation in record.limitations)
