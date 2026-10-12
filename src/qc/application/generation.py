"""Generation use cases: plan, run, validate (SPEC sections 9, 3.3)."""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qc.artifacts.profiles import find_profile, load_profile
from qc.domain.enums import OperationKind
from qc.domain.errors import GenerationError, NotFoundError, ValidationError
from qc.domain.models import GenerationPlan, GenerationReport, StrategyOverride
from qc.generation.strategies import GenerationContext, execute, resolve_strategy
from qc.generation.validation import validate_generation
from qc.security.policy import DecisionContext, Operation

if TYPE_CHECKING:
    from qc.application.runtime import Runtime


def create_plan(
    rt: Runtime,
    *,
    profile_ref: str,
    rows: int,
    seed: int = 0,
    locale: str = "en_US",
    policy_name: str | None = None,
    output: Path | None = None,
    allow_fallbacks: bool = False,
    strategy_overrides: dict[str, str] | None = None,
) -> GenerationPlan:
    """Build a GenerationPlan from a saved profile after policy checks.

    ``strategy_overrides`` maps column -> strategy name (SPEC 9.1 step 1:
    explicit user-defined strategies win over the registry).
    """
    engine = rt.engine(policy_name)
    path = rt.state.find_profile_index(profile_ref) or find_profile(rt.paths.profiles, profile_ref)
    artifact = load_profile(path)
    # policy keys are source *names*; artifacts store opaque ids
    src_cfg = rt.sources.get_by_id(artifact.source_id)
    if src_cfg is None:
        raise NotFoundError(
            "profile's source is not configured in this project "
            "(sources may have been re-created); re-run profiling"
        )
    ctx = DecisionContext(source=src_cfg.name, dataset=artifact.dataset.qualified_name)
    engine.require(Operation.DATASET_READ, ctx)

    if rows > rt.ceiling.max_rows:
        raise ValidationError(
            f"requested {rows} rows exceeds ceiling {rt.ceiling.max_rows}",
        )
    known_columns = {c.name for c in artifact.columns}
    overrides = [
        StrategyOverride(column=col, strategy=strat)
        for col, strat in (strategy_overrides or {}).items()
    ]
    for ov in overrides:
        if ov.column not in known_columns:
            raise ValidationError(
                f"strategy override names unknown column '{ov.column}'",
                detail={"column": ov.column},
            )
    plan = GenerationPlan(
        profile_ids=[artifact.profile_id],
        dataset=artifact.dataset,
        rows=rows,
        seed=seed,
        locale=locale,
        output_format=(output.suffix.lstrip(".") if output else "jsonl"),
        allow_fallbacks=allow_fallbacks,
        strategy_overrides=overrides,
        budget=artifact.budget,
    )
    plan_path = rt.paths.reports / f"{plan.plan_id}.plan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(plan.json_safe(), indent=2), encoding="utf-8")
    rt.audit.record(
        OperationKind.GENERATE,
        source_id=artifact.source_id,
        dataset_id=artifact.dataset.qualified_name,
        policy_version=engine.policy.version,
        decision="allow",
        result_status="ok",
        detail={"plan_id": plan.plan_id, "rows": str(rows)},
    )
    return plan


def plan_path(rt: Runtime, plan: GenerationPlan) -> Path:
    return rt.paths.reports / f"{plan.plan_id}.plan.json"


def _ensure_unique(
    value: Any,
    column: str,
    seen: set[str],
    strat_plan: Any,
    context: Any,
    max_attempts: int = 100,
) -> Any:
    """Bounded retry for unique columns; raises when the strategy cannot
    satisfy uniqueness (SPEC 9.6: never silently weaken a constraint)."""
    key = f"{type(value).__name__}:{value}"
    attempts = 0
    while key in seen and attempts < max_attempts:
        value = execute(strat_plan, context)
        key = f"{type(value).__name__}:{value}"
        attempts += 1
    if key in seen:
        raise GenerationError(
            f"strategy '{strat_plan.strategy}' could not produce a unique "
            f"value for column '{column}' within {max_attempts} attempts; "
            "define a strategy override or reduce row count",
            detail={"column": column, "strategy": strat_plan.strategy},
        )
    seen.add(key)
    return value


def _check_unique_feasibility(resolved: dict[str, Any], unique_cols: set[str], rows: int) -> None:
    """Reject impossible uniqueness before generating a single row.

    A bounded numeric range that cannot yield ``rows`` distinct values
    fails immediately with an actionable error instead of exhausting
    per-row retries (SPEC 9.6).
    """
    for name in sorted(unique_cols):
        plan = resolved[name]
        col = plan.profile
        if col is None or col.numeric is None:
            continue
        if plan.strategy != "numeric_distribution":
            continue
        if col.numeric.minimum is None or col.numeric.maximum is None:
            continue
        try:
            lo = int(float(col.numeric.minimum))
            hi = int(float(col.numeric.maximum))
        except (TypeError, ValueError):
            continue
        space = hi - lo + 1
        if space < rows:
            raise GenerationError(
                f"column '{name}' requires {rows} unique values but its "
                f"observed range [{col.numeric.minimum}, {col.numeric.maximum}] "
                f"holds only {space}; reduce --rows, override the strategy, "
                "or drop the uniqueness requirement",
                detail={
                    "column": name,
                    "value_space": str(space),
                    "rows_requested": str(rows),
                },
            )


async def run_generation(
    rt: Runtime,
    *,
    plan_file: Path,
    output: Path | None = None,
    report_path: Path | None = None,
    seed: int | None = None,
    strict: bool = False,
    allow_fallbacks: bool = False,
) -> dict[str, Any]:
    """Execute a generation plan: synthesize, validate, report."""
    from faker import Faker

    from qc.exports.csv_writer import write_csv_rows

    if not plan_file.is_file():
        raise NotFoundError(f"plan not found: {plan_file.name}")
    plan = GenerationPlan.model_validate(json.loads(plan_file.read_text(encoding="utf-8")))
    if seed is not None:
        plan.seed = seed
    if allow_fallbacks:
        plan.allow_fallbacks = True

    # Load all referenced profiles
    profiles: dict[str, Any] = {}
    for pid in plan.profile_ids:
        p = rt.state.find_profile_index(pid) or find_profile(rt.paths.profiles, pid)
        artifact = load_profile(p)
        profiles[artifact.dataset.qualified_name] = artifact
    if not profiles:
        raise NotFoundError("plan references no loadable profiles")
    primary = next(iter(profiles.values()))

    # Policy check: artifacts store opaque source ids, policy keys are names
    src_cfg = rt.sources.get_by_id(primary.source_id)
    engine = rt.engine()
    engine.require(
        Operation.DATASET_READ,
        DecisionContext(
            source=src_cfg.name if src_cfg else plan.dataset.source_id,
            dataset=plan.dataset.qualified_name,
        ),
    )

    started = time.monotonic()
    rng = random.Random(plan.seed)
    fake = Faker(plan.locale)
    Faker.seed(plan.seed)
    context = GenerationContext(plan=plan, profiles=profiles, faker=fake, rng=rng)

    # Resolve strategies per column (fail-closed before generating rows)
    overrides = {o.column: o.strategy for o in plan.strategy_overrides}
    resolved: dict[str, Any] = {}
    fallbacks: list[str] = []
    for col in primary.columns:
        strat_plan = resolve_strategy(col, context, overrides)
        strat_plan.column_profile = col
        resolved[col.name] = strat_plan
        if strat_plan.fallback:
            fallbacks.append(col.name)

    # Uniqueness requirements (SPEC 9.2 "declared and *approved* inferred
    # uniqueness"): enforce exact uniqueness only where it is actually a
    # key — a declared PK, an identifier-like type, or an id-ish name.
    # Sample-uniqueness on measure columns (e.g. a qty column that
    # happens to be distinct in a small table) is evidence, not a key
    # constraint, and must not make generation fail (SPEC 9.2, 9.6).
    declared_pk = set(primary.dataset_schema.primary_key)
    unique_cols: set[str] = set()
    unenforced_sample_unique: list[str] = []
    for name, sp in resolved.items():
        col = sp.profile
        if col is None or col.unique is not True:
            continue
        if col.unique_measurement.value != "exact":
            continue
        lowered = name.lower()
        keyish = (
            name in declared_pk
            or col.logical_type.value in ("identifier", "uuid")
            or lowered in ("id", "uuid", "guid")
            or lowered.endswith("_id")
            or lowered.endswith("_uuid")
        )
        if keyish:
            unique_cols.add(name)
        else:
            unenforced_sample_unique.append(name)
    seen: dict[str, set[str]] = {c: set() for c in unique_cols}
    pre_warnings: list[str] = []
    if unenforced_sample_unique:
        pre_warnings.append(
            "sample-uniqueness not enforced for non-key columns: "
            + ", ".join(sorted(unenforced_sample_unique))
        )

    # Fail fast when a unique key's value space cannot fit the request
    # (SPEC 9.6: stop and report; never silently weaken the constraint).
    _check_unique_feasibility(resolved, unique_cols, plan.rows)

    # Observed null rates (SPEC 9.2): preserve within tolerance using the
    # seeded RNG so output stays reproducible. Rate comes from the profile,
    # not from re-reading the source. Unique columns never emit nulls here
    # (their profile null_count is 0 by construction).
    null_rates: dict[str, float] = {}
    for col in primary.columns:
        total = col.row_count_seen or 0
        nulls = col.falsy.null_count
        if total and nulls and col.name not in unique_cols:
            null_rates[col.name] = nulls / total

    rows: list[dict[str, Any]] = []
    warnings: list[str] = list(pre_warnings)
    for i in range(plan.rows):
        context.row_index = i
        row: dict[str, Any] = {}
        for col_name, strat_plan in resolved.items():
            rate = null_rates.get(col_name, 0.0)
            if rate > 0 and rng.random() < rate:
                row[col_name] = None
                continue
            value = execute(strat_plan, context)
            if col_name in unique_cols:
                value = _ensure_unique(value, col_name, seen[col_name], strat_plan, context)
            row[col_name] = value
        rows.append(row)

    # Validate against profile (SPEC 9.5)
    metrics = validate_generation(rows, primary, tolerances=None)
    hard_failures = [m for m in metrics if m.hard and not m.passed]
    soft_failures = [m for m in metrics if not m.hard and not m.passed]

    if plan.preserve_relationships:
        for rel in primary.relationships:
            if rel.kind == "foreign_key" and rel.verified:
                warnings.append(
                    f"relationship {rel.from_columns}->{rel.to_columns} "
                    "preservation is declarative in v1"
                )

    warnings.extend(
        [
            "synthetic data is NOT anonymous; empirical-distribution synthesis "
            "can leak information (no differential privacy claimed)",
            "phone-like values use fictional 555 ranges but are not guaranteed unassigned",
        ]
    )
    if fallbacks:
        warnings.append(f"fallback strategies used for: {', '.join(fallbacks)}")

    # Write output
    out_path = output or (rt.paths.output / f"synthetic.{plan.output_format}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if plan.output_format == "csv":
        write_csv_rows(out_path, rows)
    else:
        with out_path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    report = GenerationReport(
        plan_id=plan.plan_id,
        rows_requested=plan.rows,
        rows_generated=len(rows),
        seed=plan.seed,
        metrics=metrics,
        constraint_violations=[
            f"{m.name}: generated {m.generated} vs observed {m.observed}" for m in hard_failures
        ],
        strategy_fallbacks=fallbacks,
        warnings=warnings,
        preserved_properties=[
            m.name
            for m in metrics
            if m.passed and m.name.endswith(("null_rate", "categorical_tvd"))
        ],
        approximated_properties=[m.name for m in soft_failures],
        runtime_seconds=time.monotonic() - started,
        status="failed" if hard_failures or (strict and soft_failures) else "completed",
        output_path=str(out_path),
    )
    rpath = report_path or (rt.paths.reports / f"{report.report_id}.report.json")
    rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(report.json_safe(), indent=2), encoding="utf-8")

    rt.audit.record(
        OperationKind.GENERATE,
        source_id=primary.source_id,
        dataset_id=plan.dataset.qualified_name,
        policy_version=engine.policy.version,
        decision="allow",
        rows_affected=len(rows),
        duration_seconds=report.runtime_seconds,
        result_status=report.status,
        detail={"report_id": report.report_id, "hard_failures": str(len(hard_failures))},
    )
    ok = not hard_failures and not (strict and soft_failures)
    return {
        "ok": ok,
        "report": report,
        "output": str(out_path),
        "report_path": str(rpath),
        "hard_failures": len(hard_failures),
        "soft_failures": len(soft_failures),
    }


async def validate_output(rt: Runtime, *, output: Path, profile_ref: str) -> GenerationReport:
    """Validate an existing generated file against a profile."""
    if not output.is_file():
        raise NotFoundError(f"output not found: {output.name}")
    path = rt.state.find_profile_index(profile_ref) or find_profile(rt.paths.profiles, profile_ref)
    artifact = load_profile(path)

    rows: list[dict[str, Any]] = []
    if output.suffix == ".csv":
        import csv

        with output.open(encoding="utf-8", newline="") as fh:
            for raw in csv.DictReader(fh):
                rows.append({k: _maybe_number(v) for k, v in raw.items()})
    else:
        for line in output.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))

    metrics = validate_generation(rows, artifact)
    hard = [m for m in metrics if m.hard and not m.passed]
    soft = [m for m in metrics if not m.hard and not m.passed]
    from qc import TOOL_VERSION

    return GenerationReport(
        plan_id="validation",
        rows_requested=len(rows),
        rows_generated=len(rows),
        metrics=metrics,
        constraint_violations=[m.name for m in hard],
        approximated_properties=[m.name for m in soft],
        status="failed" if hard else "completed",
        tool_version=TOOL_VERSION,
        output_path=str(output),
        warnings=["validation run against saved profile; no source data accessed"],
    )


def _maybe_number(v: str) -> Any:
    if v == "\\N" or v == "":
        return None
    if v in ("true", "false"):
        return v == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v
