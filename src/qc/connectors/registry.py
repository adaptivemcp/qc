"""Connector registry: maps connector names to implementations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from qc.domain.errors import InvalidArgumentsError, UnsupportedError

if TYPE_CHECKING:
    from qc.application.runtime import Runtime
    from qc.connectors.base import DataConnector
    from qc.domain.models import SourceConfig

# Optional connectors register lazily so missing driver deps don't break
# unrelated commands (SPEC 6.1 capability declarations).
_BUILTIN = {
    "fixture": "qc.connectors.fixture:FixtureConnector",
    "postgres": "qc.connectors.postgres:PostgresConnector",
    "mysql": "qc.connectors.mysql:MySQLConnector",
    "sqlite": "qc.connectors.sqlite_conn:SQLiteConnector",
    "google-sheets": "qc.connectors.google_sheets:GoogleSheetsConnector",
    "lark-sheets": "qc.connectors.lark_sheets:LarkSheetsConnector",
    "maxcompute": "qc.connectors.maxcompute:MaxComputeConnector",
}


def _load(path: str) -> type[Any]:
    module_name, _, attr = path.partition(":")
    import importlib

    module = importlib.import_module(module_name)
    cls: type[Any] = getattr(module, attr)
    return cls


def build_connector(config: SourceConfig, *, runtime: Runtime | None = None) -> DataConnector:
    """Instantiate the connector for a source; resolves fixture roots."""
    connector_type = config.connector
    cls_path = _BUILTIN.get(connector_type)
    if cls_path is None:
        raise UnsupportedError(
            f"unknown connector type '{connector_type}'",
            detail={"supported": ", ".join(sorted(_BUILTIN))},
        )
    cls = _load(cls_path)
    if connector_type == "fixture":
        # fixture sources point at a directory of jsonl/csv files
        from qc.security.secrets import resolve_secret

        root_value = config.options.get("root")
        if root_value:
            root = Path(root_value).expanduser()
        else:
            root = Path(resolve_secret(config.connection_ref)).expanduser()
        return cast("DataConnector", cls(root))
    return cast("DataConnector", cls())


def supported_connectors() -> dict[str, bool]:
    """Connector name -> importable (driver installed)."""
    out: dict[str, bool] = {}
    for name, path in _BUILTIN.items():
        try:
            _load(path)
            out[name] = True
        except Exception:
            out[name] = False
    return out


def register_connector(name: str, path: str) -> None:
    """Register an additional connector implementation."""
    if not name or ":" not in path:
        raise InvalidArgumentsError("connector registration needs name and 'module:attr'")
    _BUILTIN[name] = path
