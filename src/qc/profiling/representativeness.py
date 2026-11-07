"""How much of a dataset a profile actually saw (SPEC 7.4, 7.7).

A sample cannot establish the absence of a rare value, and a profile that says "sampled
1,000 rows" without saying *of how many* leaves the reader to guess. This turns the two
numbers the profiler already has into one stated claim: how many rows were seen, what
fraction of the table that is, by which method, and what that does and does not support.

Deliberately conservative: coverage is arithmetic, confidence is a rule, and the statement
names the limitation instead of implying more certainty than a sample can carry.
"""

from __future__ import annotations

from qc.domain.enums import Confidence, Measurement, SamplingMethod
from qc.domain.models import Representativeness

#: Below this share of the table, a sample is a spot check rather than a survey.
SMALL_COVERAGE = 0.01
#: At or above this share, a sample can reasonably describe common values.
USEFUL_COVERAGE = 0.2


def summarize_sampling(
    *,
    method: SamplingMethod,
    sampled_rows: int,
    total_rows: int | None,
    total_measurement: Measurement = Measurement.UNKNOWN,
    requested_sample_size: int | None = None,
) -> Representativeness:
    """Build the artifact's representativeness record.

    `total_rows=None` means the source did not report a row count; coverage is then
    unknown, which is different from zero and is said out loud.
    """
    limitations: list[str] = []
    coverage: float | None = None

    if total_rows is not None and total_rows > 0:
        coverage = round(min(sampled_rows / total_rows, 1.0), 6)
    elif sampled_rows > 0:
        limitations.append("total row count unknown; coverage cannot be stated")
    else:
        limitations.append("no rows were sampled")
        return Representativeness(
            method=method,
            sampled_rows=0,
            total_rows=total_rows,
            total_measurement=total_measurement,
            coverage=None,
            confidence=Confidence.UNKNOWN,
            statement="no sampling was performed; type and format evidence is limited to "
            "metadata and aggregates",
            limitations=limitations,
        )

    if total_measurement == Measurement.ESTIMATED:
        limitations.append("total row count is a metadata estimate, not an exact count")
    if method != SamplingMethod.FULL_SCAN:
        limitations.append(
            "a sample cannot establish the absence of a rare value; absence in the sample "
            "is not evidence of absence"
        )
    if sampled_rows < (requested_sample_size or sampled_rows):
        limitations.append(
            f"sample reached {sampled_rows:,} of the requested {requested_sample_size:,} rows"
        )

    if method == SamplingMethod.FULL_SCAN:
        confidence = Confidence.HIGH
        statement = f"full scan of {sampled_rows:,} rows; every row was seen"
    elif coverage is None:
        confidence = Confidence.LOW
        statement = (
            f"sampled {sampled_rows:,} rows by {method.value}; the dataset's total is "
            "unknown, so the fraction seen cannot be stated"
        )
    else:
        percent = coverage * 100
        if coverage >= USEFUL_COVERAGE:
            confidence = (
                Confidence.MEDIUM if total_measurement == Measurement.EXACT else Confidence.LOW
            )
        else:
            confidence = Confidence.LOW
        qualifier = "a spot check" if coverage < SMALL_COVERAGE else "a sample"
        statement = (
            f"sampled {sampled_rows:,} of {total_rows:,} rows ({percent:.2f}% coverage) by "
            f"{method.value} — {qualifier} of the dataset, so rare values may be missing"
        )

    return Representativeness(
        method=method,
        sampled_rows=sampled_rows,
        total_rows=total_rows,
        total_measurement=total_measurement,
        coverage=coverage,
        confidence=confidence,
        statement=statement,
        limitations=limitations,
    )
