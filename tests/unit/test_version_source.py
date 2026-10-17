"""The reported version must come from the distribution, never a literal.

It was a literal once, and drifted: 0.2.0 shipped reporting `0.1.0` from
`qc --version`, and stamped `0.1.0` into every artifact's `tool_version` — which
made the provenance *in the data* wrong, not just a string on a terminal.

Two guards: the package version equals `pyproject.toml`, and the domain models
derive their `tool_version` from the live value rather than a copy of it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import qc
from qc import TOOL_VERSION
from qc.domain.models import GenerationPlan, GenerationReport

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_package_version_matches_pyproject() -> None:
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]

    assert qc.__version__ == declared, (
        f"qc reports {qc.__version__} but pyproject declares {declared} "
        "(a stale install, or the version was pinned somewhere it can drift)"
    )
    assert declared == TOOL_VERSION


def test_domain_defaults_are_live_not_literals() -> None:
    for model in (GenerationPlan, GenerationReport):
        default = model.model_fields["tool_version"]
        assert default.default_factory is not None, (
            f"{model.__name__}.tool_version uses a literal default, which drifts "
            "silently the moment the version changes"
        )
        assert default.default_factory() == TOOL_VERSION
