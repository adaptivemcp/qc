"""Configuration and budget tests (SPEC 7.3, 13)."""

from __future__ import annotations

from pathlib import Path

import pytest

from qc.config.loader import AppConfig, Paths, default_config_toml, load_config
from qc.domain.enums import Budget, BudgetUsage
from qc.domain.errors import InvalidArgumentsError


class TestBudget:
    def test_exhausted_by_queries(self) -> None:
        b = Budget(max_queries=2)
        u = BudgetUsage()
        u.add_query()
        assert u.exhausted(b) is None
        u.add_query()
        assert u.exhausted(b) is None  # at limit, not over
        u.add_query()
        assert u.exhausted(b) == "max_queries"

    def test_exhausted_by_rows_and_bytes(self) -> None:
        u = BudgetUsage()
        u.add_query(rows=10, byte_count=100)
        assert u.exhausted(Budget(max_rows=5)) == "max_rows"
        assert u.exhausted(Budget(max_bytes=50)) == "max_bytes"
        assert u.exhausted(Budget(max_rows=10, max_bytes=100)) is None

    def test_would_exceed_precheck(self) -> None:
        u = BudgetUsage(queries=9)
        assert u.would_exceed(Budget(max_queries=10), queries=2)
        assert not u.would_exceed(Budget(max_queries=10), queries=1)

    def test_zero_is_a_real_limit(self) -> None:
        u = BudgetUsage(queries=1)
        assert u.exhausted(Budget(max_queries=0)) == "max_queries"

    def test_merge_override(self) -> None:
        base = Budget(max_queries=10, max_rows=100)
        merged = base.merge(Budget(max_rows=5))
        assert merged.max_queries == 10
        assert merged.max_rows == 5

    def test_runtime(self) -> None:
        u = BudgetUsage(runtime_seconds=10.0)
        assert u.exhausted(Budget(max_runtime_seconds=5.0)) == "max_runtime_seconds"


class TestLoadConfig:
    def test_defaults_when_absent(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        cfg = load_config(None)
        assert cfg.version == 1
        assert cfg.budgets.max_queries == 100

    def test_reads_spec_example(self, tmp_path: Path) -> None:
        p = tmp_path / "qc.toml"
        p.write_text(default_config_toml(), encoding="utf-8")
        cfg = load_config(p)
        assert cfg.default_policy == "strict-local"
        assert cfg.budgets.max_inline_mcp_rows == 20
        assert cfg.security.raw_mcp_rows is False
        assert cfg.mcp_http.enabled is False

    def test_missing_explicit_file(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidArgumentsError):
            load_config(tmp_path / "nope.toml")

    def test_invalid_toml(self, tmp_path: Path) -> None:
        p = tmp_path / "qc.toml"
        p.write_text("this is [not toml", encoding="utf-8")
        with pytest.raises(InvalidArgumentsError):
            load_config(p)

    def test_rejects_unknown_config_version(self, tmp_path: Path) -> None:
        p = tmp_path / "qc.toml"
        p.write_text("version = 99\n", encoding="utf-8")
        with pytest.raises(InvalidArgumentsError):
            load_config(p)

    def test_no_secrets_in_template(self) -> None:
        text = default_config_toml().lower()
        assert "password" not in text
        assert "secret" not in text
        assert "token" not in text

    def test_rejects_unknown_budget_key(self, tmp_path: Path) -> None:
        """A typo must fail loudly, not silently drop a limit."""
        p = tmp_path / "qc.toml"
        p.write_text("version = 1\n[budgets]\nmax_query = 10\n", encoding="utf-8")
        with pytest.raises(InvalidArgumentsError):
            load_config(p)

    def test_rejects_unknown_security_key(self, tmp_path: Path) -> None:
        p = tmp_path / "qc.toml"
        p.write_text("version = 1\n[security]\nraw_mcp_row = false\n", encoding="utf-8")
        with pytest.raises(InvalidArgumentsError):
            load_config(p)

    def test_rejects_unknown_mcp_http_key(self, tmp_path: Path) -> None:
        """Regression: docs previously named this max_request_body_size."""
        p = tmp_path / "qc.toml"
        p.write_text("version = 1\n[mcp.http]\nmax_request_body_size = 1048576\n", encoding="utf-8")
        with pytest.raises(InvalidArgumentsError):
            load_config(p)

    def test_accepts_correct_mcp_http_key(self, tmp_path: Path) -> None:
        p = tmp_path / "qc.toml"
        p.write_text(
            "version = 1\n[mcp.http]\nenabled = false\nmax_request_bytes = 2048\n",
            encoding="utf-8",
        )
        assert load_config(p).mcp_http.max_request_bytes == 2048


class TestPaths:
    def test_layout(self, tmp_path: Path) -> None:
        paths = Paths.for_root(tmp_path).ensure()
        assert paths.profiles.is_dir()
        assert paths.policies.is_dir()
        assert paths.config.name == "qc.toml"

    def test_state_dir_defaults(self) -> None:
        cfg = AppConfig()
        assert "~" in cfg.state_dir
