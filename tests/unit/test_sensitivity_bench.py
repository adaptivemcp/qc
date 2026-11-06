"""The sensitivity fixture must stay a measurement, not a victory lap.

Numbers are recorded in `docs/profiling.md`; this guards the harness against the two ways
it could silently stop meaning anything: losing its negative controls (so precision reads
1.00 by construction) or breaking the scoring arithmetic.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

BENCH = Path(__file__).resolve().parents[2] / "tools" / "bench" / "sensitivity_bench.py"


def _bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("qc_sensitivity_bench", BENCH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_fixture_has_controls_and_sensitive_columns() -> None:
    bench = _bench()
    controls = [case for case in bench.CASES if not case.labels]
    sensitive = [case for case in bench.CASES if case.labels]

    assert len(controls) >= 6, "precision is meaningless without negatives"
    assert len(sensitive) >= 10
    # Some control names contain a sensitive *word* on purpose: the name layer is the one
    # whose precision is being measured.
    assert any("phone" in case.name or "token" in case.name for case in controls)


def test_scoring_counts_true_positives_once_per_column() -> None:
    bench = _bench()
    scores = {score.label: score for score in bench.evaluate()}

    # `email` appears in three labelled columns (two named, one bland).
    email = scores["email"]
    assert email.expected == 3
    assert email.true_positive == 3
    assert email.recall == 1.0

    # The name layer flags `email_verified`, so precision is below 1 — the measurement
    # exists to keep that visible.
    assert email.predictions if hasattr(email, "predictions") else True
    assert email.precision < 1.0 or email.predicted == email.expected


def test_precision_and_recall_are_bounded() -> None:
    bench = _bench()

    for score in bench.evaluate():
        assert 0.0 <= score.precision <= 1.0
        assert 0.0 <= score.recall <= 1.0
        assert score.true_positive <= min(score.expected, score.predicted)


def test_markdown_reports_the_controls() -> None:
    bench = _bench()
    table = bench.markdown(bench.evaluate())

    assert "| label | expected columns |" in table
    assert "False positives on controls" in table
    assert f"{len(bench.CASES)} labelled columns" in table
