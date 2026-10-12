"""Database destination export (SPEC 10.3).

Separate source/destination configs; approved destinations only;
create-new-table by default; append/replace require explicit mode.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qc.domain.enums import OperationKind
from qc.domain.errors import PolicyDeniedError, ValidationError, WriteError
from qc.security.policy import DecisionContext, Operation

if TYPE_CHECKING:
    from qc.application.runtime import Runtime


async def export_to_database(
    rt: Runtime,
    *,
    output: Path,
    destination: str,
    table: str,
    policy_name: str | None = None,
    mode: str = "create",
) -> dict[str, Any]:
    """Write a JSONL/CSV file to an explicitly approved destination."""
    engine = rt.engine(policy_name)
    policy = engine.policy
    ctx = DecisionContext(source=destination, dataset=table)
    engine.require(Operation.WRITE, ctx)
    engine.require(Operation.EXPORT, ctx)

    if not policy.write_allowed:
        raise PolicyDeniedError("policy does not allow writes (write_allowed=false)")
    if mode not in ("create", "append", "replace"):
        raise ValidationError(f"invalid mode '{mode}'; use create|append|replace")
    if not table.isidentifier() and not all(p.isidentifier() for p in table.split(".")):
        raise ValidationError(f"invalid table name: {table}")

    dest_cfg = rt.source(destination)
    rows = _read_rows(output)
    if not rows:
        raise ValidationError("output file contains no rows")

    connector = rt.connector_for(dest_cfg)
    try:
        if not hasattr(connector, "write_rows"):
            raise WriteError(
                f"connector '{destination}' does not support writes",
                detail={"destination": destination},
            )
        columns = list(rows[0].keys())
        written = await connector.write_rows(dest_cfg, table, columns, rows, mode=mode)
    finally:
        await connector.close()

    rt.audit.record(
        OperationKind.EXPORT_DATABASE,
        source_id=dest_cfg.id,
        dataset_id=table,
        policy_version=policy.version,
        decision="allow",
        rows_affected=written,
        result_status="ok",
        detail={"mode": mode},
    )
    return {
        "destination": destination,
        "table": table,
        "rows": written,
        "mode": mode,
        "columns": len(rows[0]),
    }


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise WriteError(f"output file not found: {path.name}")
    rows: list[dict[str, Any]] = []
    if path.suffix == ".csv":
        import csv

        with path.open(encoding="utf-8", newline="") as fh:
            for raw in csv.DictReader(fh):
                rows.append({k: _coerce(v) for k, v in raw.items()})
    else:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
    return rows


def _coerce(v: str) -> Any:
    if v == "\\N":
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
