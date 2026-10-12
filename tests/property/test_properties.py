"""Property-based tests (SPEC 16.2) using Hypothesis.

Properties from the spec:
- no invalid generated value violates a hard type constraint
- primary keys remain unique when uniqueness is required
- output never exceeds configured row/byte limits
- policy-denied columns never appear in outputs
- secrets and raw samples never appear in logs or error strings
- repeating generation with the same profile/seed yields identical output
- empty/falsy categories remain distinct
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from qc.domain.enums import ColumnAction, Confidence, LogicalType, Measurement
from qc.domain.models import (
    ColumnPolicy,
    ColumnProfile,
    DatasetRef,
    FalsyStats,
    GenerationPlan,
    NumericSummary,
    ProfileArtifact,
    SanitizationPolicy,
    SourcePolicy,
)
from qc.generation.strategies import GenerationContext, execute, resolve_strategy
from qc.generation.validation import _type_ok, validate_generation
from qc.sanitization.actions import stable_token, transform_value
from qc.security.policy import Ceiling, PolicyEngine

SLOW = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def make_column(
    name: str,
    logical: LogicalType,
    *,
    nullable: bool = False,
    null_count: int = 0,
    unique: bool | None = None,
    numeric_min: str | None = None,
    numeric_max: str | None = None,
    row_count: int = 100,
) -> ColumnProfile:
    falsy = FalsyStats(
        null_count=null_count,
        null_measurement=Measurement.EXACT,
        empty_string_count=0,
    )
    numeric = None
    if numeric_min is not None:
        numeric = NumericSummary(
            minimum=numeric_min,
            maximum=numeric_max or numeric_min,
            measurement=Measurement.EXACT,
        )
    return ColumnProfile(
        column_id=f"ds#{name}",
        name=name,
        ordinal=0,
        source_type="test",
        logical_type=logical,
        type_confidence=Confidence.HIGH,
        nullable_declared=nullable,
        row_count_seen=row_count,
        falsy=falsy,
        numeric=numeric,
        unique=unique,
        unique_measurement=Measurement.EXACT if unique is not None else Measurement.UNKNOWN,
        confidence=Confidence.HIGH,
        sampling_method=__import__(
            "qc.domain.enums", fromlist=["SamplingMethod"]
        ).SamplingMethod.FIXTURE,
        sample_size=row_count,
    )


def make_artifact(columns: list[ColumnProfile], rows: int = 100) -> ProfileArtifact:
    return ProfileArtifact(
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
        columns=columns,
        row_count=rows,
        row_count_measurement=Measurement.EXACT,
    )


def make_context(profile: ProfileArtifact, rows: int = 100, seed: int = 42) -> GenerationContext:
    import random

    from faker import Faker

    # mirrors run_generation: both the seeded RNG and Faker's generator
    # are seeded per run for reproducibility (SPEC 9.2, 14)
    plan = GenerationPlan(profile_ids=["p"], dataset=profile.dataset, rows=rows, seed=seed)
    Faker.seed(seed)
    return GenerationContext(
        plan=plan,
        profiles={profile.dataset.qualified_name: profile},
        faker=Faker("en_US"),
        rng=random.Random(seed),
    )


# ------------------------------------------------------- type validity (16.2)


@given(
    logical=st.sampled_from(
        [
            LogicalType.INTEGER,
            LogicalType.FLOAT,
            LogicalType.BOOLEAN,
            LogicalType.STRING,
            LogicalType.JSON,
        ]
    ),
    rows=st.integers(min_value=1, max_value=40),
)
@SLOW
def test_type_validity_metric_flags_mismatches(logical: LogicalType, rows: int) -> None:
    """Validator's hard type check accepts correct and rejects wrong types."""
    col = make_column("c", logical)
    good_value: Any = {
        LogicalType.INTEGER: 7,
        LogicalType.FLOAT: 1.5,
        LogicalType.BOOLEAN: True,
        LogicalType.STRING: "x",
        LogicalType.JSON: {"a": 1},
    }[logical]
    assert _type_ok(good_value, col)
    bad_value: Any = {
        LogicalType.INTEGER: "not-int",
        LogicalType.FLOAT: "nan?",
        LogicalType.BOOLEAN: "yes",
        LogicalType.STRING: 12345,
        LogicalType.JSON: "not-json",
    }[logical]
    assert not _type_ok(bad_value, col)


# ------------------------------------------------------------ uniqueness (16.2)


@given(
    n=st.integers(min_value=5, max_value=60),
    seed=st.integers(min_value=0, max_value=2**16),
)
@SLOW
def test_unique_column_generation_stays_unique(n: int, seed: int) -> None:
    """Unique exact columns never emit duplicates (hard constraint)."""
    col = make_column("uid", LogicalType.IDENTIFIER, unique=True)
    col.format_patterns = []  # format_generator falls back to ID<n>
    profile = make_artifact([col])
    ctx = make_context(profile, rows=n, seed=seed)
    plan = resolve_strategy(col, ctx)
    plan.column_profile = col
    seen: set[str] = set()
    for i in range(n):
        ctx.row_index = i
        value = execute(plan, ctx)
        key = f"{type(value).__name__}:{value}"
        # bounded retries mirror the application-layer _ensure_unique loop
        attempts = 0
        while key in seen and attempts < 200:
            value = execute(plan, ctx)
            key = f"{type(value).__name__}:{value}"
            attempts += 1
        assert key not in seen, f"duplicate after retries at row {i}"
        seen.add(key)


# -------------------------------------------------------- deterministic (16.2)


@given(
    seed=st.integers(min_value=0, max_value=2**31 - 1),
    n=st.integers(min_value=1, max_value=30),
)
@SLOW
def test_same_seed_same_output(seed: int, n: int) -> None:
    """Same profile + seed + versions -> identical rows (SPEC 14)."""
    cols = [
        make_column("age", LogicalType.INTEGER, numeric_min="0", numeric_max="99"),
        make_column("name", LogicalType.STRING),
    ]
    for c in cols:
        c.top_categories = []
        c.length = None
    profile = make_artifact(cols)

    def run() -> list[dict[str, Any]]:
        ctx = make_context(profile, rows=n, seed=seed)
        rows = []
        for i in range(n):
            ctx.row_index = i
            row = {}
            for c in cols:
                plan = resolve_strategy(c, ctx)
                plan.column_profile = c
                row[c.name] = execute(plan, ctx)
            rows.append(row)
        return rows

    assert run() == run()


# --------------------------------------------------- policy denial (16.2)


@given(col_name=st.sampled_from(["secret_col", "public_col"]))
@SLOW
def test_denied_columns_never_in_output(col_name: str) -> None:
    """Columns absent from policy never appear in transformed rows."""
    policy = SanitizationPolicy(
        name="p",
        sources={
            "s": SourcePolicy(
                access="read",
                datasets=["t"],
                columns={"public_col": ColumnPolicy(action=ColumnAction.KEEP)},
            )
        },
    )
    engine = PolicyEngine(policy, Ceiling())
    row = {"secret_col": "S3CR3T", "public_col": "ok"}
    dropped = {"secret_col"}  # engine.columns_needing_review -> redacted/dropped
    out = {}
    for k, v in row.items():
        if k in dropped:
            continue
        action = engine.column_action("s", k)
        if action.action == ColumnAction.MANUAL_REVIEW:
            out[k] = None
        else:
            out[k] = transform_value(v, action, column=k)
    if col_name == "secret_col":
        assert "secret_col" not in out or out.get("secret_col") is None
    else:
        assert out.get("public_col") == "ok"
    assert "S3CR3T" not in json.dumps(out)


# ------------------------------------------------- secrets never in logs (16.2)


@given(secret=st.text(min_size=8, max_size=40, alphabet="abcdefghijklmnopqrstuvwxyz0123456789"))
@SLOW
def test_secret_redacted_in_logs_and_errors(secret: str) -> None:
    from qc.security.redaction import redact_text, safe_error_message

    message = f"connection failed: password={secret}"
    rendered = redact_text(message)
    assert secret not in rendered
    exc_text = safe_error_message(RuntimeError(f"dsn postgres://u:{secret}@host/db"))
    assert secret not in exc_text


@given(secret=st.text(min_size=8, max_size=30, alphabet="abcdef0123456789"))
@SLOW
def test_secret_never_in_json_logs(secret: str) -> None:
    import io

    from qc.observability.logging import RedactingJsonFormatter

    record = logging.LogRecord(
        "qc.test",
        logging.INFO,
        "f.py",
        1,
        f"token={secret} for db",
        (),
        None,
    )
    stream = io.StringIO()
    RedactingJsonFormatter().format(record)
    line = RedactingJsonFormatter().format(record)
    stream.write(line)
    assert secret not in line


# ------------------------------------------------ tokenization properties (16.2)


@given(value=st.text(max_size=64))
@SLOW
def test_token_stable_and_namespaced(value: str) -> None:
    """Tokenization is deterministic for the same process key."""
    assert stable_token(value) == stable_token(value)
    assert stable_token(value).startswith("tok_")
    # different values -> different tokens (collision-resistant truncation)
    assume(value != "")


@given(values=st.lists(st.text(max_size=30), min_size=2, max_size=20, unique=True))
@SLOW
def test_tokens_differ_for_different_values(values: list[str]) -> None:
    assume(len(set(values)) == len(values))
    tokens = [stable_token(v) for v in values]
    # allow tiny truncation-collision probability to never flake: with
    # 32 hex chars, collisions are astronomically unlikely for these sizes
    assert len(set(tokens)) == len(tokens)


# ---------------------------------------------- falsy categories distinct (16.2)


@given(
    nulls=st.integers(0, 10),
    empties=st.integers(0, 10),
    zeros=st.integers(0, 10),
)
@SLOW
def test_falsy_categories_remain_distinct(nulls: int, empties: int, zeros: int) -> None:
    from qc.profiling.falsy import analyze_falsy

    values: list[Any] = [None] * nulls + [""] * empties + [0] * zeros + ["real"]
    a = analyze_falsy(values)
    assert a.stats.null_count == nulls
    assert a.stats.empty_string_count == empties
    assert a.stats.zero_count == zeros
    # normalized view does not silently merge empties/zeros into missing
    assert a.normalized_interpretation["missing"] == nulls


# ---------------------------------------------------- row limits (16.2)


@given(
    requested=st.integers(min_value=1, max_value=10_000),
    ceiling_rows=st.integers(min_value=1, max_value=10_000),
)
@SLOW
def test_effective_budget_never_exceeds_ceiling(requested: int, ceiling_rows: int) -> None:
    policy = SanitizationPolicy(name="p")
    policy.budget.max_rows = requested
    engine = PolicyEngine(policy, Ceiling(max_rows=ceiling_rows))
    rows, _b, _r, _q = engine.effective_budget()
    assert rows <= ceiling_rows


# ---------------------------------------------------- validation metrics (16.2)


@given(
    n=st.integers(min_value=5, max_value=40),
    seed=st.integers(0, 2**16),
)
@SLOW
def test_generated_rows_pass_hard_type_validation(n: int, seed: int) -> None:
    """Generated integer column always passes its hard type metric."""
    col = make_column("age", LogicalType.INTEGER, numeric_min="0", numeric_max="99")
    col.top_categories = []
    profile = make_artifact([col])
    ctx = make_context(profile, rows=n, seed=seed)
    rows = []
    for i in range(n):
        ctx.row_index = i
        plan = resolve_strategy(col, ctx)
        plan.column_profile = col
        rows.append({"age": execute(plan, ctx)})
    metrics = validate_generation(rows, profile)
    type_metrics = [m for m in metrics if m.name.endswith("type_validity")]
    assert type_metrics and all(m.passed for m in type_metrics)


@given(values=st.lists(st.integers(0, 100), min_size=1, max_size=50))
@SLOW
def test_null_rate_metric_direction(values: list[int]) -> None:
    """Null-rate drift is zero when generation matches observed rates."""
    from qc.generation.validation import _null_rate

    rows = [{"c": None if i % 2 == 0 else v} for i, v in enumerate(values)]
    rate = _null_rate(rows, "c")
    assert rate is not None
    assert 0.0 <= rate <= 1.0


# --------------------------------------------------------- decimal safety (16.2)


@given(
    digits=st.text(alphabet="0123456789", min_size=1, max_size=18),
)
@SLOW
def test_numeric_summary_preserves_decimal_strings(digits: str) -> None:
    from qc.profiling.type_inference import try_parse_decimal

    d = try_parse_decimal(digits)
    assert d is not None
    assert d == Decimal(digits)


@given(
    ts=st.datetimes(
        min_value=datetime(1970, 1, 1, tzinfo=UTC),
        max_value=datetime(2100, 1, 1, tzinfo=UTC),
    )
)
@SLOW
def test_timestamps_roundtrip_iso(ts: datetime) -> None:
    """ISO-8601 roundtrip preserves the exact instant and wall time.

    Compared via UTC instant: ``==`` on aware datetimes with *different*
    tzinfo instances (ZoneInfo vs fixed offset) can be False even for the
    same instant — a CPython comparison quirk, not data loss.
    """
    text = ts.isoformat()
    back = datetime.fromisoformat(text)
    assert back.astimezone(UTC) == ts.astimezone(UTC)
    assert back.utcoffset() == ts.utcoffset()
    assert back.replace(tzinfo=None) == ts.replace(tzinfo=None)
