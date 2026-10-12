"""Profile artifact serialization, validation, and migrations (SPEC 10.4, 14).

Artifacts are JSON with ``schema_version``. Unknown future versions fail
safe with an actionable error; older versions migrate forward.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from pathlib import Path

from qc.domain.errors import NotFoundError, ValidationError
from qc.domain.models import SCHEMA_VERSION_PROFILE, ProfileArtifact

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _safe_id(value: str) -> bool:
    """Valid artifact id: safe charset AND not dot-only (blocks '..', '.')."""
    return bool(SAFE_ID_RE.match(value)) and value.strip(".") != ""


def artifact_path(profile_dir: Path, profile_id: str, fingerprint: str | None = None) -> Path:
    """``profiles/<fingerprint-or-id>.profile.json``; rejects traversal."""
    if not _safe_id(profile_id):
        raise ValidationError("invalid profile id")
    key = fingerprint if fingerprint and _safe_id(fingerprint) else profile_id
    if not _safe_id(key):
        raise ValidationError("invalid fingerprint")
    return profile_dir / f"{key}.profile.json"


def save_profile(profile_dir: Path, artifact: ProfileArtifact) -> Path:
    """Persist a profile artifact with restrictive permissions."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        profile_dir.chmod(0o700)
    path = artifact_path(profile_dir, artifact.profile_id, artifact.source_fingerprint)
    payload = json.dumps(artifact.json_safe(), indent=2, ensure_ascii=False)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return path


def load_profile(path: Path) -> ProfileArtifact:
    """Load + version-check + migrate a profile artifact."""
    if not path.is_file():
        raise NotFoundError(f"profile not found: {path.name}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"corrupt profile artifact {path.name}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationError("profile artifact must be a JSON object")
    version = raw.get("schema_version")
    if not isinstance(version, int):
        raise ValidationError("profile artifact missing schema_version")
    if version > SCHEMA_VERSION_PROFILE:
        raise ValidationError(
            f"profile schema_version {version} is newer than supported "
            f"{SCHEMA_VERSION_PROFILE}; upgrade QC to read this artifact"
        )
    if version < SCHEMA_VERSION_PROFILE:
        raw = _migrate(raw, version)
    try:
        return ProfileArtifact.model_validate(raw)
    except Exception as exc:
        raise ValidationError(f"invalid profile artifact: {exc}") from exc


def _migrate(raw: dict[str, object], from_version: int) -> dict[str, object]:
    """Forward migrations. v1 is current; none registered yet."""
    if from_version < 1:
        raise ValidationError(f"cannot migrate profile from schema_version {from_version}")
    return raw


def list_profiles(profile_dir: Path) -> list[Path]:
    if not profile_dir.is_dir():
        return []
    return sorted(profile_dir.glob("*.profile.json"))


def find_profile(profile_dir: Path, profile_ref: str) -> Path:
    """Locate by exact filename stem (profile_id or fingerprint prefix)."""
    if not _safe_id(profile_ref):
        raise ValidationError("invalid profile reference")
    direct = profile_dir / f"{profile_ref}.profile.json"
    if direct.is_file():
        return direct
    matches = [p for p in list_profiles(profile_dir) if p.name.startswith(profile_ref)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValidationError(f"ambiguous profile reference '{profile_ref}'")
    raise NotFoundError(f"profile not found: {profile_ref}")
