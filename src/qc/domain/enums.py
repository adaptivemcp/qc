"""Core enumerations and shared value types for the QC domain.

Everything in this module is source-independent. Connector-specific
quirks must be normalized before reaching these types.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class LogicalType(StrEnum):
    """Source-independent logical type of a column or value (SPEC 7.6)."""

    STRING = "string"
    CATEGORICAL_STRING = "categorical_string"
    FREE_TEXT = "free_text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    TIME = "time"
    TIMESTAMP = "timestamp"
    TIMESTAMP_TZ = "timestamp_tz"
    UUID = "uuid"
    IDENTIFIER = "identifier"
    EMAIL = "email"
    PHONE = "phone"
    URL = "url"
    IP_ADDRESS = "ip_address"
    POSTAL_CODE = "postal_code"
    CURRENCY = "currency"
    COUNTRY_CODE = "country_code"
    JSON = "json"
    BINARY = "binary"
    UNSUPPORTED = "unsupported"


class DatasetKind(StrEnum):
    TABLE = "table"
    VIEW = "view"
    SHEET = "sheet"
    NAMED_RANGE = "named_range"
    QUERY_RESULT = "query_result"


class Confidence(StrEnum):
    """Calibrated evidence strength (SPEC 2, 7.9). Never a probability."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class EvidenceKind(StrEnum):
    """How an observation was obtained (SPEC 5.5)."""

    METADATA = "metadata"
    AGGREGATE = "aggregate"
    SAMPLE = "sample"
    DETECTOR = "detector"
    RELATIONSHIP = "relationship"
    ANOMALY = "anomaly"


class Measurement(StrEnum):
    """Whether a recorded number is exact, estimated, or unknown (SPEC 5.4)."""

    EXACT = "exact"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class SensitivityLabel(StrEnum):
    """Candidate sensitivity classifications (SPEC 8.3)."""

    EMAIL = "email"
    PHONE = "phone"
    NAME = "name"
    ADDRESS = "address"
    GOVERNMENT_ID = "government_id"
    CREDENTIAL = "credential"
    API_TOKEN = "api_token"
    DATE_OF_BIRTH = "date_of_birth"
    IP_ADDRESS = "ip_address"
    FREE_TEXT = "free_text"
    FINANCIAL = "financial"
    HEALTH = "health"
    LOCATION = "location"
    CUSTOM = "custom"


class ColumnAction(StrEnum):
    """Per-column handling action in a sanitization policy (SPEC 5.7)."""

    KEEP = "keep"
    MASK = "mask"
    REDACT = "redact"
    GENERALIZE = "generalize"
    TOKENIZE = "tokenize"
    SYNTHETIC = "synthetic"
    DROP = "drop"
    MANUAL_REVIEW = "manual_review"


class ProfileDisclosure(StrEnum):
    """How much of a profile may leave the trusted boundary (SPEC 5.7)."""

    NONE = "none"
    AGGREGATE_ONLY = "aggregate_only"
    FULL = "full"


class SamplingMethod(StrEnum):
    """Method used to select rows for sampling (SPEC 7.4)."""

    NONE = "none"
    FULL_SCAN = "full_scan"
    STABLE_KEY = "stable_key"
    RANDOM = "random"
    STRATIFIED = "stratified"
    RARE_VALUE = "rare_value"
    TIME_WINDOW = "time_window"
    PROVIDER = "provider"
    USER_PREDICATE = "user_predicate"
    RESERVOIR = "reservoir"
    FIXTURE = "fixture"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class OperationKind(StrEnum):
    """Audit-classified operations (SPEC 5.9, 12.2)."""

    SOURCE_TEST = "source_test"
    LIST_SOURCES = "list_sources"
    LIST_DATASETS = "list_datasets"
    GET_SCHEMA = "get_schema"
    PROFILE = "profile"
    GET_PROFILE = "get_profile"
    SANITIZE_PREVIEW = "sanitize_preview"
    SANITIZE_EXPORT = "sanitize_export"
    GENERATE = "generate"
    VALIDATE = "validate"
    EXPORT_DATABASE = "export_database"
    POLICY_VALIDATE = "policy_validate"
    CANCEL_JOB = "cancel_job"
    GET_JOB_STATUS = "get_job_status"
    GET_POLICY_SUMMARY = "get_policy_summary"


def utc_now() -> datetime:
    """Current UTC time with no tz ambiguity."""
    from datetime import UTC

    return datetime.now(UTC)


class QCBaseModel(BaseModel):
    """Base model: forbid unknown fields, stable serialization.

    Scalar types are validated in pydantic's default (lax) mode so that
    YAML/JSON string enums parse naturally; ``extra="forbid"`` rejects
    unknown keys at every boundary.
    """

    model_config = {"extra": "forbid", "use_enum_values": False}

    def json_safe(self) -> dict[str, object]:
        """Serialize to plain JSON-compatible data for artifacts."""
        return self.model_dump(mode="json")


class Budget(QCBaseModel):
    """Bounded-work limits applied to a single operation (SPEC 1.3, 7.3).

    None means 'not set'; zero is a real limit meaning 'nothing allowed'.
    """

    max_queries: int | None = Field(default=None, ge=0)
    max_rows: int | None = Field(default=None, ge=0)
    max_bytes: int | None = Field(default=None, ge=0)
    max_runtime_seconds: float | None = Field(default=None, ge=0)
    max_cost: float | None = Field(default=None, ge=0)

    def merge(self, override: Budget) -> Budget:
        """Return a budget where non-None values from ``override`` win."""
        return Budget(
            max_queries=override.max_queries if override.max_queries is not None else self.max_queries,
            max_rows=override.max_rows if override.max_rows is not None else self.max_rows,
            max_bytes=override.max_bytes if override.max_bytes is not None else self.max_bytes,
            max_runtime_seconds=(
                override.max_runtime_seconds
                if override.max_runtime_seconds is not None
                else self.max_runtime_seconds
            ),
            max_cost=override.max_cost if override.max_cost is not None else self.max_cost,
        )


class BudgetUsage(QCBaseModel):
    """Cumulative consumption against a :class:`Budget`."""

    queries: int = 0
    rows: int = 0
    bytes: int = 0
    runtime_seconds: float = 0.0
    cost: float = 0.0

    def add_query(self, rows: int = 0, byte_count: int = 0, runtime: float = 0.0, cost: float = 0.0) -> None:
        self.queries += 1
        self.rows += rows
        self.bytes += byte_count
        self.runtime_seconds += runtime
        self.cost += cost

    def exhausted(self, budget: Budget) -> str | None:
        """Return the name of the first exhausted dimension, or None."""
        if budget.max_queries is not None and self.queries > budget.max_queries:
            return "max_queries"
        if budget.max_rows is not None and self.rows > budget.max_rows:
            return "max_rows"
        if budget.max_bytes is not None and self.bytes > budget.max_bytes:
            return "max_bytes"
        if budget.max_runtime_seconds is not None and self.runtime_seconds > budget.max_runtime_seconds:
            return "max_runtime_seconds"
        if budget.max_cost is not None and self.cost > budget.max_cost:
            return "max_cost"
        return None

    def would_exceed(self, budget: Budget, *, queries: int = 0, rows: int = 0, byte_count: int = 0) -> bool:
        """Would one more query of this shape cross a limit?"""
        if budget.max_queries is not None and self.queries + queries > budget.max_queries:
            return True
        if budget.max_rows is not None and self.rows + rows > budget.max_rows:
            return True
        return bool(
            budget.max_bytes is not None and self.bytes + byte_count > budget.max_bytes
        )
