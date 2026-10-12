"""Source registry: persisted source configurations (SPEC 5.1, 11.1).

Stored as JSON with restrictive permissions. Only secret *references*
are ever written; the registry never sees resolved credentials.
"""

from __future__ import annotations

import builtins
import json
import os
from pathlib import Path

from qc.domain.errors import InvalidArgumentsError, NotFoundError
from qc.domain.models import SourceConfig

SAFE_NAME = __import__("re").compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


class SourceRegistry:
    """Load/save source configs at ``<root>/sources.json``."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def list(self) -> list[SourceConfig]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InvalidArgumentsError(f"corrupt sources registry: {exc}") from exc
        if not isinstance(raw, dict) or "sources" not in raw:
            raise InvalidArgumentsError("sources registry must be an object with 'sources'")
        return [SourceConfig.model_validate(s) for s in raw["sources"]]

    def get(self, name: str) -> SourceConfig:
        for src in self.list():
            if src.name == name:
                return src
        raise NotFoundError(f"source not found: {name}")

    def get_by_id(self, source_id: str) -> SourceConfig | None:
        """Look up by opaque id (policy keys are names, artifacts store ids)."""
        for src in self.list():
            if src.id == source_id:
                return src
        return None

    def add(self, config: SourceConfig) -> SourceConfig:
        if not SAFE_NAME.match(config.name):
            raise InvalidArgumentsError("source name must match [a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")
        existing = self.list()
        if any(s.name == config.name for s in existing):
            raise InvalidArgumentsError(f"source already exists: {config.name}")
        existing.append(config)
        self._save(existing)
        return config

    def remove(self, name: str) -> None:
        existing = [s for s in self.list() if s.name != name]
        if len(existing) == len(self.list()):
            raise NotFoundError(f"source not found: {name}")
        self._save(existing)

    def _save(self, sources: builtins.list[SourceConfig]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": 1, "sources": [s.json_safe() for s in sources]},
            indent=2,
            ensure_ascii=False,
        )
        tmp = self.path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, self.path)
