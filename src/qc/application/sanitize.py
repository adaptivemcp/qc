"""Sanitization use cases: bounded, policy-transformed preview/export (SPEC 8).

Policy is evaluated *before* source rows are read. Raw rows are denied
by default; every row leaving this module has passed through a
transformation action or an explicit ``keep`` decision.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qc.domain.enums import ColumnAction, OperationKind
from qc.domain.errors import PolicyDeniedError, ValidationError
from qc.security.policy import DecisionContext, Operation

if TYPE_CHECKING:
    from qc.application.runtime import Runtime

MAX_INLINE_PREVIEW_ROWS = 1000


async def preview_rows(
    rt: Runtime,
    *,
    source: str,
    dataset: str,
    policy_name: str,
    limit: int,
    columns: list[str] | None = None,
    include_null_patterns: bool = False,
    dry_run: bool = False,
    inline: bool = True,
) -> dict[str, Any]:
    """Read bounded rows and apply per-column policy actions.

    Order of operations (SPEC 3.2): policy check → column actions →
    read → transform → disclosure check → return.

    ``inline=True`` enforces the small-row cap for MCP-style inline
    responses; file exports set ``inline=False`` and rely on policy
    budget limits instead (SPEC 12.2 tool 6).
    """
    from qc.sanitization.actions import transform_row

    if inline and limit > MAX_INLINE_PREVIEW_ROWS:
        raise ValidationError(f"limit {limit} exceeds max {MAX_INLINE_PREVIEW_ROWS} preview rows")
    engine = rt.engine(policy_name)
    ctx = DecisionContext(source=source, dataset=dataset, transport="cli")
    decision = engine.require(Operation.ROW_OUTPUT, ctx)
    policy = engine.policy

    cfg = rt.source(source)
    qualified = dataset
    if not policy.dataset_permitted(source, qualified):
        raise PolicyDeniedError(
            f"dataset '{dataset}' not permitted by policy '{policy_name}'",
            detail={"policy": policy_name},
        )

    # Column action map: DROP/MANUAL_REVIEW handled before any read
    async def _collect() -> dict[str, Any]:
        ref = _ref(cfg, dataset)
        connector = rt.connector_for(cfg)
        try:
            from qc.connectors.base import SampleRequest

            request = SampleRequest(dataset=ref, limit=limit, columns=columns)
            rows: list[dict[str, Any]] = []
            async for row in connector.sample_rows(cfg, request):
                rows.append(row.values)
                if len(rows) >= limit:
                    break
            return {"rows": rows, "ref": ref}
        finally:
            await connector.close()

    collected = await _collect()
    raw_rows: list[dict[str, Any]] = collected["rows"]

    # Effective column set
    all_cols = list(raw_rows[0].keys()) if raw_rows else (columns or [])
    blocked = engine.columns_needing_review(source, all_cols)
    actions = {c: engine.column_action(source, c) for c in all_cols}

    transformed: list[dict[str, Any]] = []
    dropped: list[str] = [c for c, a in actions.items() if a.action == ColumnAction.DROP]
    for row in raw_rows:
        out: dict[str, Any] = {}
        for col, value in row.items():
            if col in dropped:
                continue
            action = actions.get(col)
            if action is None or action.action == ColumnAction.MANUAL_REVIEW:
                # unreviewed column: redact rather than leak (fail closed)
                out[col] = None
                continue
            out[col] = transform_row(value, action, column=col)
        transformed.append(out)

    if dry_run:
        transformed = transformed[:0]

    raw_allowed = engine.check(Operation.RAW_SAMPLE, ctx).allowed
    rt.audit.record(
        OperationKind.SANITIZE_PREVIEW,
        source_id=cfg.id,
        dataset_id=dataset,
        policy_version=policy.version,
        decision=decision.verdict,
        rows_affected=len(raw_rows),
        result_status="ok",
        detail={"dry_run": str(dry_run)},
    )
    result: dict[str, Any] = {
        "policy": policy_name,
        "dataset": dataset,
        "rows": transformed,
        "row_count": len(transformed),
        "raw_output": raw_allowed,
        "blocked": blocked,
        "dropped": dropped,
        "include_null_patterns": include_null_patterns,
        "warnings": (
            ["unreviewed columns were redacted; add explicit actions to export them"]
            if blocked
            else []
        ),
    }
    if include_null_patterns:
        # disclose only *counts* of null-like representations, never values
        result["null_patterns"] = _null_patterns(raw_rows, all_cols)
    return result


def _null_patterns(rows: list[dict[str, Any]], columns: list[str]) -> dict[str, dict[str, int]]:
    """Per-column counts of distinct null-like representations (SPEC 7.5/11.2)."""
    out: dict[str, dict[str, int]] = {}
    for col in columns:
        counts = {
            "sql_null": 0,
            "empty_string": 0,
            "whitespace_only": 0,
            "string_null": 0,
            "literal_na": 0,
        }
        for row in rows:
            v = row.get(col)
            if v is None:
                counts["sql_null"] += 1
            elif isinstance(v, str):
                if v == "":
                    counts["empty_string"] += 1
                elif v.strip() == "":
                    counts["whitespace_only"] += 1
                elif v.strip().lower() in ("null", "none", "nil"):
                    counts["string_null"] += 1
                elif v.strip().lower() in ("n/a", "na", "unknown"):
                    counts["literal_na"] += 1
        out[col] = counts
    return out


async def export_rows(
    rt: Runtime,
    *,
    source: str,
    dataset: str,
    policy_name: str,
    output: Path,
    fmt: str = "jsonl",
    limit: int | None = None,
    dry_run: bool = False,
    write_manifest: bool = True,
    columns: list[str] | None = None,
    csv_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sanitized export with manifest (SPEC 10.1, 10.2).

    ``csv_options`` forwards delimiter / header / quoting / protect-formula
    settings to the CSV writer; the manifest records them so null encoding
    and escaping transformations are auditable.
    """
    engine = rt.engine(policy_name)
    ctx = DecisionContext(source=source, dataset=dataset)
    engine.require(Operation.EXPORT, ctx)

    if not engine.policy.export_allowed:
        raise PolicyDeniedError(
            f"policy '{policy_name}' does not allow export (export_allowed=false)"
        )
    if fmt not in ("jsonl", "csv"):
        raise ValidationError(f"unsupported export format: {fmt}")

    effective_limit = limit or engine.policy.budget.max_rows
    effective_limit = min(effective_limit, engine.policy.budget.max_rows, rt.ceiling.max_rows)

    result = await preview_rows(
        rt,
        source=source,
        dataset=dataset,
        policy_name=policy_name,
        limit=effective_limit,
        columns=columns,
        dry_run=dry_run,
        inline=False,
    )
    rows: list[dict[str, Any]] = result["rows"]
    if columns:
        rows = [{k: v for k, v in r.items() if k in set(columns)} for r in rows]
    csv_opts = dict(csv_options or {})
    manifest_path = None
    if not dry_run:
        output.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "jsonl":
            _write_jsonl(output, rows)
        else:
            _write_csv(output, rows, **csv_opts)
        if write_manifest:
            manifest_path = _write_manifest(
                output,
                source=source,
                dataset=dataset,
                policy=policy_name,
                fmt=fmt,
                rows=len(rows),
                blocked=result["blocked"],
                csv_options=csv_opts,
            )
        rt.audit.record(
            OperationKind.SANITIZE_EXPORT,
            source_id=rt.source(source).id,
            dataset_id=dataset,
            policy_version=engine.policy.version,
            decision="allow",
            rows_affected=len(rows),
            result_status="ok",
            detail={"format": fmt},
        )
    return {
        "output": str(output),
        "manifest": str(manifest_path) if manifest_path else None,
        "rows": len(rows),
        "dry_run": dry_run,
        "format": fmt,
        "policy": policy_name,
        "blocked": result["blocked"],
    }


def _ref(cfg: Any, dataset: str):  # type: ignore[no-untyped-def]
    from qc.domain.models import DatasetRef

    if "." in dataset and not dataset.endswith((".jsonl", ".csv")):
        ns, _, name = dataset.rpartition(".")
        return DatasetRef(source_id=cfg.id, namespace=ns, name=name, locator=dataset)
    return DatasetRef(source_id=cfg.id, name=dataset, locator=dataset)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    from qc.exports.jsonl import write_jsonl

    write_jsonl(path, rows)


def _write_csv(path: Path, rows: list[dict[str, Any]], **options: Any) -> None:
    """CSV export with null encoding + formula-injection protection (SPEC 10.2)."""
    from qc.exports.csv_writer import write_csv_rows

    write_csv_rows(path, rows, **options)


def _write_manifest(
    path: Path,
    *,
    source: str,
    dataset: str,
    policy: str,
    fmt: str,
    rows: int,
    blocked: list[str],
    csv_options: dict[str, Any] | None = None,
) -> Path:
    from qc import TOOL_VERSION

    opts = csv_options or {}
    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "tool_version": TOOL_VERSION,
        "output": path.name,
        "format": fmt,
        "rows": rows,
        "source": source,
        "dataset": dataset,
        "policy": policy,
        "blocked_columns": blocked,
        "null_encoding": (
            "\\N marks null; empty string stays empty" if fmt == "csv" else "json null"
        ),
        "decimal_encoding": (
            "csv:text" if fmt == "csv" else "json:number when exact round-trip else string"
        ),
        "formula_injection_protection": (
            bool(opts.get("protect_formulas", True)) if fmt == "csv" else False
        ),
        "contains_raw_samples": False,
        "secrets": "none",
    }
    if fmt == "csv":
        manifest["csv"] = {
            "delimiter": opts.get("delimiter", ","),
            "header": bool(opts.get("header", True)),
            "protect_formulas": bool(opts.get("protect_formulas", True)),
        }
    mpath = path.with_suffix(path.suffix + ".manifest.json")
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return mpath
