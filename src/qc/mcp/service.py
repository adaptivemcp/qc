"""MCP server: stdio and Streamable HTTP over shared application services
(SECP section 12). Policy is enforced server-side before any source read;
inputs are strictly validated; outputs pass the envelope + row/byte caps.

Security posture (SPEC 12.6):
- authorization on every call
- budgets from server config only (tool args can only narrow them)
- source content treated as untrusted data, never instructions
- stable error codes; no secrets, raw values, or unapproved schema in errors
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from qc import TOOL_VERSION
from qc.application.runtime import Runtime
from qc.domain.errors import (
    AuthError,
    BudgetExceededError,
    CancelledError,
    ErrorCode,
    NotFoundError,
    PolicyDeniedError,
    QCError,
    SourceError,
    UnsupportedError,
    ValidationError,
)
from qc.mcp.schemas import (
    EXCLUDED_TOOLS,
    Envelope,
    GenerateSyntheticInput,
    GetDatasetSchemaInput,
    GetPolicySummaryInput,
    GetProfileInput,
    JobIdInput,
    ListDatasetsInput,
    ListSourcesInput,
    PreviewSanitizedInput,
    ProfileDatasetInput,
    ValidateSyntheticInput,
)
from qc.observability.logging import get_logger
from qc.security.policy import Ceiling, DecisionContext, Operation

log = get_logger("mcp")


def _error_code(exc: Exception) -> ErrorCode:
    if isinstance(exc, QCError):
        return exc.code
    if isinstance(exc, PydanticValidationError):
        return ErrorCode.VALIDATION_FAILED
    return ErrorCode.INTERNAL


def envelope_error(exc: Exception, *, job_id: str | None = None) -> dict[str, Any]:
    """Render a safe error envelope (SPEC 12.4). Never leaks internals."""
    code = _error_code(exc)
    if isinstance(exc, QCError):
        message = exc.message
        detail = exc.detail
    elif isinstance(exc, PydanticValidationError):
        # pydantic errors can echo input values; keep only field paths
        message = "invalid tool arguments"
        detail = {"fields": ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors()[:5])}
    else:
        # unexpected: generic message only (qc.observability logs the rest)
        message = "internal error; see server logs"
        detail = {}
    return {
        "status": "error",
        "job_id": job_id,
        "error": code.value,
        "message": message,
        "detail": detail,
        "data": {},
        "warnings": [],
        "provenance": {"tool_version": TOOL_VERSION},
        "truncated": False,
    }


class QCService:
    """Tool implementations shared by stdio and HTTP transports.

    All methods return an envelope-shaped dict. Policy checks happen
    inside each method *before* connector calls.
    """

    def __init__(self, runtime: Runtime, *, transport: str = "mcp-stdio") -> None:
        self.rt = runtime
        self.transport = transport
        self.ceiling: Ceiling = runtime.ceiling

    def _engine(self, policy_id: str | None = None):  # type: ignore[no-untyped-def]
        return self.rt.engine(policy_id)

    def _audit(self, operation: Any, **kwargs: Any) -> None:
        self.rt.audit.record(operation, **kwargs)

    # ------------------------------------------------------------- tools

    def list_sources(self, raw: dict[str, Any]) -> dict[str, Any]:
        ListSourcesInput.model_validate(raw)
        engine = self._engine()
        # Listing is itself the filtered result: only sources with read
        # access in policy appear. No dataset context to check yet.
        allowed = [
            s
            for s in self.rt.sources.list()
            if s.name in engine.policy.sources and engine.policy.sources[s.name].access == "read"
        ]
        data = {
            "sources": [
                {
                    "source_id": s.id,
                    "name": s.name,
                    "connector": s.connector,
                    "read_only": s.read_only,
                }
                for s in allowed
            ]
        }
        return Envelope.build(data, policy_version=engine.policy.version).model_dump()

    def list_datasets(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = ListDatasetsInput.model_validate(raw)
        engine = self._engine()
        cfg, name = self._resolve_source(args.source_id)
        engine.require(
            Operation.SOURCE_ACCESS,
            DecisionContext(source=name, actor="mcp", transport=self.transport),
        )

        import asyncio

        async def _run() -> Any:
            connector = self.rt.connector_for(cfg)
            try:
                return await connector.list_datasets(cfg)
            finally:
                await connector.close()

        datasets = asyncio.run(_run())
        permitted: list[dict[str, Any]] = []
        for d in datasets:
            if args.namespace and d.namespace != args.namespace:
                continue
            if args.search and args.search not in d.qualified_name:
                continue
            if not engine.policy.dataset_permitted(name, d.qualified_name):
                continue
            permitted.append({"dataset_id": d.qualified_name, "kind": d.kind.value})
        # bounded pagination
        start = int(args.cursor or "0") if (args.cursor or "").isdigit() else 0
        page = permitted[start : start + args.page_size]
        nxt = start + args.page_size
        return Envelope.build(
            {
                "datasets": page,
                "next_cursor": str(nxt) if nxt < len(permitted) else None,
                "total": len(permitted),
            },
            policy_version=engine.policy.version,
        ).model_dump()

    def get_dataset_schema(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = GetDatasetSchemaInput.model_validate(raw)
        engine = self._engine()
        cfg, name = self._resolve_source(args.source_id)
        ctx = DecisionContext(
            source=name, dataset=args.dataset_id, actor="mcp", transport=self.transport
        )
        engine.require(Operation.DATASET_READ, ctx)

        import asyncio

        from qc.cli.app import _parse_dataset_ref  # shared ref parsing

        ref = _parse_dataset_ref(cfg, args.dataset_id)

        async def _run() -> Any:
            connector = self.rt.connector_for(cfg)
            try:
                return await connector.get_schema(cfg, ref)
            finally:
                await connector.close()

        schema = asyncio.run(_run())
        disclosure = engine.policy.defaults.get("profile_disclosure", "aggregate_only")
        cols = []
        for c in schema.columns:
            action = engine.column_action(name, c.name)
            col_out: dict[str, Any] = {
                "name": c.name,
                "ordinal": c.ordinal,
                "source_type": c.source_type,
                "nullable": c.nullable_declared,
            }
            # redact column names only under the strictest disclosure
            if disclosure == "none":
                col_out["name"] = f"col_{c.ordinal}"
            col_out["suggested_action"] = action.action.value
            cols.append(col_out)
        return Envelope.build(
            {
                "dataset": schema.dataset.qualified_name,
                "columns": cols,
                "primary_key": schema.primary_key,
                "row_estimate": schema.row_estimate,
                "fingerprint": schema.source_fingerprint,
            },
            warnings=(
                ["column names may be redacted by profile_disclosure policy"]
                if disclosure == "none"
                else []
            ),
            policy_version=engine.policy.version,
        ).model_dump()

    def profile_dataset(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = ProfileDatasetInput.model_validate(raw)
        engine = self._engine()
        cfg, name = self._resolve_source(args.source_id)
        ctx = DecisionContext(
            source=name, dataset=args.dataset_id, actor="mcp", transport=self.transport
        )
        engine.require(Operation.PROFILE_RUN, ctx)

        from qc.application.job_runner import JobRunner, run_job
        from qc.artifacts.profiles import save_profile
        from qc.cli.app import _parse_dataset_ref
        from qc.domain.enums import Budget, OperationKind
        from qc.profiling.engine import Profiler, ProfileSettings

        # server maxima; args can only narrow (SPEC 12.2 tool 4)
        max_q = args.max_queries
        if max_q is not None:
            max_q = min(max_q, self.ceiling.max_queries)
        ref = _parse_dataset_ref(cfg, args.dataset_id)
        runner = JobRunner(
            self.rt,
            kind="profile",
            actor="mcp",
            source_id=cfg.id,
            dataset_id=args.dataset_id,
        )
        settings = ProfileSettings(
            sample_size=min(args.sample_size, self.ceiling.max_rows),
            budget=Budget(
                max_queries=max_q,
                max_rows=self.ceiling.max_rows,
                max_bytes=self.ceiling.max_bytes,
                max_runtime_seconds=self.ceiling.max_runtime_seconds,
            ),
            sensitivity_scan=args.sensitivity_scan,
            allow_full_scan=False,
            cancel=runner.token,
        )

        async def _run() -> Any:
            connector = self.rt.connector_for(cfg)
            try:
                profiler = Profiler(connector, engine, ceiling=self.ceiling)
                return await profiler.profile(
                    cfg, ref, settings, actor="mcp", transport=self.transport
                )
            finally:
                await connector.close()

        try:
            outcome = asyncio_run(run_job(runner, _run, operation_kind=OperationKind.PROFILE))
        except Exception:
            # job already recorded as failed/cancelled by run_job
            raise
        result = outcome.result
        artifact = result.artifact
        path = save_profile(self.rt.paths.profiles, artifact)
        self.rt.state.index_profile(artifact, path)
        runner.complete(result_ref=str(path), message=f"profile {artifact.profile_id}")
        summary = {
            "profile_id": artifact.profile_id,
            "row_count": artifact.row_count,
            "row_count_measurement": artifact.row_count_measurement.value,
            "columns": len(artifact.columns),
            "coverage": {
                "queries_used": result.budget_usage.queries,
                "rows_seen": result.budget_usage.rows,
                "incomplete_checks": artifact.incomplete_checks,
            },
            "confidence_summary": artifact.confidence_summary,
            "sensitivity_findings": sorted({f.label.value for f in artifact.sensitivity_findings}),
            "column_summary": [
                {
                    "name": c.name,
                    "logical_type": c.logical_type.value,
                    "confidence": c.confidence.value,
                }
                for c in artifact.columns
            ],
        }
        return Envelope.build(
            summary,
            warnings=artifact.warnings,
            job_id=runner.id,
            profile_id=artifact.profile_id,
            policy_version=engine.policy.version,
        ).model_dump()

    def get_profile(self, raw: dict[str, Any]) -> dict[str, Any]:
        from qc.artifacts.profiles import find_profile, load_profile

        args = GetProfileInput.model_validate(raw)
        engine = self._engine()
        engine.require(
            Operation.PROFILE_DISCLOSE,
            DecisionContext(actor="mcp", transport=self.transport),
        )
        path = self.rt.state.find_profile_index(args.profile_id) or find_profile(
            self.rt.paths.profiles, args.profile_id
        )
        artifact = load_profile(path)
        disclosure = engine.policy.defaults.get("profile_disclosure", "aggregate_only")

        columns: list[dict[str, Any]] = []
        for c in artifact.columns:
            entry: dict[str, Any] = {
                "name": c.name,
                "logical_type": c.logical_type.value,
                "confidence": c.confidence.value,
                "null_count": c.falsy.null_count,
                "distinct_count": c.distinct_count,
                "distinct_measurement": c.distinct_measurement.value,
            }
            if disclosure == "full":
                entry["top_categories"] = [
                    {"value": cat.display, "count": cat.count}
                    for cat in c.top_categories
                    if cat.display is not None
                ]
                entry["sensitivity"] = [
                    {"label": f.label.value, "confidence": f.confidence.value}
                    for f in c.sensitivity
                ]
            columns.append(entry)
        return Envelope.build(
            {
                "profile_id": artifact.profile_id,
                "dataset": artifact.dataset.qualified_name,
                "created_at": artifact.created_at.isoformat(),
                "row_count": artifact.row_count,
                "row_count_measurement": artifact.row_count_measurement.value,
                "algorithm_version": artifact.algorithm_version,
                "columns": columns,
                "relationships": len(artifact.relationships),
                "disclosure_level": disclosure,
                "incomplete_checks": artifact.incomplete_checks,
            },
            warnings=artifact.warnings,
            profile_id=artifact.profile_id,
            policy_version=engine.policy.version,
        ).model_dump()

    def preview_sanitized_data(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = PreviewSanitizedInput.model_validate(raw)
        engine = self._engine(args.policy_id)
        if not args.source_id or not args.dataset_id:
            raise ValidationError(
                "source_id and dataset_id are required when profile_id is omitted"
            )
        if args.limit > self.ceiling.max_inline_mcp_rows:
            raise ValidationError(
                f"limit {args.limit} exceeds server max "
                f"{self.ceiling.max_inline_mcp_rows} inline rows"
            )
        from qc.application.sanitize import preview_rows

        result = asyncio_run(
            preview_rows(
                self.rt,
                source=self._source_name(args.source_id),
                dataset=args.dataset_id,
                policy_name=args.policy_id,
                limit=args.limit,
                columns=args.columns,
            )
        )
        return Envelope.build(
            {
                "rows": result["rows"],
                "row_count": result["row_count"],
                "blocked_columns": result["blocked"],
                "dropped_columns": result["dropped"],
                "raw_output": result["raw_output"],
            },
            warnings=result["warnings"],
            policy_version=engine.policy.version,
            truncated=result["row_count"] >= args.limit,
        ).model_dump()

    def generate_synthetic_data(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = GenerateSyntheticInput.model_validate(raw)
        engine = self._engine()
        # create_plan enforces DATASET_READ with the resolved source name
        from qc.application.generation import create_plan, run_generation
        from qc.application.job_runner import JobRunner

        plan = create_plan(
            self.rt,
            profile_ref=args.profile_id,
            rows=min(args.rows, self.ceiling.max_rows),
            seed=args.seed if args.seed is not None else 0,
        )
        plan_file = self.rt.paths.reports / f"{plan.plan_id}.plan.json"
        # generation honors the same cancellation token (SPEC 12.2 tool 10)
        runner = JobRunner(
            self.rt,
            kind="generate",
            actor="mcp",
            source_id=plan.dataset.source_id,
            dataset_id=plan.dataset.qualified_name,
        )
        runner.start("generating")
        try:
            result = asyncio_run(
                run_generation(
                    self.rt,
                    plan_file=plan_file,
                    output=self.rt.paths.output / f"mcp_{plan.plan_id}.jsonl",
                    cancel=runner.token,
                )
            )
        except BaseException as exc:
            runner.fail(exc)
            raise
        rep = result["report"]
        runner.complete(result_ref=result["output"], message=f"generated {rep.rows_generated} rows")
        data: dict[str, Any] = {
            "plan_id": plan.plan_id,
            "artifact_ref": result["output"],
            "report_ref": result["report_path"],
            "rows_generated": rep.rows_generated,
            "seed": rep.seed,
            "hard_failures": len(rep.hard_failures),
            "soft_deviations": len(rep.soft_failures),
        }
        if args.output_format == "inline":
            limit = min(args.inline_limit, self.ceiling.max_inline_mcp_rows)
            rows = [
                json.loads(line)
                for line in Path(result["output"]).read_text(encoding="utf-8").splitlines()[:limit]
            ]
            data["rows"] = rows
            data["truncated_rows"] = rep.rows_generated > limit
        return Envelope.build(
            data,
            warnings=rep.warnings,
            job_id=runner.id,
            profile_id=args.profile_id,
            policy_version=engine.policy.version,
        ).model_dump()

    def validate_synthetic_data(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = ValidateSyntheticInput.model_validate(raw)
        engine = self._engine()
        # local file vs local profile: gate on profile disclosure, not
        # dataset read (no source data is accessed by this tool)
        engine.require(
            Operation.PROFILE_DISCLOSE,
            DecisionContext(actor="mcp", transport=self.transport),
        )
        from qc.application.generation import validate_output

        ref = args.artifact_ref
        # path traversal guard: artifacts must live under project output/
        target = (self.rt.paths.output / Path(ref).name).resolve()
        if not str(target).startswith(str(self.rt.paths.output.resolve())):
            raise ValidationError("artifact_ref escapes the output directory")
        report = asyncio_run(validate_output(self.rt, output=target, profile_ref=args.profile_id))
        return Envelope.build(
            {
                "metrics": [m.json_safe() for m in report.metrics],
                "hard_failures": [m.name for m in report.hard_failures],
                "soft_deviations": [m.name for m in report.soft_failures],
                "rows_validated": report.rows_generated,
            },
            warnings=["validation metrics contain no source values"],
            profile_id=args.profile_id,
            policy_version=engine.policy.version,
        ).model_dump()

    def get_job_status(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = JobIdInput.model_validate(raw)
        job = self.rt.state.get_job(args.job_id)
        if job is None:
            raise NotFoundError(f"job not found: {args.job_id}")
        return Envelope.build(
            {
                "job_id": job.id,
                "kind": job.kind,
                "status": job.status.value,
                "progress": job.progress,
                "message": job.message,
                "cancellable": job.cancellable,
                "created_at": job.created_at.isoformat(),
                "updated_at": job.updated_at.isoformat(),
            },
            job_id=job.id,
        ).model_dump()

    def cancel_job(self, raw: dict[str, Any]) -> dict[str, Any]:
        from qc.application.cancellation import REGISTRY
        from qc.domain.enums import JobStatus, OperationKind

        args = JobIdInput.model_validate(raw)
        job = self.rt.state.get_job(args.job_id)
        if job is None:
            raise NotFoundError(f"job not found: {args.job_id}")
        if not job.cancellable:
            raise ValidationError("job is not cancellable")

        # signal a running job first (cooperative); then reflect the state.
        signalled = REGISTRY.cancel(job.id, "cancelled by caller")
        running = job.status == JobStatus.RUNNING
        if signalled or running:
            self.rt.state.set_job_status(
                job.id, JobStatus.CANCELLED, message="cancellation requested"
            )
            status = "cancelling" if signalled else "cancelled"
        else:
            # terminal job: no-op, report its actual status
            status = job.status.value
        self.rt.audit.record(
            OperationKind.CANCEL_JOB,
            actor="mcp",
            decision="allow",
            result_status="ok",
            detail={"job_id": job.id, "signalled": str(signalled)},
        )
        return Envelope.build(
            {"job_id": job.id, "status": status, "in_flight": signalled}, job_id=job.id
        ).model_dump()

    def get_policy_summary(self, raw: dict[str, Any]) -> dict[str, Any]:
        args = GetPolicySummaryInput.model_validate(raw)
        engine = self._engine(args.policy_id)
        policy = engine.policy
        return Envelope.build(
            {
                "policy_id": policy.name,
                "version": policy.version,
                "defaults": policy.defaults,
                "budget": policy.budget.model_dump(),
                "sources": {
                    name: {
                        "access": src.access,
                        "dataset_count": len(src.datasets),
                        "columns_with_actions": len(src.columns),
                    }
                    for name, src in policy.sources.items()
                },
                "export_allowed": policy.export_allowed,
                "write_allowed": policy.write_allowed and self.ceiling.source_writes,
                "allow_raw_samples": policy.allow_raw_samples,
                "server_ceiling": {
                    "max_rows": self.ceiling.max_rows,
                    "max_inline_mcp_rows": self.ceiling.max_inline_mcp_rows,
                    "raw_mcp_rows": self.ceiling.raw_mcp_rows,
                    "source_writes": self.ceiling.source_writes,
                    "external_model_calls": self.ceiling.external_model_calls,
                },
            },
            policy_version=policy.version,
        ).model_dump()

    # ------------------------------------------------------------- helpers

    def _source_name(self, source_ref: str) -> str:
        """Accept opaque id or name; return the policy key (name)."""
        cfg = self.rt.sources.get_by_id(source_ref)
        if cfg is not None:
            return cfg.name
        try:
            self.rt.source(source_ref)
            return source_ref
        except NotFoundError:
            raise NotFoundError(f"source not found: {source_ref}") from None

    def _resolve_source(self, source_ref: str):  # type: ignore[no-untyped-def]
        name = self._source_name(source_ref)
        return self.rt.source(name), name


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


# ----------------------------------------------------------- dispatch table


def build_dispatch(service: QCService) -> dict[str, Any]:
    """name -> validated handler(raw dict) -> envelope dict."""
    return {
        "list_sources": service.list_sources,
        "list_datasets": service.list_datasets,
        "get_dataset_schema": service.get_dataset_schema,
        "profile_dataset": service.profile_dataset,
        "get_profile": service.get_profile,
        "preview_sanitized_data": service.preview_sanitized_data,
        "generate_synthetic_data": service.generate_synthetic_data,
        "validate_synthetic_data": service.validate_synthetic_data,
        "get_job_status": service.get_job_status,
        "cancel_job": service.cancel_job,
        "get_policy_summary": service.get_policy_summary,
    }


def call_tool(dispatch: dict[str, Any], name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Single entry point: excluded tools, unknown tools, safe errors."""
    if name in EXCLUDED_TOOLS:
        return envelope_error(
            UnsupportedError(f"tool '{name}' is not available: {EXCLUDED_TOOLS[name]}")
        )
    handler = dispatch.get(name)
    if handler is None:
        return envelope_error(NotFoundError(f"unknown tool: {name}"))
    try:
        result: dict[str, Any] = handler(raw)
        return result
    except (
        PolicyDeniedError,
        BudgetExceededError,
        NotFoundError,
        ValidationError,
        UnsupportedError,
        SourceError,
        AuthError,
        CancelledError,
    ) as exc:
        log.warning(
            "tool denied",
            fields={"error_code": exc.code.value, "operation_id": name},
        )
        return envelope_error(exc)
    except PydanticValidationError as exc:
        return envelope_error(exc)
    except Exception as exc:
        from qc.security.redaction import safe_error_message

        log.error(
            "tool failed",
            fields={
                "error_code": ErrorCode.INTERNAL.value,
                "operation_id": name,
                "detail": safe_error_message(exc),
            },
        )
        return envelope_error(exc)
