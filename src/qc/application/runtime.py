"""Runtime context: wires config, policy, sources, state, and connectors.

Both the CLI and MCP server construct a :class:`Runtime` and pass it to
application services so both interfaces share the same policy checks.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from qc.application.jobs import StateStore
from qc.application.sources import SourceRegistry
from qc.config.loader import AppConfig, Paths, load_config
from qc.config.policies import load_policy
from qc.domain.errors import InvalidArgumentsError, NotFoundError
from qc.domain.models import SanitizationPolicy, SourceConfig
from qc.security.audit import AuditLog
from qc.security.policy import Ceiling, PolicyEngine

if TYPE_CHECKING:
    from qc.connectors.base import DataConnector


class Runtime:
    """Everything a use case needs, assembled once at process start."""

    def __init__(
        self,
        *,
        root: Path | str | None = None,
        config_path: Path | str | None = None,
        config: AppConfig | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve() if root else Path.cwd()
        if config is None and config_path is None:
            # explicit root: prefer its own qc.toml over process cwd
            root_config = self.root / "qc.toml"
            config_path = root_config if root_config.is_file() else None
        self.config = config or load_config(config_path)
        self.paths = Paths.for_root(self.root).ensure()
        self.sources = SourceRegistry(self.paths.sources_file)
        self.state = StateStore(self.paths.state_db)
        self.audit = AuditLog(self.paths.root / "audit.jsonl")
        self.ceiling = Ceiling(
            max_rows=self.config.budgets.max_rows,
            max_bytes=self.config.budgets.max_bytes,
            max_runtime_seconds=self.config.budgets.max_runtime_seconds,
            max_queries=self.config.budgets.max_queries,
            raw_mcp_rows=self.config.security.raw_mcp_rows,
            external_model_calls=self.config.security.external_model_calls,
            source_writes=self.config.security.source_writes,
            max_inline_mcp_rows=self.config.budgets.max_inline_mcp_rows,
            max_inline_mcp_bytes=self.config.budgets.max_inline_mcp_bytes,
        )
        self._policy_cache: dict[str, SanitizationPolicy] = {}

    # ------------------------------------------------------------------ policy

    def policy_path(self, name: str) -> Path:
        return self.paths.policies / f"{name}.yaml"

    def load_policy_named(self, name: str | None = None) -> SanitizationPolicy:
        """Load a policy by name (defaults to config.default_policy)."""
        pname = name or self.config.default_policy
        if pname in self._policy_cache:
            return self._policy_cache[pname]
        path = self.policy_path(pname)
        if not path.is_file():
            # fall back to built-in strict defaults so we never fail open
            from qc.config.policies import default_policy_yaml, parse_policy

            if pname == self.config.default_policy and not path.exists():
                policy = parse_policy(default_policy_yaml())
                self._policy_cache[pname] = policy
                return policy
            raise NotFoundError(f"policy not found: {pname}")
        policy = load_policy(path)
        self._policy_cache[pname] = policy
        return policy

    def engine(self, policy_name: str | None = None) -> PolicyEngine:
        return PolicyEngine(self.load_policy_named(policy_name), self.ceiling)

    # ---------------------------------------------------------------- sources

    def source(self, name: str) -> SourceConfig:
        return self.sources.get(name)

    def connector_for(self, config: SourceConfig) -> DataConnector:
        """Build the connector registered for a source config."""
        from qc.connectors.registry import build_connector

        return build_connector(config, runtime=self)

    def close(self) -> None:
        self.state.close()


def require_configured(root: Path | str | None = None) -> Runtime:
    """Runtime for commands that need an initialized project."""
    rt = Runtime(root=root)
    if not rt.paths.config.is_file():
        raise InvalidArgumentsError("not a QC project (qc.toml missing); run 'qc init' first")
    return rt
