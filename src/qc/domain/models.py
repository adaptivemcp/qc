"""Domain models per SPEC section 5.

All models carry explicit schema versions, use UTC ISO-8601 timestamps,
and use opaque IDs. Nothing here performs I/O.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from qc.domain.enums import (
    Budget,
    ColumnAction,
    Confidence,
    DatasetKind,
    EvidenceKind,
    JobStatus,
    LogicalType,
    Measurement,
    OperationKind,
    ProfileDisclosure,
    SamplingMethod,
    SensitivityLabel,
    utc_now,
)
from qc.domain.enums import QCBaseModel as _Base

PROFILE_ALGORITHM_VERSION = "1.0.0"
SCHEMA_VERSION_PROFILE = 1
SCHEMA_VERSION_POLICY = 1
SCHEMA_VERSION_PLAN = 1
SCHEMA_VERSION_REPORT = 1


def new_id(prefix: str) -> str:
    """Opaque, prefixed UUID4 identifier."""
    return f"{prefix}_{uuid.uuid4().hex}"


# ---------------------------------------------------------------- SourceConfig


class SourceConfig(_Base):
    """Configured data source (SPEC 5.1). Never contains credentials."""

    id: str = Field(default_factory=lambda: new_id("src"))
    name: str
    connector: str
    connection_ref: str  # e.g. "env:DATA_SOURCE_ANALYTICS" or "keyring:qc/analytics"
    default_schema: str | None = None
    read_only: bool = True
    allowed_datasets: list[str] = Field(default_factory=list)
    policy_ref: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    options: dict[str, str] = Field(default_factory=dict)

    @field_validator("connection_ref")
    @classmethod
    def _no_plaintext_credentials(cls, v: str) -> str:
        """Reject obviously-credentialled connection strings in config."""
        lowered = v.lower()
        if "://" in v and "@" in v.split("://", 1)[1]:
            raise ValueError(
                "connection_ref must be a secret reference (env:/keyring:/file:), "
                "not an inline connection string"
            )
        for marker in ("password=", "token=", "secret="):
            if marker in lowered:
                raise ValueError(f"connection_ref must not contain inline '{marker}'")
        if not any(v.startswith(p) for p in ("env:", "keyring:", "file:", "cmd:")):
            raise ValueError("connection_ref must start with env:, keyring:, file:, or cmd:")
        return v


# ------------------------------------------------------------------ DatasetRef


class DatasetRef(_Base):
    """Reference to a dataset within a source (SPEC 5.2)."""

    source_id: str
    namespace: str | None = None
    name: str
    kind: DatasetKind = DatasetKind.TABLE
    provider_id: str | None = None
    locator: str | None = None  # connector-specific opaque locator

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}.{self.name}" if self.namespace else self.name


# --------------------------------------------------------------- DatasetSchema


class ColumnSchema(_Base):
    """Declared column metadata from the source (SPEC 5.3)."""

    name: str
    ordinal: int
    source_type: str
    logical_type: LogicalType = LogicalType.UNSUPPORTED
    nullable_declared: bool | None = None
    default: str | None = None
    is_primary_key: bool = False
    comment: str | None = None


class ForeignKeyConstraint(_Base):
    name: str | None = None
    columns: list[str]
    ref_namespace: str | None = None
    ref_name: str
    ref_columns: list[str]
    declared: bool = True


class DatasetSchema(_Base):
    """Schema of a dataset (SPEC 5.3)."""

    schema_version: int = SCHEMA_VERSION_PROFILE
    dataset: DatasetRef
    source_fingerprint: str
    columns: list[ColumnSchema]
    primary_key: list[str] = Field(default_factory=list)
    primary_key_declared: bool = False
    foreign_keys: list[ForeignKeyConstraint] = Field(default_factory=list)
    unique_constraints: list[list[str]] = Field(default_factory=list)
    row_estimate: int | None = None
    row_estimate_method: Measurement = Measurement.UNKNOWN
    discovered_at: datetime = Field(default_factory=utc_now)
    provider_metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


# ---------------------------------------------------------------- Observation


class Observation(_Base):
    """A single piece of evidence (SPEC 5.5). Raw values are never persisted."""

    id: str = Field(default_factory=lambda: new_id("obs"))
    dataset_id: str
    column_id: str | None = None
    kind: EvidenceKind
    method: str
    algorithm_version: str = PROFILE_ALGORITHM_VERSION
    result_summary: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=utc_now)
    rows_seen: int = 0
    queries_used: int = 0
    bytes_used: int = 0
    scope: str = "full_dataset"
    confidence: Confidence = Confidence.UNKNOWN
    limitations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- ColumnProfile


class LengthSummary(_Base):
    minimum: int | None = None
    maximum: int | None = None
    quantiles: dict[str, int] = Field(default_factory=dict)  # "p50" -> length
    histogram: dict[str, int] = Field(default_factory=dict)  # bucket -> count
    measurement: Measurement = Measurement.UNKNOWN


class NumericSummary(_Base):
    minimum: str | None = None  # decimal-safe: serialized as string
    maximum: str | None = None
    quantiles: dict[str, str] = Field(default_factory=dict)
    precision: int | None = None
    scale: int | None = None
    histogram: dict[str, int] = Field(default_factory=dict)
    mean: str | None = None
    measurement: Measurement = Measurement.UNKNOWN


class TemporalSummary(_Base):
    minimum: datetime | None = None
    maximum: datetime | None = None
    formats: list[str] = Field(default_factory=list)
    measurement: Measurement = Measurement.UNKNOWN


class FalsyStats(_Base):
    """Null/falsy distinctions that must never be collapsed (SPEC 7.5)."""

    null_count: int | None = None
    null_measurement: Measurement = Measurement.UNKNOWN
    empty_string_count: int | None = None
    whitespace_only_count: int | None = None
    zero_count: int | None = None
    false_count: int | None = None
    string_false_count: int | None = None  # literal "false"
    string_zero_count: int | None = None  # literal "0"
    string_null_count: int | None = None  # literal "null"/"None"
    nan_count: int | None = None
    sentinel_candidates: dict[str, int] = Field(default_factory=dict)  # hashed -> count
    spreadsheet_blank_count: int | None = None
    formula_empty_count: int | None = None


class CategorySummary(_Base):
    """Top-k categories with disclosure control (SPEC 7.7).

    ``value_key`` is a keyed digest, never the raw value; ``display`` is
    only populated under an explicit disclosure policy.
    """

    value_key: str
    display: str | None = None
    count: int
    is_falsy_variant: bool = False


class FormatPattern(_Base):
    """An observed value format with frequency (SPEC 7.6)."""

    pattern_id: str
    regex: str
    examples: list[str] = Field(default_factory=list)  # sanitized exemplars only
    occurrences: int = 0
    coverage: float = 0.0
    measurement: Measurement = Measurement.EXACT


class SensitivityFinding(_Base):
    label: SensitivityLabel
    confidence: Confidence
    evidence: str  # evidence type name, never a matched value
    detector: str
    recommended_action: ColumnAction = ColumnAction.MANUAL_REVIEW


class ColumnProfile(_Base):
    """Observed properties of one column (SPEC 5.4)."""

    column_id: str
    name: str
    ordinal: int
    source_type: str
    logical_type: LogicalType
    type_confidence: Confidence = Confidence.UNKNOWN
    nullable_declared: bool | None = None
    row_count_seen: int = 0
    distinct_count: int | None = None
    distinct_measurement: Measurement = Measurement.UNKNOWN
    distinct_method: str | None = None
    falsy: FalsyStats = Field(default_factory=FalsyStats)
    length: LengthSummary | None = None
    numeric: NumericSummary | None = None
    temporal: TemporalSummary | None = None
    top_categories: list[CategorySummary] = Field(default_factory=list)
    format_patterns: list[FormatPattern] = Field(default_factory=list)
    sensitivity: list[SensitivityFinding] = Field(default_factory=list)
    unique: bool | None = None
    unique_measurement: Measurement = Measurement.UNKNOWN
    constraints: list[str] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.UNKNOWN
    sampling_method: SamplingMethod = SamplingMethod.NONE
    sample_size: int = 0
    query_cost: int = 0
    generation_strategy: str | None = None
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------- Relationships


class Relationship(_Base):
    """Declared or hypothesized relationship between datasets/columns (SPEC 7.8)."""

    id: str = Field(default_factory=lambda: new_id("rel"))
    kind: str  # "foreign_key" | "key_candidate" | "dependency"
    from_dataset: str
    from_columns: list[str]
    to_dataset: str
    to_columns: list[str]
    declared: bool = False
    verified: bool = False
    confidence: Confidence = Confidence.UNKNOWN
    evidence_refs: list[str] = Field(default_factory=list)


# ------------------------------------------------------------- ProfileArtifact


class ProfileArtifact(_Base):
    """Versioned, shareable profile summary (SPEC 5.6)."""

    schema_version: int = SCHEMA_VERSION_PROFILE
    profile_id: str = Field(default_factory=lambda: new_id("prof"))
    created_at: datetime = Field(default_factory=utc_now)
    tool_version: str
    algorithm_version: str = PROFILE_ALGORITHM_VERSION
    source_id: str
    source_fingerprint: str
    schema_fingerprint: str
    dataset: DatasetRef
    dataset_schema: DatasetSchema
    columns: list[ColumnProfile] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    row_count: int | None = None
    row_count_measurement: Measurement = Measurement.UNKNOWN
    profile_config: dict[str, Any] = Field(default_factory=dict)
    budget: Budget = Field(default_factory=Budget)
    budget_usage: dict[str, float] = Field(default_factory=dict)
    sensitivity_findings: list[SensitivityFinding] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    incomplete_checks: list[str] = Field(default_factory=list)
    confidence_summary: dict[str, str] = Field(default_factory=dict)
    deterministic_settings: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float = 0.0
    status: str = "completed"

    @property
    def dataset_id(self) -> str:
        return self.dataset.qualified_name


# ------------------------------------------------------------ SanitizationPolicy


class ColumnPolicy(_Base):
    """Per-column handling (SPEC 5.7)."""

    action: ColumnAction
    generalize_format: str | None = None  # e.g. "year", "age_band_10", "day"
    mask_char: str = "*"
    mask_keep_prefix: int = 0
    mask_keep_suffix: int = 0


class BudgetPolicy(_Base):
    max_rows: int = 10_000
    max_bytes: int = 16_777_216
    max_runtime_seconds: float = 120.0
    max_queries: int = 100
    allow_full_scan: bool = False


class SourcePolicy(_Base):
    """Per-source access rules (SPEC 5.7, 13)."""

    access: str = "deny"  # "read" | "deny"
    datasets: list[str] = Field(default_factory=list)  # empty = none permitted
    columns: dict[str, ColumnPolicy] = Field(default_factory=dict)


class SanitizationPolicy(_Base):
    """Declarative policy controlling access and disclosure (SPEC 5.7)."""

    schema_version: int = SCHEMA_VERSION_POLICY
    version: str = "1"

    @field_validator("version", mode="before")
    @classmethod
    def _coerce_version(cls, v: object) -> str:
        return str(v)

    name: str
    owner: str | None = None
    approved_at: datetime | None = None
    description: str | None = None
    defaults: dict[str, Any] = Field(
        default_factory=lambda: {
            "source_access": "deny",
            "raw_row_output": "deny",
            "source_writes": "deny",
            "external_egress": "deny",
            "profile_disclosure": ProfileDisclosure.AGGREGATE_ONLY.value,
            "external_model_calls": False,
        }
    )
    budget: BudgetPolicy = Field(default_factory=BudgetPolicy)
    sources: dict[str, SourcePolicy] = Field(default_factory=dict)
    # Egress: allowed destinations for row-level output
    allowed_destinations: list[str] = Field(default_factory=list)
    allow_raw_samples: bool = False
    allow_aggregate_full_scan: bool = False
    rare_category_min_count: int = 5
    export_allowed: bool = False
    write_allowed: bool = False

    def dataset_permitted(self, source_name: str, qualified_dataset: str) -> bool:
        src = self.sources.get(source_name)
        if src is None or src.access != "read":
            return False
        if not src.datasets:
            return False
        return qualified_dataset in src.datasets or "*" in src.datasets


# ----------------------------------------------------------- GenerationPlan


class StrategyOverride(_Base):
    column: str
    strategy: str
    params: dict[str, Any] = Field(default_factory=dict)


class GenerationPlan(_Base):
    """Recipe for producing a synthetic dataset (SPEC 5.8)."""

    schema_version: int = SCHEMA_VERSION_PLAN
    plan_id: str = Field(default_factory=lambda: new_id("plan"))
    created_at: datetime = Field(default_factory=utc_now)
    profile_ids: list[str]
    dataset: DatasetRef
    rows: int = Field(ge=1)
    seed: int = 0
    locale: str = "en_US"
    strategy_overrides: list[StrategyOverride] = Field(default_factory=list)
    preserve_relationships: bool = True
    null_rate_tolerance: float = 0.05
    enforce_uniqueness: bool = True
    output_format: str = "jsonl"
    budget: Budget = Field(default_factory=Budget)
    tool_version: str = "0.1.0"
    allow_fallbacks: bool = False


class FidelityMetric(_Base):
    name: str
    logical_type: LogicalType | None = None
    observed: float | None = None
    generated: float | None = None
    distance: float | None = None
    tolerance: float | None = None
    passed: bool = True
    hard: bool = False  # hard constraint vs soft distribution mismatch


class GenerationReport(_Base):
    """Outcome of a generation + validation run (SPEC 5.8, 9.5)."""

    schema_version: int = SCHEMA_VERSION_REPORT
    report_id: str = Field(default_factory=lambda: new_id("rep"))
    created_at: datetime = Field(default_factory=utc_now)
    plan_id: str
    rows_requested: int
    rows_generated: int = 0
    seed: int = 0
    tool_version: str = "0.1.0"
    strategy_version: str = "1.0.0"
    metrics: list[FidelityMetric] = Field(default_factory=list)
    constraint_violations: list[str] = Field(default_factory=list)
    strategy_fallbacks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    preserved_properties: list[str] = Field(default_factory=list)
    approximated_properties: list[str] = Field(default_factory=list)
    runtime_seconds: float = 0.0
    status: str = "completed"
    output_path: str | None = None

    @property
    def hard_failures(self) -> list[FidelityMetric]:
        return [m for m in self.metrics if m.hard and not m.passed]

    @property
    def soft_failures(self) -> list[FidelityMetric]:
        return [m for m in self.metrics if not m.hard and not m.passed]


# ---------------------------------------------------------------- AuditEvent


class AuditEvent(_Base):
    """Security-relevant event (SPEC 5.9). Never contains raw rows/credentials."""

    id: str = Field(default_factory=lambda: new_id("aud"))
    occurred_at: datetime = Field(default_factory=utc_now)
    actor: str = "local"
    operation: OperationKind
    source_id: str | None = None
    dataset_id: str | None = None
    policy_version: str | None = None
    decision: str = "allow"
    rows_affected: int = 0
    duration_seconds: float = 0.0
    result_status: str = "ok"
    correlation_id: str = Field(default_factory=lambda: new_id("corr"))
    detail: dict[str, str] = Field(default_factory=dict)


# --------------------------------------------------------------------- Job


class JobRecord(_Base):
    """Persisted job metadata (SPEC 15, 19.5). No raw rows ever."""

    id: str = Field(default_factory=lambda: new_id("job"))
    kind: str
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    actor: str = "local"
    source_id: str | None = None
    dataset_id: str | None = None
    progress: float = 0.0
    message: str | None = None
    result_ref: str | None = None
    error_code: str | None = None
    correlation_id: str = Field(default_factory=lambda: new_id("corr"))
    cancellable: bool = True


class ConnectionTest(_Base):
    """Result of a connectivity check (SPEC 6)."""

    ok: bool
    message: str = "connection ok"
    latency_ms: float | None = None
    server_info: dict[str, str] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    error_code: str | None = None


class SizeEstimate(_Base):
    """Estimated dataset size used by the profiling planner (SPEC 6)."""

    row_estimate: int | None = None
    byte_estimate: int | None = None
    method: Measurement = Measurement.UNKNOWN
    note: str | None = None


class EphemeralRow(_Base):
    """A row observed during a profiling session; never persisted (SPEC 5.5).

    ``values`` maps column name -> value. ``cell_kinds`` preserves
    spreadsheet distinctions (blank vs empty vs formula-empty, SPEC 6.7)
    for connectors that expose them.
    """

    values: dict[str, Any]
    cell_kinds: dict[str, str] = Field(default_factory=dict)
    row_number: int | None = None
