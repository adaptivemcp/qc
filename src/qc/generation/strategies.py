"""Generation strategy registry (SPEC 9.1).

Preferred order: explicit override → declared constraints → strongly
inferred format → semantic Faker → learned categorical/distribution →
generic type-aware → fail closed.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, timedelta
from decimal import Decimal
from typing import Any, Protocol

from qc.domain.enums import LogicalType
from qc.domain.errors import GenerationError
from qc.domain.models import ColumnProfile, GenerationPlan, ProfileArtifact


@dataclass
class GenerationContext:
    """Shared state across strategies for one generation run."""

    plan: GenerationPlan
    profiles: dict[str, ProfileArtifact]  # dataset-qualified -> artifact
    faker: Any  # Faker instance
    rng: Any  # random.Random seeded from plan.seed
    row_index: int = 0
    # parent key pools for foreign-key fills: column -> [values]
    key_pools: dict[str, list[Any]] = field(default_factory=dict)
    # value frequencies learned from profiles for categorical sampling
    category_weights: dict[str, list[tuple[Any, float]]] = field(default_factory=dict)


@dataclass
class StrategyPlan:
    """Resolved generation recipe for one column."""

    strategy: str
    column: str
    column_profile: ColumnProfile | None = None
    params: dict[str, Any] = field(default_factory=dict)
    fallback: bool = False

    @property
    def profile(self) -> ColumnProfile:
        if self.column_profile is None:
            raise GenerationError(f"column profile not attached for '{self.column}'")
        return self.column_profile


class GenerationStrategy(Protocol):
    name: str

    def can_generate(self, column: ColumnProfile) -> bool: ...

    def plan(self, column: ColumnProfile, context: GenerationContext) -> StrategyPlan: ...

    def generate(self, plan: StrategyPlan, context: GenerationContext) -> Any: ...


# --------------------------------------------------------------------- helpers


class _BaseStrategy:
    name = "base"

    def can_generate(self, column: ColumnProfile) -> bool:
        return False

    def plan(self, column: ColumnProfile, context: GenerationContext) -> StrategyPlan:
        return StrategyPlan(strategy=self.name, column=column.name, column_profile=column)

    def generate(self, plan: StrategyPlan, context: GenerationContext) -> Any:
        raise GenerationError(f"strategy {self.name} cannot generate '{plan.column}'")


# ---------------------------------------------------------------- strategies


class CategoricalSampler(_BaseStrategy):
    """Learned categorical/distribution sampler (SPEC 9.1 step 5)."""

    name = "categorical_sampler"

    def can_generate(self, column: ColumnProfile) -> bool:
        # A unique column needs unbounded distinct values; a bounded
        # category pool can never satisfy that (SPEC 9.2/9.6).
        if column.unique is True:
            return False
        return bool(column.top_categories) and (
            column.distinct_count is not None and column.distinct_count <= 200
        )

    def plan(self, column: ColumnProfile, context: GenerationContext) -> StrategyPlan:
        # Falsy groups (NULL/false) have no displayable source value:
        # they sample as None rather than leaking a digest. Real category
        # values sample by observed frequency.
        weights: list[tuple[Any, float]] = []
        for c in column.top_categories:
            value: Any = None if c.display is None else c.display
            weights.append((value, c.count))
        total = sum(w for _, w in weights) or 1
        normalized = [(v, w / total) for v, w in weights]
        return StrategyPlan(
            strategy=self.name,
            column=column.name,
            column_profile=column,
            params={"weights": normalized},
        )

    def generate(self, plan: StrategyPlan, context: GenerationContext) -> Any:
        weights = plan.params["weights"]
        if not weights:
            return None
        # rare categories already suppressed upstream (rare_category_min)
        choices = [w[0] for w in weights]
        probs = [w[1] for w in weights]
        return context.rng.choices(choices, weights=probs, k=1)[0]


class FormatEmail(_BaseStrategy):
    name = "format_email"

    def can_generate(self, column: ColumnProfile) -> bool:
        return column.logical_type == LogicalType.EMAIL

    def generate(self, plan: StrategyPlan, context: GenerationContext) -> Any:
        user = context.faker.user_name()
        # reserved synthetic domain, never real addresses (SPEC 9.3)
        return f"{user}@example.invalid"


class FormatUuid(_BaseStrategy):
    name = "format_uuid"

    def can_generate(self, column: ColumnProfile) -> bool:
        return column.logical_type == LogicalType.UUID

    def generate(self, plan: StrategyPlan, context: GenerationContext) -> Any:
        return str(uuid.UUID(int=context.rng.getrandbits(128), version=4))


class TemporalSampler(_BaseStrategy):
    name = "temporal_sampler"

    def can_generate(self, column: ColumnProfile) -> bool:
        return (
            column.logical_type
            in (LogicalType.DATE, LogicalType.TIMESTAMP, LogicalType.TIMESTAMP_TZ, LogicalType.TIME)
            and column.temporal is not None
            and column.temporal.minimum is not None
        )

    def generate(self, plan: StrategyPlan, context: GenerationContext) -> Any:
        col = plan.profile
        temporal = col.temporal
        if temporal is None or temporal.minimum is None:
            raise GenerationError(f"no temporal range for '{col.name}'")
        lo = temporal.minimum
        hi = temporal.maximum or lo
        span = (hi - lo).total_seconds()
        offset = context.rng.random() * max(span, 0)
        value = lo + timedelta(seconds=offset)
        if col.logical_type == LogicalType.DATE:
            return value.date().isoformat()
        if col.logical_type == LogicalType.TIME:
            return value.strftime("%H:%M:%S")
        if col.logical_type == LogicalType.TIMESTAMP_TZ:
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        # preserve observed precision: seconds (no microseconds) if source had none
        if lo.microsecond == 0 and hi.microsecond == 0:
            return value.replace(microsecond=0).isoformat(sep=" ")
        return value.isoformat(sep=" ")


class NumericDistribution(_BaseStrategy):
    name = "numeric_distribution"

    def can_generate(self, column: ColumnProfile) -> bool:
        return (
            column.logical_type in (LogicalType.INTEGER, LogicalType.DECIMAL, LogicalType.FLOAT)
            and column.numeric is not None
            and column.numeric.minimum is not None
        )

    def generate(self, plan: StrategyPlan, context: GenerationContext) -> Any:
        col = plan.profile
        numeric = col.numeric
        if numeric is None or numeric.minimum is None:
            raise GenerationError(f"no numeric range for '{col.name}'")
        lo = Decimal(numeric.minimum)
        hi = Decimal(numeric.maximum or numeric.minimum)
        if col.logical_type == LogicalType.INTEGER:
            span = int(hi) - int(lo)
            return int(lo) + context.rng.randint(0, max(span, 0))
        raw = lo + (hi - lo) * Decimal(str(context.rng.random()))
        if col.logical_type == LogicalType.DECIMAL:
            scale = numeric.scale
            if scale is None:
                # infer from observed strings
                scale = max(
                    (
                        len(v.split(".")[1])
                        for v in (numeric.minimum or "", numeric.maximum or "")
                        if "." in v
                    ),
                    default=0,
                )
            return float(round(raw, scale))
        return float(raw)


class BooleanRate(_BaseStrategy):
    name = "boolean_rate"

    def can_generate(self, column: ColumnProfile) -> bool:
        return column.logical_type == LogicalType.BOOLEAN

    def generate(self, plan: StrategyPlan, context: GenerationContext) -> Any:
        col = plan.profile
        total = col.row_count_seen or 0
        false_rate = 0.5
        if col.falsy.false_count is not None and total:
            false_rate = col.falsy.false_count / total
        # false_rate here reflects observed False counts among seen rows
        return context.rng.random() >= false_rate


class FormatGenerator(_BaseStrategy):
    """Format-compatible synthetic identifiers (SPEC 9.3)."""

    name = "format_generator"

    def can_generate(self, column: ColumnProfile) -> bool:
        return column.logical_type in (
            LogicalType.IDENTIFIER,
            LogicalType.PHONE,
            LogicalType.URL,
            LogicalType.IP_ADDRESS,
            LogicalType.POSTAL_CODE,
            LogicalType.COUNTRY_CODE,
        )

    def generate(self, plan: StrategyPlan, context: GenerationContext) -> Any:
        col = plan.profile
        lt = col.logical_type
        if lt == LogicalType.PHONE:
            # fictional/test range shapes; values are synthetic, not guaranteed
            # unassigned (documented in report warnings)
            return f"+1-555-{context.rng.randint(000, 999):03d}-{context.rng.randint(0, 9999):04d}"
        if lt == LogicalType.URL:
            return f"https://{context.faker.domain_name()}/item/{context.rng.randint(1, 999999)}"
        if lt == LogicalType.IP_ADDRESS:
            return f"10.{context.rng.randint(0, 255)}.{context.rng.randint(0, 255)}.{context.rng.randint(1, 254)}"
        if lt == LogicalType.POSTAL_CODE:
            return f"{context.rng.randint(10000, 99999)}"
        if lt == LogicalType.COUNTRY_CODE:
            return context.faker.country_code()
        # identifier: preserve shape from observed patterns
        patterns = [p.regex for p in col.format_patterns if p.regex]
        if patterns:
            shape = max(patterns, key=lambda p: len(p))
            return _fill_shape(shape, context)
        return f"ID{context.rng.randint(100000, 999999)}"


class StringSampler(_BaseStrategy):
    name = "string_sampler"

    def can_generate(self, column: ColumnProfile) -> bool:
        return column.logical_type in (
            LogicalType.STRING,
            LogicalType.CATEGORICAL_STRING,
        )

    def generate(self, plan: StrategyPlan, context: GenerationContext) -> Any:
        col = plan.profile
        length = 8
        if col.length is not None and col.length.minimum is not None:
            lo = col.length.minimum
            hi = col.length.maximum or lo
            length = context.rng.randint(lo, max(hi, lo))
        return context.faker.lexify("?" * length)


class RedactPlaceholder(_BaseStrategy):
    """Free text defaults to redaction (SPEC 9.3)."""

    name = "redact_placeholder"

    def can_generate(self, column: ColumnProfile) -> bool:
        return column.logical_type == LogicalType.FREE_TEXT

    def generate(self, plan: StrategyPlan, context: GenerationContext) -> Any:
        return "[synthetic free text]"


class JsonShape(_BaseStrategy):
    name = "json_shape"

    def can_generate(self, column: ColumnProfile) -> bool:
        return column.logical_type == LogicalType.JSON

    def generate(self, plan: StrategyPlan, context: GenerationContext) -> Any:
        # shape not learned in v1 without samples: emit an empty object
        return {}


class NameFaker(_BaseStrategy):
    name = "faker_name"

    def can_generate(self, column: ColumnProfile) -> bool:

        # semantic hint only from name heuristic (label == name)
        return any(f.label.value == "name" for f in column.sensitivity)

    def generate(self, plan: StrategyPlan, context: GenerationContext) -> Any:
        col = plan.profile
        name = col.name.lower()
        if "first" in name:
            return context.faker.first_name()
        if "last" in name or "surname" in name:
            return context.faker.last_name()
        return context.faker.name()


# ------------------------------------------------------------------- registry

#: ordered by SPEC 9.1 preference (lower index wins when multiple can_generate)
STRATEGY_ORDER: list[GenerationStrategy] = [
    NameFaker(),
    FormatEmail(),
    FormatUuid(),
    TemporalSampler(),
    BooleanRate(),
    NumericDistribution(),
    FormatGenerator(),
    CategoricalSampler(),
    StringSampler(),
    RedactPlaceholder(),
    JsonShape(),
]


def resolve_strategy(
    column: ColumnProfile,
    context: GenerationContext,
    overrides: dict[str, str] | None = None,
) -> StrategyPlan:
    """Pick a strategy; raise GenerationError when nothing supports the column.

    Explicit user overrides win (SPEC 9.1 step 1); everything else walks
    the ordered registry. Failure is loud — never silently wrong data.
    """
    if overrides and column.name in overrides:
        wanted = overrides[column.name]
        for strat in STRATEGY_ORDER:
            if strat.name == wanted:
                plan = strat.plan(column, context)
                plan.strategy = wanted
                return plan
        raise GenerationError(
            f"unknown strategy '{wanted}' for column '{column.name}'",
            detail={"column": column.name, "strategy": wanted},
        )

    fallback_allowed = context.plan.allow_fallbacks
    for strat in STRATEGY_ORDER:
        if strat.can_generate(column):
            plan = strat.plan(column, context)
            plan.strategy = strat.name
            return plan

    if fallback_allowed:
        # documented fallback: type-aware generic with explicit flag
        return StrategyPlan(
            strategy="fallback_constant",
            column=column.name,
            column_profile=column,
            params={"value": None},
            fallback=True,
        )
    raise GenerationError(
        f"no strategy can generate column '{column.name}' "
        f"(logical_type={column.logical_type.value}); "
        "define a strategy override or pass --allow-fallbacks",
        detail={"column": column.name, "logical_type": column.logical_type.value},
    )


def execute(plan: StrategyPlan, context: GenerationContext) -> Any:
    """Run a resolved strategy plan."""
    if plan.strategy == "fallback_constant":
        return plan.params.get("value")
    for strat in STRATEGY_ORDER:
        if strat.name == plan.strategy:
            return strat.generate(plan, context)
    raise GenerationError(f"unknown strategy '{plan.strategy}'")


def _fill_shape(shape: str, context: GenerationContext) -> str:
    """Replace digit runs in a normalized pattern with random digits."""
    return re.sub(
        r"9+",
        lambda m: "".join(str(context.rng.randint(0, 9)) for _ in range(len(m.group()))),
        shape,
    )
