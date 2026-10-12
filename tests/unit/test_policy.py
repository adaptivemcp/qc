"""Policy engine tests (SPEC 5.7, 8.2, 13)."""

from __future__ import annotations

import pytest

from qc.config.policies import default_policy_yaml, parse_policy, validate_policy_file
from qc.domain.errors import PolicyDeniedError
from qc.security.policy import Ceiling, DecisionContext, Operation, PolicyEngine


def strict_policy() -> PolicyEngine:
    return PolicyEngine(parse_policy(default_policy_yaml()))


def permissive_policy() -> PolicyEngine:
    text = (
        default_policy_yaml()
        + """
sources:
  analytics:
    access: read
    datasets:
      - public.orders
    columns:
      customer_email:
        action: synthetic
      total_amount:
        action: keep
"""
    )
    return PolicyEngine(parse_policy(text))


class TestDefaults:
    def test_deny_by_default_source(self) -> None:
        e = strict_policy()
        d = e.check(Operation.SOURCE_ACCESS, DecisionContext(source="anything"))
        assert not d.allowed
        assert d.reason

    def test_deny_unlisted_dataset(self) -> None:
        e = permissive_policy()
        d = e.check(
            Operation.DATASET_READ,
            DecisionContext(source="analytics", dataset="public.other"),
        )
        assert not d.allowed

    def test_allow_listed_dataset(self) -> None:
        e = permissive_policy()
        d = e.check(
            Operation.DATASET_READ,
            DecisionContext(source="analytics", dataset="public.orders"),
        )
        assert d.allowed

    def test_raw_sample_denied(self) -> None:
        e = permissive_policy()
        d = e.check(
            Operation.RAW_SAMPLE,
            DecisionContext(source="analytics", dataset="public.orders"),
        )
        assert not d.allowed
        assert "default" in d.reason or "denied" in d.reason

    def test_write_denied_even_when_policy_allows(self) -> None:
        e = permissive_policy()
        d = e.check(Operation.WRITE, DecisionContext(source="analytics"))
        assert not d.allowed  # ceiling.source_writes is False

    def test_external_egress_denied(self) -> None:
        e = permissive_policy()
        d = e.check(Operation.EXTERNAL_EGRESS, DecisionContext(source="analytics"))
        assert not d.allowed

    def test_external_model_denied(self) -> None:
        e = strict_policy()
        d = e.check(Operation.EXTERNAL_MODEL, DecisionContext())
        assert not d.allowed

    def test_full_scan_denied_without_flag(self) -> None:
        e = permissive_policy()
        d = e.check(Operation.FULL_SCAN, DecisionContext(source="analytics"))
        assert not d.allowed


class TestRequire:
    def test_require_raises_policy_denied(self) -> None:
        e = strict_policy()
        with pytest.raises(PolicyDeniedError) as exc:
            e.require(Operation.DATASET_READ, DecisionContext(source="x", dataset="y"))
        assert exc.value.code.value == "POLICY_DENIED"
        assert exc.value.exit_code == 4

    def test_require_passes_when_allowed(self) -> None:
        e = permissive_policy()
        d = e.require(
            Operation.PROFILE_RUN,
            DecisionContext(source="analytics", dataset="public.orders"),
        )
        assert d.allowed


class TestBudgetCeiling:
    def test_policy_budget_cannot_exceed_ceiling(self) -> None:
        policy = parse_policy(
            default_policy_yaml() + "\nbudget:\n  max_rows: 1000000\n  max_bytes: 999999999\n"
        )
        e = PolicyEngine(policy, Ceiling(max_rows=50, max_bytes=100))
        rows, byte_count, _rt, _q = e.effective_budget()
        assert rows == 50
        assert byte_count == 100

    def test_ceiling_blocks_model_calls(self) -> None:
        policy = parse_policy(default_policy_yaml())
        policy.defaults["external_model_calls"] = True
        e = PolicyEngine(policy, Ceiling(external_model_calls=False))
        assert not e.check(Operation.EXTERNAL_MODEL, DecisionContext()).allowed


class TestColumnHandling:
    def test_unlisted_column_needs_review(self) -> None:
        e = permissive_policy()
        assert e.column_action("analytics", "unknown_col").action.value == "manual_review"

    def test_listed_column_action(self) -> None:
        e = permissive_policy()
        assert e.column_action("analytics", "customer_email").action.value == "synthetic"
        assert e.column_action("analytics", "total_amount").action.value == "keep"

    def test_columns_needing_review(self) -> None:
        e = permissive_policy()
        pending = e.columns_needing_review(
            "analytics", ["customer_email", "total_amount", "nickname"]
        )
        assert pending == ["nickname"]


class TestPolicyValidation:
    def test_validate_rejects_wildcard_dataset(self, tmp_path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text(
            default_policy_yaml() + "\nsources:\n  s:\n    access: read\n    datasets: ['*']\n",
            encoding="utf-8",
        )
        from qc.domain.errors import ValidationError

        with pytest.raises(ValidationError):
            validate_policy_file(p)

    def test_validate_rejects_read_without_datasets(self, tmp_path) -> None:
        p = tmp_path / "bad2.yaml"
        p.write_text(
            default_policy_yaml() + "\nsources:\n  s:\n    access: read\n",
            encoding="utf-8",
        )
        from qc.domain.errors import ValidationError

        with pytest.raises(ValidationError):
            validate_policy_file(p)

    def test_invalid_action_rejected(self) -> None:
        from qc.domain.errors import InvalidArgumentsError

        with pytest.raises(InvalidArgumentsError):
            parse_policy(
                default_policy_yaml() + "\nsources:\n  s:\n    access: read\n    datasets: [t]\n"
                "    columns:\n      c:\n        action: explode\n"
            )

    def test_unknown_key_rejected(self) -> None:
        from qc.domain.errors import InvalidArgumentsError

        with pytest.raises(InvalidArgumentsError):
            parse_policy("name: x\nnot_a_real_key: 1\n")

    def test_export_allowed_needs_destinations(self, tmp_path) -> None:
        from qc.domain.errors import ValidationError

        p = tmp_path / "exp.yaml"
        p.write_text(default_policy_yaml() + "\nexport_allowed: true\n", encoding="utf-8")
        with pytest.raises(ValidationError):
            validate_policy_file(p)
