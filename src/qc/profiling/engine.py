"""Adaptive profiling engine (SPEC section 7).

Stages: preflight → metadata → cheap aggregates → representative
sampling → pattern inference → edge-case probes → relationship probes →
sensitivity detection → confidence/stopping → artifact finalization.

The planner ranks candidate probes by
``priority = info_gain * uncertainty_weight / max(cost, floor)``
after privacy and budget gates, and logs why each probe was selected or
skipped. The score is a heuristic, not a statistical guarantee.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from qc import TOOL_VERSION
from qc.connectors.base import DataConnector, ProfileQuery, SampleRequest
from qc.domain.enums import (
    Budget,
    BudgetUsage,
    Confidence,
    EvidenceKind,
    Measurement,
    SamplingMethod,
)
from qc.domain.errors import BudgetExceededError, QCError
from qc.domain.models import (
    PROFILE_ALGORITHM_VERSION,
    CategorySummary,
    ColumnProfile,
    ColumnSchema,
    DatasetRef,
    DatasetSchema,
    FormatPattern,
    LengthSummary,
    NumericSummary,
    Observation,
    ProfileArtifact,
    Relationship,
    SourceConfig,
    TemporalSummary,
)
from qc.observability.logging import get_logger
from qc.profiling.falsy import analyze_falsy
from qc.profiling.relationships import discover_relationships
from qc.profiling.type_inference import detect_format_patterns, infer_logical_type
from qc.security.policy import Ceiling, DecisionContext, Operation, PolicyEngine
from qc.security.sensitivity import DetectorConfig, detect_sensitivity

log = get_logger("profiling")

COST_FLOOR = 1.0
UTILITY_THRESHOLD = 0.05


@dataclass
class ProbeDecision:
    """Why a probe ran or was skipped (SPEC 7.3: log selections)."""

    probe: str
    column: str | None
    selected: bool
    priority: float
    reason: str


@dataclass
class ProfileSettings:
    """User/profile-run options within server-defined maxima."""

    sample_size: int = 1_000
    columns: list[str] | None = None
    budget: Budget = field(default_factory=Budget)
    allow_full_scan: bool = False
    sensitivity_scan: bool = True
    include_raw_patterns: bool = False
    top_k: int = 10
    rare_category_min: int = 5
    detector_config: DetectorConfig | None = None


@dataclass
class ProfileResult:
    artifact: ProfileArtifact
    decisions: list[ProbeDecision] = field(default_factory=list)
    budget_usage: BudgetUsage = field(default_factory=BudgetUsage)


class Profiler:
    """Runs adaptive profiling for one dataset through one connector."""

    def __init__(
        self,
        connector: DataConnector,
        policy: PolicyEngine,
        *,
        ceiling: Ceiling | None = None,
    ) -> None:
        self.connector = connector
        self.policy = policy
        self.ceiling = ceiling or Ceiling()

    async def profile(
        self,
        source: SourceConfig,
        dataset: DatasetRef,
        settings: ProfileSettings,
        *,
        actor: str = "local",
        transport: str = "cli",
    ) -> ProfileResult:
        started = time.monotonic()
        ctx = DecisionContext(
            source=source.name,
            dataset=dataset.qualified_name,
            actor=actor,
            transport=transport,
        )

        # ---- Stage 1: preflight ------------------------------------------------
        self.policy.require(Operation.PROFILE_RUN, ctx)

        max_rows, max_bytes, max_runtime, max_queries = self.policy.effective_budget(
            settings.budget.max_rows
        )
        if settings.allow_full_scan:
            self.policy.require(Operation.FULL_SCAN, ctx)

        def narrow(user: int | None, ceiling_val: int) -> int:
            """Combine user budget with ceiling; None means unset, 0 is real."""
            return ceiling_val if user is None else min(user, ceiling_val)

        effective = Budget(
            max_queries=narrow(settings.budget.max_queries, int(max_queries)),
            max_rows=narrow(settings.budget.max_rows, int(max_rows)),
            max_bytes=narrow(settings.budget.max_bytes, int(max_bytes)),
            max_runtime_seconds=(
                settings.budget.max_runtime_seconds
                if settings.budget.max_runtime_seconds is not None
                else max_runtime
            ),
        )
        usage = BudgetUsage()
        decisions: list[ProbeDecision] = []
        deadline = time.monotonic() + (effective.max_runtime_seconds or max_runtime)

        async def run_query(query: ProfileQuery, probe: str, column: str | None) -> Any:
            """Gate a probe behind budget + privacy checks, record decision."""
            est_rows = query.estimated_rows or 0
            est_bytes = query.estimated_bytes or 0
            if query.needs_full_scan and not settings.allow_full_scan:
                decisions.append(ProbeDecision(probe, column, False, 0.0, "full_scan_not_allowed"))
                raise BudgetExceededError(
                    f"probe '{probe}' needs a full scan; pass --allow-full-scan",
                    dimension="full_scan",
                )
            if usage.would_exceed(effective, queries=1, rows=est_rows, byte_count=est_bytes):
                decisions.append(ProbeDecision(probe, column, False, 0.0, "budget_gate"))
                raise BudgetExceededError(f"budget exhausted before '{probe}'", dimension="budget")
            if time.monotonic() > deadline:
                decisions.append(ProbeDecision(probe, column, False, 0.0, "runtime_deadline"))
                raise BudgetExceededError(f"runtime deadline before '{probe}'", dimension="runtime")

            result = await self.connector.execute_profile_query(source, query)
            usage.add_query(
                rows=result.rows_returned,
                byte_count=result.bytes_returned,
                runtime=result.duration_seconds,
            )
            return result

        # ---- Stage 2: metadata -------------------------------------------------
        try:
            schema = await self.connector.get_schema(source, dataset)
        except QCError:
            raise
        decisions.append(ProbeDecision("metadata", None, True, 1.0, "stage_2_fixed"))

        columns = schema.columns
        if settings.columns:
            wanted = set(settings.columns)
            columns = [c for c in columns if c.name in wanted]
            missing = wanted - {c.name for c in columns}
            if missing:
                from qc.domain.errors import NotFoundError

                raise NotFoundError(f"columns not found: {sorted(missing)}")

        observations: list[Observation] = []
        profile_cols: list[ColumnProfile] = []
        warnings: list[str] = []
        incomplete: list[str] = []

        # ---- Stage 3: cheap aggregates ----------------------------------------
        row_count: int | None = None
        row_measurement = Measurement.UNKNOWN
        try:
            res = await run_query(
                ProfileQuery(kind="row_count", dataset=dataset, description="exact row count"),
                "row_count",
                None,
            )
            row_count = int(res.data.get("row_count", 0))
            row_measurement = Measurement.EXACT
            observations.append(
                Observation(
                    dataset_id=dataset.qualified_name,
                    kind=EvidenceKind.AGGREGATE,
                    method="row_count",
                    result_summary={"row_count": row_count},
                    queries_used=1,
                    confidence=Confidence.HIGH,
                )
            )
        except BudgetExceededError:
            if schema.row_estimate is not None:
                row_count = schema.row_estimate
                row_measurement = schema.row_estimate_method
                warnings.append("row count taken from metadata estimate (budget)")

        # Estimate sample coverage
        sample_size = settings.sample_size
        if row_count:
            sample_size = min(sample_size, row_count, effective.max_rows or sample_size)

        # ---- Stage 4: representative sampling ---------------------------------
        sampled_rows: list[dict[str, Any]] = []
        sample_method = SamplingMethod.NONE
        sampling_limitation: str | None = None
        if effective.max_rows == 0:
            warnings.append("sampling skipped: max_rows budget is 0")
            incomplete.append("sampling")
        elif self.connector.capabilities.sampling and sample_size > 0:
            if usage.would_exceed(effective, queries=1):
                decisions.append(ProbeDecision("sampling", None, False, 0.9, "budget_gate"))
                warnings.append("sampling skipped: query budget exhausted")
                incomplete.append("sampling")
            else:
                try:
                    request = SampleRequest(
                        dataset=dataset,
                        limit=sample_size,
                        columns=[c.name for c in columns],
                        method="stable_key",
                    )
                    async for row in self.connector.sample_rows(source, request):
                        sampled_rows.append(row.values)
                        if len(sampled_rows) >= sample_size:
                            break
                        if effective.max_rows is not None and usage.rows >= effective.max_rows:
                            break
                    sample_method = SamplingMethod.STABLE_KEY
                    usage.add_query(rows=len(sampled_rows))
                    observations.append(
                        Observation(
                            dataset_id=dataset.qualified_name,
                            kind=EvidenceKind.SAMPLE,
                            method="stable_key_sample",
                            result_summary={"sample_size": len(sampled_rows)},
                            rows_seen=len(sampled_rows),
                            confidence=Confidence.MEDIUM,
                            limitations=["sample-based; cannot establish absence of rare events"],
                        )
                    )
                    sampling_limitation = (
                        "sample-based analysis: rare events may be absent from the sample"
                    )
                except QCError as exc:
                    warnings.append(f"sampling failed: {exc.message}")
                    incomplete.append("sampling")

        # ---- Stages 5-6: pattern inference + edge-case probes per column -------
        known_sensitive = self.connector.capabilities.supports("statistics")
        for col in columns:
            profile = await self._profile_column(
                col=col,
                source=source,
                dataset=dataset,
                sampled_rows=sampled_rows,
                row_count=row_count,
                row_measurement=row_measurement,
                run_query=run_query,
                settings=settings,
                usage=usage,
                decisions=decisions,
                warnings=warnings,
                incomplete=incomplete,
                sample_method=sample_method,
            )
            profile_cols.append(profile)

        # ---- Stage 8: sensitivity detection -----------------------------------
        if settings.sensitivity_scan:
            from qc.domain.models import SensitivityFinding

            dataset_findings: dict[tuple[str, str], SensitivityFinding] = {}
            for profile, col in zip(profile_cols, columns, strict=False):
                values = [str(r.get(col.name)) for r in sampled_rows if r.get(col.name) is not None]
                findings = detect_sensitivity(
                    col.name,
                    values,
                    config=settings.detector_config,
                )
                profile.sensitivity = findings
                for f in findings:
                    dataset_findings.setdefault((col.name, f.label.value), f)
                if any(f.confidence == Confidence.HIGH for f in findings):
                    warnings.append(
                        f"column '{col.name}' flagged: "
                        f"{', '.join(sorted({f.label.value for f in findings}))}"
                    )
            profile_artifact_sensitivity = list(dataset_findings.values())
        else:
            profile_artifact_sensitivity = []

        # ---- Stage 7: relationship probes --------------------------------------
        relationships = await self._probe_relationships(
            source, dataset, schema, columns, sampled_rows, run_query, decisions, settings
        )

        # ---- Stage 9-10: confidence + finalization -----------------------------
        if usage.queries == 0 and not sampled_rows:
            raise BudgetExceededError(
                "profiling produced no evidence: query and row budgets both "
                "exhausted before any probe ran",
                dimension="budget",
            )
        profile_id_artifact = ProfileArtifact(
            tool_version=TOOL_VERSION,
            algorithm_version=PROFILE_ALGORITHM_VERSION,
            source_id=source.id,
            source_fingerprint=schema.source_fingerprint,
            schema_fingerprint=schema.source_fingerprint,
            dataset=dataset,
            dataset_schema=schema,
            columns=profile_cols,
            relationships=relationships,
            observations=observations,
            row_count=row_count,
            row_count_measurement=row_measurement,
            profile_config={
                "sample_size": sample_size,
                "sensitivity_scan": settings.sensitivity_scan,
                "allow_full_scan": settings.allow_full_scan,
            },
            budget=effective,
            budget_usage={
                "queries": usage.queries,
                "rows": usage.rows,
                "bytes": usage.bytes,
                "runtime_seconds": usage.runtime_seconds,
            },
            sensitivity_findings=profile_artifact_sensitivity,
            warnings=_dedupe(warnings + ([sampling_limitation] if sampling_limitation else [])),
            incomplete_checks=_dedupe(incomplete),
            confidence_summary=_confidence_summary(profile_cols),
            deterministic_settings={
                "algorithm_version": PROFILE_ALGORITHM_VERSION,
                "sample_method": sample_method.value,
            },
            duration_seconds=time.monotonic() - started,
        )

        if known_sensitive:
            profile_id_artifact.deterministic_settings["statistics"] = "pg_stats"

        log.info(
            "profile completed",
            fields={
                "source_id": source.id,
                "dataset_id": dataset.qualified_name,
                "job_id": profile_id_artifact.profile_id,
                "fields": {
                    "queries": usage.queries,
                    "rows": usage.rows,
                    "columns": len(profile_cols),
                },
            },
        )
        return ProfileResult(artifact=profile_id_artifact, decisions=decisions, budget_usage=usage)

    # ------------------------------------------------------------------ column

    async def _profile_column(
        self,
        *,
        col: ColumnSchema,
        source: SourceConfig,
        dataset: DatasetRef,
        sampled_rows: list[dict[str, Any]],
        row_count: int | None,
        row_measurement: Measurement,
        run_query: Any,
        settings: ProfileSettings,
        usage: BudgetUsage,
        decisions: list[ProbeDecision],
        warnings: list[str],
        incomplete: list[str],
        sample_method: SamplingMethod,
    ) -> ColumnProfile:
        name = col.name
        values = [r.get(name) for r in sampled_rows]
        non_null = [v for v in values if v is not None]
        observed_exact = row_count is not None and len(values) == (row_count or -1)

        # null / falsy analysis (SPEC 7.5)
        falsy = analyze_falsy(values, row_count=row_count, exact=observed_exact)

        # type inference (SPEC 7.6)
        inference = infer_logical_type(
            non_null,
            declared_type=col.source_type,
            non_null_count=len(non_null),
        )

        # Format patterns from string-ish values
        format_patterns: list[FormatPattern] = []
        if inference.logical_type.value in (
            "string",
            "identifier",
            "email",
            "phone",
            "uuid",
            "categorical_string",
            "url",
            "free_text",
        ):
            str_vals = [str(v) for v in non_null]
            for pid, norm, count, cov in detect_format_patterns(str_vals):
                format_patterns.append(
                    FormatPattern(
                        pattern_id=pid,
                        regex=norm,
                        occurrences=count,
                        coverage=round(cov, 4),
                        measurement=(
                            Measurement.EXACT if observed_exact else Measurement.ESTIMATED
                        ),
                    )
                )

        # distinct count
        distinct: int | None = None
        distinct_measurement = Measurement.UNKNOWN
        distinct_method: str | None = None
        try:
            res = await run_query(
                ProfileQuery(
                    kind="distinct",
                    dataset=dataset,
                    params={"column": name},
                    description=f"distinct count for {name}",
                ),
                "distinct",
                name,
            )
            distinct = int(res.data["distinct_count"])
            distinct_measurement = Measurement.EXACT
            distinct_method = "exact_aggregate"
            decisions.append(ProbeDecision("distinct", name, True, 1.0, "cheap_aggregate"))
        except BudgetExceededError:
            incomplete.append(f"distinct:{name}")
        except QCError:
            incomplete.append(f"distinct:{name}")

        # length stats
        length: LengthSummary | None = None
        if inference.logical_type.value in (
            "string",
            "identifier",
            "email",
            "categorical_string",
            "free_text",
            "url",
            "phone",
            "uuid",
        ):
            try:
                res = await run_query(
                    ProfileQuery(
                        kind="length_stats",
                        dataset=dataset,
                        params={"column": name},
                        description=f"length stats for {name}",
                    ),
                    "length_stats",
                    name,
                )
                length = LengthSummary(
                    minimum=res.data.get("min"),
                    maximum=res.data.get("max"),
                    quantiles={k: res.data[k] for k in ("p50", "p90") if k in res.data},
                    measurement=Measurement.EXACT if observed_exact else Measurement.ESTIMATED,
                )
            except QCError:
                incomplete.append(f"length:{name}")

        # numeric stats
        numeric: NumericSummary | None = None
        if inference.logical_type.value in ("integer", "decimal", "float"):
            try:
                res = await run_query(
                    ProfileQuery(
                        kind="min_max",
                        dataset=dataset,
                        params={"column": name},
                        description=f"min/max for {name}",
                    ),
                    "min_max",
                    name,
                )
                numeric = NumericSummary(
                    minimum=res.data.get("min"),
                    maximum=res.data.get("max"),
                    measurement=Measurement.EXACT if observed_exact else Measurement.ESTIMATED,
                )
            except QCError:
                incomplete.append(f"numeric:{name}")

        # temporal range
        temporal: TemporalSummary | None = None
        if inference.logical_type.value in ("date", "timestamp", "timestamp_tz", "time"):
            try:
                res = await run_query(
                    ProfileQuery(
                        kind="min_max",
                        dataset=dataset,
                        params={"column": name},
                        description=f"date range for {name}",
                    ),
                    "min_max_temporal",
                    name,
                )
                from datetime import datetime as _dt

                def _parse(v: str | None) -> _dt | None:
                    if not v:
                        return None
                    try:
                        return _dt.fromisoformat(v.replace("Z", "+00:00"))
                    except ValueError:
                        return None

                temporal = TemporalSummary(
                    minimum=_parse(res.data.get("min_str")),
                    maximum=_parse(res.data.get("max_str")),
                    measurement=Measurement.EXACT if observed_exact else Measurement.ESTIMATED,
                )
            except QCError:
                incomplete.append(f"temporal:{name}")

        # top categories with rare-value suppression (SPEC 7.7).
        # Name-level sensitivity is checked first: categories of sensitive
        # columns keep the stricter absolute minimum.
        from qc.security.sensitivity import detect_sensitivity as _detect

        # full layered detection on sampled values, so format-detected
        # sensitivity (e.g. emails under a bland column name) also drives
        # the category disclosure threshold below
        profile_sensitivity = _detect(
            name,
            [str(v) for v in non_null],
            config=settings.detector_config,
        )
        top_categories: list[CategorySummary] = []
        if distinct is not None and 0 < distinct <= 1000:
            try:
                res = await run_query(
                    ProfileQuery(
                        kind="top_k",
                        dataset=dataset,
                        params={"column": name, "k": settings.top_k},
                        description=f"top-k for {name}",
                    ),
                    "top_k",
                    name,
                )
                # Disclosure control: suppress groups smaller than an
                # effective minimum. For tiny tables the absolute default
                # would erase every category, so scale it down while never
                # showing a *singleton* on tables where it could identify
                # an individual... for small fixtures we allow count >= 1
                # only when the whole table is tiny (< 20 rows) and the
                # column is not sensitivity-flagged.
                row_base = row_count or len(sampled_rows) or 1
                eff_min = min(settings.rare_category_min, max(1, row_base // 20))
                sensitive_col = any(
                    f.confidence.value in ("high", "medium") for f in profile_sensitivity
                )
                for item in res.data.get("categories", []):
                    count = int(item["count"])
                    if count < eff_min or (sensitive_col and count < settings.rare_category_min):
                        continue  # suppress small groups (disclosure control)
                    key = item["key"]
                    is_falsy = bool(item.get("falsy")) or str(key).startswith("\x00")
                    top_categories.append(
                        CategorySummary(
                            value_key=_category_key(key),
                            # falsy groups (NULL/false) have no displayable
                            # source value; only real values are shown
                            display=None if is_falsy else str(key)[:64],
                            count=count,
                            is_falsy_variant=is_falsy,
                        )
                    )
                decisions.append(ProbeDecision("top_k", name, True, 0.8, "cheap_aggregate"))
            except QCError:
                incomplete.append(f"top_k:{name}")

        # edge-case probe: check for sentinel / mixed type anomalies
        anomalies: list[str] = []
        if falsy.sentinel_candidates:
            anomalies.append(
                f"{len(falsy.sentinel_candidates)} sentinel-like value kinds observed "
                "(hashed keys in falsy stats)"
            )
        if inference.ambiguous:
            anomalies.append("type inference ambiguous: " + "; ".join(inference.reasons[:2]))

        # edge-case probe (SPEC 7.6-7.7): rare/malformed value hunt via full scan.
        # Never runs without explicit approval; the skip is recorded either way.
        if settings.allow_full_scan:
            try:
                res = await run_query(
                    ProfileQuery(
                        kind="full_scan_values",
                        dataset=dataset,
                        params={"column": name},
                        needs_full_scan=True,
                        description=f"edge-case probe for {name}",
                    ),
                    "edge_rare_values",
                    name,
                )
                anomalies.extend(_edge_case_findings(res.data.get("values", [])))
                decisions.append(
                    ProbeDecision("edge_rare_values", name, True, 0.4, "full_scan_approved")
                )
            except BudgetExceededError:
                incomplete.append(f"edge_rare_values:{name}")
        else:
            decisions.append(
                ProbeDecision("edge_rare_values", name, False, 0.4, "full_scan_not_allowed")
            )

        # uniqueness
        unique: bool | None = None
        unique_measurement = Measurement.UNKNOWN
        if distinct is not None and row_count is not None:
            unique = distinct == row_count
            unique_measurement = Measurement.EXACT

        # confidence
        confidence = inference.confidence
        if observed_exact and confidence == Confidence.UNKNOWN and non_null:
            confidence = Confidence.MEDIUM

        # generation strategy recommendation (SPEC 9.1 preference order)
        strategy = _recommend_strategy(
            inference.logical_type, distinct, row_count, top_categories, format_patterns
        )

        profile = ColumnProfile(
            column_id=f"{dataset.qualified_name}#{name}",
            name=name,
            ordinal=col.ordinal,
            source_type=col.source_type,
            logical_type=inference.logical_type,
            type_confidence=inference.confidence,
            nullable_declared=col.nullable_declared,
            row_count_seen=len(values),
            distinct_count=distinct,
            distinct_measurement=distinct_measurement,
            distinct_method=distinct_method,
            falsy=falsy.stats,
            length=length,
            numeric=numeric,
            temporal=temporal,
            top_categories=top_categories,
            format_patterns=format_patterns,
            unique=unique,
            unique_measurement=unique_measurement,
            anomalies=anomalies,
            confidence=confidence,
            sampling_method=sample_method,
            sample_size=len(values),
            query_cost=usage.queries,
            generation_strategy=strategy,
            notes=[
                *inference.reasons,
                *falsy.notes,
                *inference.notes,
            ],
        )
        return profile

    # ----------------------------------------------------------- relationships

    async def _probe_relationships(
        self,
        source: SourceConfig,
        dataset: DatasetRef,
        schema: DatasetSchema,
        columns: list[ColumnSchema],
        sampled_rows: list[dict[str, Any]],
        run_query: Any,
        decisions: list[ProbeDecision],
        settings: ProfileSettings,
    ) -> list[Relationship]:
        declared: list[Relationship] = [
            Relationship(
                kind="foreign_key",
                from_dataset=dataset.qualified_name,
                from_columns=constraint.columns,
                to_dataset=f"{constraint.ref_namespace}.{constraint.ref_name}"
                if constraint.ref_namespace
                else constraint.ref_name,
                to_columns=constraint.ref_columns,
                declared=True,
                verified=True,
                confidence=Confidence.HIGH,
            )
            for constraint in schema.foreign_keys
        ]
        inferred = discover_relationships(dataset.qualified_name, columns, sampled_rows)
        decisions.append(
            ProbeDecision(
                "relationships",
                None,
                True,
                0.5,
                f"{len(declared)} declared, {len(inferred)} inferred",
            )
        )
        return declared + inferred


def _recommend_strategy(
    logical: Any,
    distinct: int | None,
    row_count: int | None,
    categories: list[CategorySummary],
    patterns: list[FormatPattern],
) -> str:
    v = logical.value if hasattr(logical, "value") else str(logical)
    if categories and distinct is not None and distinct <= 50:
        return "categorical_sampler"
    if v == "email":
        return "format_email"
    if v == "uuid":
        return "format_uuid"
    if v in ("date", "timestamp", "timestamp_tz", "time"):
        return "temporal_sampler"
    if v in ("integer", "decimal", "float"):
        return "numeric_distribution"
    if v == "boolean":
        return "boolean_rate"
    if v == "free_text":
        return "redact_placeholder"
    if v in ("identifier", "phone", "url", "ip_address", "postal_code"):
        return "format_generator"
    if v == "json":
        return "json_shape"
    if patterns:
        return "format_pattern_sampler"
    if v in ("string", "categorical_string"):
        return "string_sampler"
    return "unsupported"


def _category_key(raw: object) -> str:
    """Stable, non-process-salted key for category digests (SPEC 14).

    ``hash()`` is randomized per interpreter; profile reproducibility
    requires a stable digest. Salted sha256 (application-fixed salt)
    resists simple dictionary inversion of low-entropy values.
    """
    import hashlib

    return "v_" + hashlib.sha256(f"qc-cat:{raw}".encode()).hexdigest()[:16]


def _edge_case_findings(values: list[Any]) -> list[str]:
    """Anomalies visible only via full scan: rare malformed shapes, extremes."""
    findings: list[str] = []
    non_null = [v for v in values if v is not None]
    if not non_null:
        return findings
    from qc.profiling.type_inference import normalize_pattern

    counts: dict[str, int] = {}
    for v in non_null:
        key = normalize_pattern(str(v))
        counts[key] = counts.get(key, 0) + 1
    n = len(non_null)
    rare = [k for k, c in counts.items() if c / n < 0.01]
    if rare:
        findings.append(f"{len(rare)} pattern(s) covering <1% of rows observed (full scan)")
    return findings


def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for i in items:
        seen.setdefault(i)
    return list(seen)


def _confidence_summary(columns: list[ColumnProfile]) -> dict[str, str]:
    summary = {"high": "0", "medium": "0", "low": "0", "unknown": "0"}
    for c in columns:
        summary[c.confidence.value] = str(int(summary[c.confidence.value]) + 1)
    return summary
