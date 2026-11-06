"""Artifact schema-version policy (SPEC 14, SPEC 10.4).

One rule, applied everywhere an artifact is read: the file records the version of the
shape it was written with.

- **Same version** — read it.
- **Older version** — migrate forward, through an *explicit* registered migration.
  Guessing at a shape is how a tool silently corrupts someone's data.
- **Newer version** — refuse, with an actionable message. A newer file may contain
  fields this build would drop on the next write, so "read what we understand and
  ignore the rest" is data loss with a friendly face.

This lives in one place because the failure mode is a single missed call site: the
profile loader had the check and the generation-plan loader did not, so a future plan
was accepted without complaint.
"""

from __future__ import annotations

from typing import Any

from qc.domain.errors import ValidationError


def read_schema_version(kind: str, raw: Any) -> int:
    """The artifact's declared version, or a validation error explaining why not."""
    if not isinstance(raw, dict):
        raise ValidationError(f"{kind} artifact must be a JSON object")
    version = raw.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValidationError(f"{kind} artifact missing schema_version")
    return version


def check_supported(kind: str, raw: Any, supported: int) -> int:
    """Validate that this build can read the artifact; return its version.

    Older versions are returned unchanged so the caller can run its migration table —
    this function deliberately does not migrate, because only the owning module knows
    what its versions mean.
    """
    version = read_schema_version(kind, raw)
    if version > supported:
        raise ValidationError(
            f"{kind} schema_version {version} is newer than supported {supported}; "
            "upgrade QC to read this artifact"
        )
    return version
