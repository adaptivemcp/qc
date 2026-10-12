"""Job lifecycle helper (SPEC 5.9, 12.2, 15).

Wraps a synchronous/async operation in a persisted :class:`JobRecord`
with a registered cancellation token, so the CLI and MCP share identical
job semantics: status transitions, progress, error codes, and
cooperative cancellation that closes resources.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from qc.application.cancellation import REGISTRY
from qc.domain.cancellation import CancellationToken
from qc.domain.enums import JobStatus, OperationKind, utc_now
from qc.domain.models import JobRecord
from qc.observability.logging import get_logger
from qc.security.redaction import safe_error_message

if TYPE_CHECKING:
    from qc.application.runtime import Runtime
    from qc.security.audit import AuditLog

log = get_logger("jobs")


@dataclass
class JobOutcome:
    """Result of a completed job plus its persisted record."""

    job: JobRecord
    result: Any = None
    status: JobStatus = JobStatus.COMPLETED
    error_code: str | None = None
    warnings: list[str] = field(default_factory=list)


class JobRunner:
    """Creates, tracks, and cancels a single job."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        kind: str,
        actor: str = "local",
        source_id: str | None = None,
        dataset_id: str | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.rt = runtime
        self.audit = audit or runtime.audit
        self.job = JobRecord(kind=kind, actor=actor, source_id=source_id, dataset_id=dataset_id)
        self.token: CancellationToken = REGISTRY.register(self.job.id)

    @property
    def id(self) -> str:
        return self.job.id

    def start(self, message: str = "running") -> None:
        self.job.status = JobStatus.RUNNING
        self.job.updated_at = utc_now()
        self.job.message = message
        self.job.progress = 0.0
        self.rt.state.upsert_job(self.job)

    def progress(self, value: float, message: str | None = None) -> None:
        self.job.progress = max(0.0, min(1.0, value))
        self.job.updated_at = utc_now()
        if message is not None:
            self.job.message = message
        self.rt.state.upsert_job(self.job)

    def complete(self, result_ref: str | None = None, message: str | None = None) -> None:
        self.job.status = JobStatus.COMPLETED
        self.job.progress = 1.0
        self.job.updated_at = utc_now()
        self.job.message = message or "completed"
        if result_ref is not None:
            self.job.result_ref = result_ref
        self._persist_and_release()

    def fail(self, exc: BaseException) -> str:
        from qc.domain.errors import CancelledError

        self.job.updated_at = utc_now()
        if isinstance(exc, CancelledError):
            self.job.status = JobStatus.CANCELLED
            self.job.message = safe_error_message(exc)[:200] or "cancelled"
            code = "CANCELLED"
        else:
            self.job.status = JobStatus.FAILED
            self.job.message = safe_error_message(exc)[:200] or "failed"
            code = getattr(getattr(exc, "code", None), "value", "INTERNAL")
        self.job.error_code = code
        self._persist_and_release()
        return code

    def cancel_from_registry(self, reason: str | None = None) -> bool:
        """Signal cancellation; returns False if the job is not running.

        Does not require the job to be persisted: a token is registered
        the moment the runner is constructed (SPEC 12.6).
        """
        return REGISTRY.cancel(self.job.id, reason)

    def _persist_and_release(self) -> None:
        self.rt.state.upsert_job(self.job)
        REGISTRY.unregister(self.job.id)
        log.info(
            "job finished",
            fields={
                "operation_id": self.job.correlation_id,
                "job_id": self.job.id,
                "source_id": self.job.source_id,
                "dataset_id": self.job.dataset_id,
                "status": self.job.status.value,
                "error_code": self.job.error_code,
            },
        )


async def run_job[T](
    runner: JobRunner,
    operation: Callable[[], Awaitable[T]],
    *,
    operation_kind: OperationKind | None = None,
    start_message: str = "running",
) -> JobOutcome:
    """Run ``operation`` under a job, recording status + audit."""
    runner.start(start_message)
    if operation_kind is not None:
        runner.audit.record(
            operation_kind,
            actor=runner.job.actor,
            source_id=runner.job.source_id,
            dataset_id=runner.job.dataset_id,
            decision="allow",
            result_status="started",
            detail={"job_id": runner.job.id},
        )
    try:
        result = await operation()
    except BaseException as exc:
        code = runner.fail(exc)
        if operation_kind is not None:
            runner.audit.record(
                operation_kind,
                actor=runner.job.actor,
                source_id=runner.job.source_id,
                dataset_id=runner.job.dataset_id,
                decision="deny" if code != "CANCELLED" else "allow",
                result_status=runner.job.status.value,
                detail={"job_id": runner.job.id, "error_code": code},
            )
        raise
    runner.complete()
    return JobOutcome(job=runner.job, result=result)


__all__ = ["JobOutcome", "JobRunner", "run_job"]
