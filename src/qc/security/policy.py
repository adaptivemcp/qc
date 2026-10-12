"""Policy evaluation engine (SPEC 5.7, 8.2, 13).

Deny by default. Every operation that could touch source data or egress
rows calls into this engine *before* reading data. A policy can never
grant more access than the server's configured ceiling (``Ceiling``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from qc.domain.enums import ColumnAction, ProfileDisclosure
from qc.domain.errors import PolicyDeniedError
from qc.domain.models import ColumnPolicy, SanitizationPolicy, SourceConfig


class Operation(StrEnum):
    """Operations subject to policy checks."""

    SOURCE_ACCESS = "source_access"
    DATASET_READ = "dataset_read"
    PROFILE_RUN = "profile_run"
    PROFILE_DISCLOSE = "profile_disclose"
    RAW_SAMPLE = "raw_sample"
    ROW_OUTPUT = "row_output"
    EXPORT = "export"
    WRITE = "write"
    EXTERNAL_EGRESS = "external_egress"
    FULL_SCAN = "full_scan"
    EXTERNAL_MODEL = "external_model"


@dataclass(frozen=True)
class Ceiling:
    """Server-level hard limits a policy may only narrow, never widen."""

    max_rows: int = 10_000
    max_bytes: int = 16_777_216
    max_runtime_seconds: float = 120.0
    max_queries: int = 100
    raw_mcp_rows: bool = False
    external_model_calls: bool = False
    source_writes: bool = False
    max_inline_mcp_rows: int = 20
    max_inline_mcp_bytes: int = 65_536


@dataclass(frozen=True)
class DecisionContext:
    """Who/what is asking, for what, and under which configuration."""

    actor: str = "local"
    source: str | None = None
    dataset: str | None = None
    transport: str = "cli"  # "cli" | "mcp-stdio" | "mcp-http"
    client_id: str | None = None
    destination: str | None = None


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str
    checks: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        return "allow" if self.allowed else "deny"


class PolicyEngine:
    """Evaluates a :class:`SanitizationPolicy` plus server ceiling."""

    def __init__(self, policy: SanitizationPolicy, ceiling: Ceiling | None = None) -> None:
        self.policy = policy
        self.ceiling = ceiling or Ceiling()

    # ------------------------------------------------------------ primitives

    def _default(self, key: str, fallback: str = "deny") -> str:
        return str(self.policy.defaults.get(key, fallback))

    def check(self, operation: Operation, ctx: DecisionContext) -> PolicyDecision:
        """Evaluate one operation. Raises nothing; caller decides."""
        checks: list[str] = []

        def deny(reason: str) -> PolicyDecision:
            return PolicyDecision(False, reason, checks)

        if operation == Operation.SOURCE_ACCESS:
            if ctx.source is None:
                return deny("no source specified")
            src = self.policy.sources.get(ctx.source)
            if src is None:
                # Unlisted sources fall back to the deny-by-default stance.
                if self._default("source_access", "deny") != "read":
                    return deny("defaults.source_access is deny and source is unlisted")
                checks.append("source permitted by defaults")
                return PolicyDecision(True, "source read permitted by defaults", checks)
            if src.access != "read":
                return deny(f"source '{ctx.source}' access is {src.access}")
            checks.append("source permitted")
            return PolicyDecision(True, "source read permitted", checks)

        if operation == Operation.DATASET_READ:
            base = self.check(Operation.SOURCE_ACCESS, ctx)
            checks.extend(base.checks)
            if not base.allowed:
                return base
            assert ctx.source is not None and ctx.dataset is not None
            if not self.policy.dataset_permitted(ctx.source, ctx.dataset):
                return deny(f"dataset '{ctx.dataset}' not permitted by policy")
            checks.append("dataset permitted")
            return PolicyDecision(True, "dataset read permitted", checks)

        if operation == Operation.PROFILE_RUN:
            return self.check(Operation.DATASET_READ, ctx)

        if operation == Operation.PROFILE_DISCLOSE:
            level = self._default("profile_disclosure", "aggregate_only")
            if level == ProfileDisclosure.NONE.value:
                return deny("profile disclosure is none")
            if self.ceiling.raw_mcp_rows is False and level == ProfileDisclosure.FULL.value:
                # full disclosure of a profile (column names etc.) still requires
                # dataset-level permission
                inner = self.check(Operation.DATASET_READ, ctx)
                checks.extend(inner.checks)
                if not inner.allowed:
                    return inner
            checks.append(f"disclosure level {level}")
            return PolicyDecision(True, f"profile disclosure level {level}", checks)

        if operation == Operation.RAW_SAMPLE:
            if not self.policy.allow_raw_samples:
                return deny("raw sample disclosure denied by default")
            if self._default("raw_row_output", "deny") != "allow":
                return deny("defaults.raw_row_output is deny")
            inner = self.check(Operation.DATASET_READ, ctx)
            checks.extend(inner.checks)
            if not inner.allowed:
                return inner
            return PolicyDecision(True, "raw samples explicitly permitted", checks)

        if operation == Operation.ROW_OUTPUT:
            if self._default("raw_row_output", "deny") == "allow":
                # rows allowed as-is only if raw allowed; otherwise transforms apply
                inner = self.check(Operation.DATASET_READ, ctx)
                checks.extend(inner.checks)
                if not inner.allowed:
                    return inner
                return PolicyDecision(True, "raw row output permitted", checks)
            # transformed (sanitized) rows: still need dataset permission
            inner = self.check(Operation.DATASET_READ, ctx)
            checks.extend(inner.checks)
            if not inner.allowed:
                return inner
            checks.append("rows must be transformed before output")
            return PolicyDecision(True, "sanitized row output permitted", checks)

        if operation == Operation.EXPORT:
            if not self.policy.export_allowed:
                return deny("export denied by policy")
            inner = self.check(Operation.DATASET_READ, ctx)
            checks.extend(inner.checks)
            if not inner.allowed:
                return inner
            return PolicyDecision(True, "export permitted", checks)

        if operation == Operation.WRITE:
            if self.ceiling.source_writes:
                # ceiling allows writes, but policy must also allow
                if not self.policy.write_allowed:
                    return deny("write denied by policy")
            else:
                return deny("server ceiling disables source writes")
            if self._default("source_writes", "deny") != "allow":
                return deny("defaults.source_writes is deny")
            return PolicyDecision(True, "write permitted", checks)

        if operation == Operation.EXTERNAL_EGRESS:
            if self._default("external_egress", "deny") != "allow":
                return deny("external egress denied by default")
            if ctx.destination is None or ctx.destination not in self.policy.allowed_destinations:
                return deny("destination not in allowed_destinations")
            return PolicyDecision(True, "egress to approved destination", checks)

        if operation == Operation.EXTERNAL_MODEL:
            if not self.ceiling.external_model_calls:
                return deny("server ceiling disables external model calls")
            if self._default("external_model_calls", "deny") not in ("allow", True):
                return deny("external model calls denied by policy")
            return PolicyDecision(True, "external model calls permitted", checks)

        if operation == Operation.FULL_SCAN:
            allow_full = self.policy.allow_aggregate_full_scan or self.policy.budget.allow_full_scan
            if not allow_full:
                return deny("full scan requires explicit allow_full_scan in policy")
            return PolicyDecision(True, "full scan approved by policy", checks)

        return PolicyDecision(False, f"unknown operation {operation}", checks)

    def require(self, operation: Operation, ctx: DecisionContext) -> PolicyDecision:
        """Like :meth:`check` but raises :class:`PolicyDeniedError` on deny."""
        decision = self.check(operation, ctx)
        if not decision.allowed:
            raise PolicyDeniedError(
                f"policy denied {operation.value}: {decision.reason}",
                detail={"operation": operation.value, "reason": decision.reason},
            )
        return decision

    # ------------------------------------------------------------ ceilings

    def effective_budget(self, requested_rows: int | None = None) -> tuple[int, int, float, int]:
        """Return (max_rows, max_bytes, max_runtime, max_queries), narrowed by ceiling."""
        bp = self.policy.budget
        max_rows = min(bp.max_rows, self.ceiling.max_rows)
        max_bytes = min(bp.max_bytes, self.ceiling.max_bytes)
        max_runtime = min(bp.max_runtime_seconds, self.ceiling.max_runtime_seconds)
        max_queries = min(bp.max_queries, self.ceiling.max_queries)
        if requested_rows is not None:
            max_rows = min(max_rows, requested_rows)
        return max_rows, max_bytes, max_runtime, max_queries

    # ------------------------------------------------------- column handling

    def column_action(self, source_name: str, column: str) -> ColumnPolicy:
        """Effective per-column action; unlisted columns default to manual_review."""
        src = self.policy.sources.get(source_name)
        if src is None:
            return ColumnPolicy(action=ColumnAction.MANUAL_REVIEW)
        policy = src.columns.get(column)
        if policy is None:
            return ColumnPolicy(action=ColumnAction.MANUAL_REVIEW)
        return policy

    def columns_needing_review(self, source_name: str, columns: list[str]) -> list[str]:
        """Columns with no explicit action: blocked for row-level export (SPEC 5.7)."""
        src = self.policy.sources.get(source_name)
        if src is None:
            return list(columns)
        return [c for c in columns if c not in src.columns]


def engine_for(policy: SanitizationPolicy, ceiling: Ceiling | None = None) -> PolicyEngine:
    return PolicyEngine(policy, ceiling)


def context_from_source(
    config: SourceConfig,
    dataset: str | None = None,
    *,
    actor: str = "local",
    transport: str = "cli",
    client_id: str | None = None,
) -> DecisionContext:
    return DecisionContext(
        source=config.name,
        dataset=dataset,
        actor=actor,
        transport=transport,
        client_id=client_id,
    )
