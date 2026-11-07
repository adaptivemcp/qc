"""Generation and validation report artifacts (SPEC 10.4, 14).

Reports are written by `run_generation` and `validate_output`. This is the reader side:
the same version rule the profile and plan loaders use — same version reads, older
migrates, newer refuses with an upgrade instruction — so a report written by a newer QC
cannot be half-read by an older one.
"""

from __future__ import annotations

import json
from pathlib import Path

from qc.artifacts.versions import check_supported
from qc.domain.errors import NotFoundError, ValidationError
from qc.domain.models import SCHEMA_VERSION_REPORT, GenerationReport


def load_report(path: Path) -> GenerationReport:
    """Read a generation/validation report, refusing one from a newer QC."""
    if not path.is_file():
        raise NotFoundError(f"report not found: {path.name}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"corrupt report artifact {path.name}: {exc}") from exc
    check_supported("generation report", raw, SCHEMA_VERSION_REPORT)
    try:
        return GenerationReport.model_validate(raw)
    except Exception as exc:
        raise ValidationError(f"invalid generation report: {exc}") from exc
