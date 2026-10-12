"""MCP tool schemas and output envelope (SPEC 12.2, 12.4).

Every tool: strict typed input, bounded output, policy enforced before
any source read, stable error codes, provenance in every response.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from qc import TOOL_VERSION

# Error codes (SPEC 12.4) re-exported for handlers
ENVELOPE_VERSION = 1


class Envelope(BaseModel):
    """Consistent MCP output envelope (SPEC 12.4)."""

    status: str = "completed"
    job_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False

    @classmethod
    def build(
        cls,
        data: dict[str, Any],
        *,
        status: str = "completed",
        job_id: str | None = None,
        warnings: list[str] | None = None,
        profile_id: str | None = None,
        policy_version: str | None = None,
        truncated: bool = False,
    ) -> Envelope:
        return cls(
            status=status,
            job_id=job_id,
            data=data,
            warnings=warnings or [],
            provenance={
                "profile_id": profile_id,
                "policy_version": policy_version,
                "tool_version": TOOL_VERSION,
                "envelope_version": ENVELOPE_VERSION,
            },
            truncated=truncated,
        )


# ------------------------------------------------------------------- inputs


class ListSourcesInput(BaseModel):
    """No inputs (SPEC 12.2 tool 1)."""


class ListDatasetsInput(BaseModel):
    source_id: str = Field(min_length=1, max_length=128)
    namespace: str | None = Field(default=None, max_length=128)
    search: str | None = Field(default=None, max_length=128)
    page_size: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=256)


class GetDatasetSchemaInput(BaseModel):
    source_id: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=512)


class ProfileDatasetInput(BaseModel):
    source_id: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=512)
    sample_size: int = Field(default=500, ge=1, le=10_000)
    sensitivity_scan: bool = True
    # server-defined maxima; callers cannot exceed them (SPEC 12.2 tool 4)
    max_queries: int | None = Field(default=None, ge=0, le=1000)


class GetProfileInput(BaseModel):
    profile_id: str = Field(min_length=1, max_length=128)


class PreviewSanitizedInput(BaseModel):
    source_id: str | None = Field(default=None, max_length=128)
    dataset_id: str | None = Field(default=None, max_length=512)
    profile_id: str | None = Field(default=None, max_length=128)
    policy_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=10, ge=1, le=100)
    columns: list[str] | None = Field(default=None, max_length=500)


class GenerateSyntheticInput(BaseModel):
    profile_id: str = Field(min_length=1, max_length=128)
    rows: int = Field(ge=1, le=10_000)
    seed: int | None = Field(default=None)
    output_format: str = Field(default="summary", pattern="^(summary|inline)$")
    inline_limit: int = Field(default=20, ge=1, le=100)


class ValidateSyntheticInput(BaseModel):
    artifact_ref: str = Field(min_length=1, max_length=512)
    profile_id: str = Field(min_length=1, max_length=128)


class JobIdInput(BaseModel):
    job_id: str = Field(min_length=1, max_length=128)


class GetPolicySummaryInput(BaseModel):
    policy_id: str = Field(min_length=1, max_length=128)


# --------------------------------------------------------------- tool specs


class ToolSpec(BaseModel):
    name: str
    description: str
    input_model: type[BaseModel]
    # MCP tools excluded from v1 (SPEC 12.3): arbitrary SQL/Python,
    # policy mutation, source creation, writes, public URL export,
    # unrestricted raw rows. Nothing below offers any of those.


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="list_sources",
        description=(
            "List configured data sources visible to this client. "
            "Never returns credentials or connection strings."
        ),
        input_model=ListSourcesInput,
    ),
    ToolSpec(
        name="list_datasets",
        description=(
            "List datasets for a source, filtered by policy allowlists. "
            "Returns only permitted dataset identifiers."
        ),
        input_model=ListDatasetsInput,
    ),
    ToolSpec(
        name="get_dataset_schema",
        description=(
            "Schema metadata and constraints for one dataset, as allowed "
            "by policy. Sensitive column names may be redacted."
        ),
        input_model=GetDatasetSchemaInput,
    ),
    ToolSpec(
        name="profile_dataset",
        description=(
            "Run bounded adaptive profiling. Returns a profile summary, "
            "coverage, warnings, and a profile ID. Server budgets and "
            "privacy policy cannot be overridden by arguments."
        ),
        input_model=ProfileDatasetInput,
    ),
    ToolSpec(
        name="get_profile",
        description="Return a policy-filtered profile artifact summary.",
        input_model=GetProfileInput,
    ),
    ToolSpec(
        name="preview_sanitized_data",
        description=(
            "Return policy-approved transformed rows. Raw rows are denied "
            "by default; unreviewed columns are redacted."
        ),
        input_model=PreviewSanitizedInput,
    ),
    ToolSpec(
        name="generate_synthetic_data",
        description=(
            "Generate synthetic rows from a profile. Default response is a "
            "summary + artifact reference; inline records require an "
            "explicit small limit and approved fields."
        ),
        input_model=GenerateSyntheticInput,
    ),
    ToolSpec(
        name="validate_synthetic_data",
        description=(
            "Validate a generated artifact against its profile. Returns "
            "metrics and violations without source values."
        ),
        input_model=ValidateSyntheticInput,
    ),
    ToolSpec(
        name="get_job_status",
        description="Status, progress, safe diagnostics, cancellation availability.",
        input_model=JobIdInput,
    ),
    ToolSpec(
        name="cancel_job",
        description="Cancel a job owned by the caller; closes resources.",
        input_model=JobIdInput,
    ),
    ToolSpec(
        name="get_policy_summary",
        description="Effective permissions and restrictions for a policy, no secrets.",
        input_model=GetPolicySummaryInput,
    ),
]

#: v1 exclusions, surfaced in error messages when clients probe (SPEC 12.3)
EXCLUDED_TOOLS: dict[str, str] = {
    "execute_sql": "arbitrary SQL execution is not exposed in v1",
    "run_python": "arbitrary Python execution is not exposed in v1",
    "update_policy": "policy mutation via MCP is not exposed in v1",
    "create_source": "source creation via MCP is not exposed in v1",
    "write_rows": "source writes are not exposed in v1",
    "export_to_url": "direct export to public URLs is not exposed in v1",
    "get_raw_rows": "unrestricted raw rows are not exposed in v1",
}
