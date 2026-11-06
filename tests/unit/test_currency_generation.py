"""Currency-like generation (SPEC 7.6, 9.3).

An inferred currency column must stay generatable, and it must keep its marker:
a column of ``$12.50`` should not start emitting bare numbers, because the marker
is part of what the source contained.
"""

from __future__ import annotations

import random

from faker import Faker

from qc.domain.enums import Confidence, LogicalType, Measurement
from qc.domain.models import (
    ColumnProfile,
    DatasetRef,
    GenerationPlan,
    ProfileArtifact,
)
from qc.generation.strategies import GenerationContext, execute, resolve_strategy


def _column(
    patterns: list[str],
    *,
    numeric_min: str | None = None,
    numeric_max: str | None = None,
) -> ColumnProfile:
    from qc.domain.models import FormatPattern, NumericSummary

    return ColumnProfile(
        column_id="ds#amount",
        name="amount",
        ordinal=0,
        source_type="text",
        logical_type=LogicalType.CURRENCY,
        type_confidence=Confidence.HIGH,
        row_count_seen=100,
        numeric=(
            NumericSummary(minimum=numeric_min, maximum=numeric_max, measurement=Measurement.EXACT)
            if numeric_min is not None
            else None
        ),
        format_patterns=[
            FormatPattern(
                pattern_id=f"fmt_{i}",
                regex=pattern,
                occurrences=10,
                coverage=0.5,
                measurement=Measurement.EXACT,
            )
            for i, pattern in enumerate(patterns)
        ],
        confidence=Confidence.HIGH,
    )


def _context(column: ColumnProfile, seed: int = 7) -> GenerationContext:
    plan = GenerationPlan(
        profile_ids=["p"],
        dataset=DatasetRef(source_id="src_test", name="t"),
        rows=10,
        seed=seed,
    )
    artifact = ProfileArtifact(
        tool_version="test",
        source_id="src_test",
        source_fingerprint="fp",
        schema_fingerprint="fp",
        dataset=DatasetRef(source_id="src_test", name="t"),
        dataset_schema=__import__("qc.domain.models", fromlist=["DatasetSchema"]).DatasetSchema(
            dataset=DatasetRef(source_id="src_test", name="t"),
            source_fingerprint="fp",
            columns=[],
        ),
        columns=[column],
        row_count=100,
        row_count_measurement=Measurement.EXACT,
    )
    Faker.seed(seed)
    return GenerationContext(
        plan=plan,
        profiles={artifact.dataset.qualified_name: artifact},
        faker=Faker("en_US"),
        rng=random.Random(seed),
    )


def _generate(column: ColumnProfile, seed: int = 7) -> list[str]:
    context = _context(column, seed)
    plan = resolve_strategy(column, context)
    plan.column_profile = column
    assert plan.strategy == "currency_amount"
    return [execute(plan, context) for _ in range(5)]


def test_symbol_prefix_is_preserved() -> None:
    values = _generate(_column(["$999.99"]))

    assert all(value.startswith("$") for value in values), values
    assert all(value[1:].replace(",", "").count(".") == 1 for value in values)


def test_iso_suffix_is_preserved() -> None:
    values = _generate(_column(["999.99 usd"]))

    assert all(value.endswith("USD") for value in values), values


def test_grouping_follows_the_observed_pattern() -> None:
    grouped = _generate(_column(["$9,999.99"]))
    plain = _generate(_column(["$999.99"]))

    # With grouping the thousands separator appears; without it, it does not.
    assert any("," in value for value in grouped), grouped
    assert all("," not in value for value in plain), plain


def test_numeric_range_is_respected_when_known() -> None:
    values = _generate(_column(["$999.99"], numeric_min="10", numeric_max="20"))

    amounts = [float(value.lstrip("$")) for value in values]
    assert all(10.0 <= amount <= 20.0 for amount in amounts), amounts


def test_same_seed_gives_the_same_amounts() -> None:
    assert _generate(_column(["$999.99"]), seed=11) == _generate(_column(["$999.99"]), seed=11)


def test_no_pattern_still_produces_a_plain_amount() -> None:
    values = _generate(_column([]))

    assert all(value.replace(".", "").isdigit() for value in values), values
