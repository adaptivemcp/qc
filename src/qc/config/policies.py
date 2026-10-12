"""Policy file loading and validation (SPEC 13, 11.1 ``qc policy validate``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from qc.domain.enums import ColumnAction
from qc.domain.errors import InvalidArgumentsError, ValidationError
from qc.domain.models import (
    BudgetPolicy,
    ColumnPolicy,
    SanitizationPolicy,
    SourcePolicy,
)

_VALID_ACTIONS = {a.value for a in ColumnAction}
_VALID_ACCESS = {"read", "deny"}


def _from_dict(data: dict[str, Any]) -> SanitizationPolicy:
    """Validate and normalize a raw policy mapping with actionable errors."""
    if not isinstance(data, dict):
        raise InvalidArgumentsError("policy document must be a mapping")

    raw_sources = data.pop("sources", {}) or {}
    if not isinstance(raw_sources, dict):
        raise InvalidArgumentsError("policy 'sources' must be a mapping")

    sources: dict[str, SourcePolicy] = {}
    for src_name, src_body in raw_sources.items():
        if not isinstance(src_body, dict):
            raise InvalidArgumentsError(f"sources.{src_name} must be a mapping")
        body = dict(src_body)
        access = body.get("access", "deny")
        if access not in _VALID_ACCESS:
            raise InvalidArgumentsError(
                f"sources.{src_name}.access must be one of {sorted(_VALID_ACCESS)}"
            )
        columns_raw = body.get("columns", {}) or {}
        if not isinstance(columns_raw, dict):
            raise InvalidArgumentsError(f"sources.{src_name}.columns must be a mapping")
        columns: dict[str, ColumnPolicy] = {}
        for col_name, col_body in columns_raw.items():
            if isinstance(col_body, str):
                col_body = {"action": col_body}
            if not isinstance(col_body, dict):
                raise InvalidArgumentsError(
                    f"sources.{src_name}.columns.{col_name} must be a mapping or action string"
                )
            action = col_body.get("action")
            if action not in _VALID_ACTIONS:
                raise InvalidArgumentsError(
                    f"sources.{src_name}.columns.{col_name}.action must be one of "
                    f"{sorted(_VALID_ACTIONS)}; got {action!r}"
                )
            try:
                columns[col_name] = ColumnPolicy.model_validate(col_body)
            except Exception as exc:
                raise InvalidArgumentsError(
                    f"invalid column policy for {src_name}.{col_name}: {exc}"
                ) from exc
        datasets = body.get("datasets", []) or []
        if not isinstance(datasets, list):
            raise InvalidArgumentsError(f"sources.{src_name}.datasets must be a list")
        sources[src_name] = SourcePolicy(
            access=access,
            datasets=[str(d) for d in datasets],
            columns=columns,
        )

    budget_raw = data.pop("budget", None) or data.pop("budgets", None)
    try:
        budget = BudgetPolicy.model_validate(budget_raw) if budget_raw else BudgetPolicy()
    except Exception as exc:
        raise InvalidArgumentsError(f"invalid policy budget: {exc}") from exc

    known = SanitizationPolicy.model_fields.keys()
    unknown = set(data.keys()) - known
    if unknown:
        raise InvalidArgumentsError(
            f"unknown policy keys: {sorted(unknown)}; allowed: {sorted(known)}"
        )

    try:
        return SanitizationPolicy.model_validate({**data, "sources": sources, "budget": budget})
    except Exception as exc:
        raise InvalidArgumentsError(f"invalid policy: {exc}") from exc


def parse_policy(text: str, *, fmt: str | None = None) -> SanitizationPolicy:
    """Parse YAML (default) or JSON policy text."""
    fmt = fmt or ("json" if text.lstrip().startswith("{") else "yaml")
    try:
        data = json.loads(text) if fmt == "json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise InvalidArgumentsError(f"cannot parse policy file: {exc}") from exc
    if data is None:
        raise InvalidArgumentsError("policy file is empty")
    return _from_dict(dict(data))


def load_policy(path: Path | str) -> SanitizationPolicy:
    p = Path(path)
    if not p.is_file():
        raise InvalidArgumentsError(f"policy file not found: {p}")
    return parse_policy(p.read_text(encoding="utf-8"))


def validate_policy_file(path: Path | str) -> SanitizationPolicy:
    """``qc policy validate``: parse + semantic checks; raises on failure."""
    policy = load_policy(path)
    if not policy.name:
        raise ValidationError("policy must have a non-empty name")
    if policy.export_allowed and not policy.allowed_destinations:
        raise ValidationError(
            "export_allowed is true but allowed_destinations is empty; declare where rows may go"
        )
    for src_name, src in policy.sources.items():
        if src.access == "read" and not src.datasets:
            raise ValidationError(
                f"source '{src_name}' has read access but no datasets allowlist; "
                "list datasets or set access: deny"
            )
        if "*" in src.datasets:
            raise ValidationError(
                f"source '{src_name}' allowlists '*'; explicit dataset lists are required"
            )
    return policy


def default_policy_yaml() -> str:
    """Strict-default policy written by ``qc init`` (SPEC 5.7, 13)."""
    return """\
version: 1
name: strict-local
description: Deny-by-default local policy (SPEC 5.7 defaults)
defaults:
  source_access: deny
  raw_row_output: deny
  source_writes: deny
  external_egress: deny
  profile_disclosure: aggregate_only
  external_model_calls: false
budget:
  max_rows: 10000
  max_bytes: 16777216
  max_runtime_seconds: 120
  max_queries: 100
  allow_full_scan: false
export_allowed: false
write_allowed: false
allow_raw_samples: false
sources: {}
"""
