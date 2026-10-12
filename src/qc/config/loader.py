"""Application configuration (SPEC 13).

TOML config file ``qc.toml`` plus programmatic defaults. Secrets are
references only. All configuration is validated at startup.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from qc.domain.errors import InvalidArgumentsError

CONFIG_FILENAME = "qc.toml"
CONFIG_VERSION = 1


class BudgetConfig(BaseModel):
    max_queries: int = Field(default=100, ge=0)
    max_rows: int = Field(default=100_000, ge=0)
    max_bytes: int = Field(default=67_108_864, ge=0)
    max_runtime_seconds: float = Field(default=300.0, ge=0)
    max_inline_mcp_rows: int = Field(default=20, ge=0)
    max_inline_mcp_bytes: int = Field(default=65_536, ge=0)


class SecurityConfig(BaseModel):
    raw_sample_persistence: bool = False
    raw_mcp_rows: bool = False
    external_model_calls: bool = False
    source_writes: bool = False
    redact_logs: bool = True


class McpStdioConfig(BaseModel):
    enabled: bool = True


class McpHttpConfig(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    require_auth: bool = True
    auth_token_ref: str | None = None  # e.g. "env:QC_MCP_HTTP_TOKEN"
    rate_limit_per_minute: int = Field(default=120, ge=1)
    max_request_bytes: int = Field(default=1_048_576, ge=1)


class AppConfig(BaseModel):
    """Validated application configuration (SPEC 13)."""

    version: int = CONFIG_VERSION
    default_policy: str = "strict-local"
    state_dir: str = "~/.local/share/qc"
    profile_dir: str = "~/.local/share/qc/profiles"
    policy_dir: str = "policies"
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    mcp_stdio: McpStdioConfig = Field(default_factory=McpStdioConfig)
    mcp_http: McpHttpConfig = Field(default_factory=McpHttpConfig)

    @field_validator("version")
    @classmethod
    def _check_version(cls, v: int) -> int:
        if v != CONFIG_VERSION:
            raise ValueError(f"unsupported config version {v}; expected {CONFIG_VERSION}")
        return v

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser()

    @property
    def profiles_path(self) -> Path:
        return Path(self.profile_dir).expanduser()


def load_config(path: Path | str | None = None) -> AppConfig:
    """Load and validate ``qc.toml``; defaults when absent.

    Raises :class:`InvalidArgumentsError` on malformed configuration.
    """
    candidate: Path | None = None
    if path is not None:
        candidate = Path(path)
        if not candidate.is_file():
            raise InvalidArgumentsError(f"config file not found: {candidate}")
    else:
        cwd_default = Path.cwd() / CONFIG_FILENAME
        if cwd_default.is_file():
            candidate = cwd_default

    if candidate is None:
        return AppConfig()

    try:
        with candidate.open("rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise InvalidArgumentsError(f"invalid TOML in {candidate.name}: {exc}") from exc

    # TOML sections map to nested models; normalize aliases from spec example
    normalized = dict(raw)
    if "mcp" in raw and isinstance(raw["mcp"], dict):
        mcp = raw["mcp"]
        if "stdio" in mcp:
            normalized["mcp_stdio"] = mcp["stdio"]
        if "http" in mcp:
            normalized["mcp_http"] = mcp["http"]
        normalized.pop("mcp", None)

    try:
        return AppConfig.model_validate(normalized)
    except Exception as exc:  # pydantic ValidationError
        raise InvalidArgumentsError(f"invalid configuration: {exc}") from exc


def default_config_toml() -> str:
    """Template written by ``qc init`` (no secrets, SPEC 10.4)."""
    return """\
version = 1
default_policy = "strict-local"
state_dir = "~/.local/share/qc"
profile_dir = "~/.local/share/qc/profiles"
policy_dir = "policies"

[budgets]
max_queries = 100
max_rows = 100000
max_bytes = 67108864
max_runtime_seconds = 300
max_inline_mcp_rows = 20
max_inline_mcp_bytes = 65536

[security]
raw_sample_persistence = false
raw_mcp_rows = false
external_model_calls = false
source_writes = false
redact_logs = true

[mcp.stdio]
enabled = true

[mcp.http]
enabled = false
host = "127.0.0.1"
port = 8765
require_auth = true
"""


@dataclass
class Paths:
    """Resolved filesystem layout for a project (SPEC 10.4)."""

    root: Path
    config: Path
    policies: Path
    profiles: Path
    reports: Path
    output: Path
    sources_file: Path
    state_db: Path
    extra: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def for_root(cls, root: Path | str) -> Paths:
        root_path = Path(root).expanduser()
        return cls(
            root=root_path,
            config=root_path / CONFIG_FILENAME,
            policies=root_path / "policies",
            profiles=root_path / "profiles",
            reports=root_path / "reports",
            output=root_path / "output",
            sources_file=root_path / "sources.json",
            state_db=root_path / "state.db",
        )

    def ensure(self) -> Paths:
        import contextlib

        for d in (self.policies, self.profiles, self.reports, self.output):
            d.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                d.chmod(0o700)
        return self
