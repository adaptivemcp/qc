"""Audit trail (SPEC 5.9, 15).

Events are appended to a JSONL file with restrictive permissions. Events
never contain credentials, raw rows, or unredacted SQL parameters.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from qc.domain.enums import OperationKind
from qc.domain.models import AuditEvent
from qc.observability.logging import get_logger
from qc.security.redaction import redact_mapping

log = get_logger("audit")


class AuditLog:
    """Append-only JSONL audit sink."""

    def __init__(self, path: Path | str | None) -> None:
        self.path = Path(path) if path else None

    def record(
        self,
        operation: OperationKind,
        *,
        actor: str = "local",
        source_id: str | None = None,
        dataset_id: str | None = None,
        policy_version: str | None = None,
        decision: str = "allow",
        rows_affected: int = 0,
        duration_seconds: float = 0.0,
        result_status: str = "ok",
        detail: dict[str, str] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            operation=operation,
            actor=actor,
            source_id=source_id,
            dataset_id=dataset_id,
            policy_version=policy_version,
            decision=decision,
            rows_affected=rows_affected,
            duration_seconds=duration_seconds,
            result_status=result_status,
            detail=redact_mapping(detail or {}),
        )
        log.info(
            "audit",
            fields={
                "operation_id": event.correlation_id,
                "source_id": source_id,
                "dataset_id": dataset_id,
                "policy_version": policy_version,
                "error_code": None if decision == "allow" else "POLICY_DENIED",
                "actor": actor,
                "event": {
                    "operation": operation.value,
                    "decision": decision,
                    "status": result_status,
                    "rows": rows_affected,
                },
            },
        )
        self._append(event)
        return event

    def _append(self, event: AuditEvent) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(event.json_safe(), ensure_ascii=False)
            fd = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            try:
                os.write(fd, (payload + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        except OSError as exc:
            # Auditing must not crash the operation, but the failure is loud.
            log.warning(
                "audit write failed",
                fields={"error_code": "AUDIT_WRITE_FAILED", "detail": str(exc)},
            )

    def read_all(self) -> list[AuditEvent]:
        if self.path is None or not self.path.is_file():
            return []
        events: list[AuditEvent] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(AuditEvent.model_validate_json(line))
            except Exception:
                continue
        return events
