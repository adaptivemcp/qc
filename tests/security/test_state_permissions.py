"""`state.db` holds job metadata and a profile index; it must not be world-readable.

`SECURITY.md` claims "state directory is private"; this is the assertion behind that
claim, cited by `docs/security-review.md`.
"""

from __future__ import annotations

from pathlib import Path

from qc.application.jobs import StateStore


def test_state_database_is_private(tmp_path: Path) -> None:
    path = tmp_path / "state.db"

    store = StateStore(path)
    store.close()

    assert oct(path.stat().st_mode)[-3:] == "600", (
        "state.db holds job history and profile paths; it is created 0600"
    )
