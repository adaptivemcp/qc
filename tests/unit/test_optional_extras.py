"""Optional integrations must be installable, and imported lazily.

qc imports its optional integrations inside the code path that needs them and
reports a helpful error when the import fails — but that only helps if the name
it suggests actually resolves. `pip install 'querycleaner[keyring]'` has to
exist, which means one `[project.optional-dependencies]` entry per optional
module. These tests keep the imports, the extras and the error messages in step.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# module name -> the extra that installs it
OPTIONAL_MODULES = {
    "psycopg": "postgres",
    "mysql": "mysql",
    "pyarrow": "parquet",
    "keyring": "keyring",
}

# module name -> the distribution that provides it (most are the same)
DISTRIBUTION = {"mysql": "mysql-connector-python"}


def _extras() -> dict[str, list[str]]:
    doc = tomllib.loads((REPO / "pyproject.toml").read_text())
    return doc["project"]["optional-dependencies"]


def test_every_optional_module_has_an_extra() -> None:
    extras = _extras()
    missing = sorted(f"{m} -> [{e}]" for m, e in OPTIONAL_MODULES.items() if e not in extras)
    assert not missing, "lazily-imported modules with no extra: " + ", ".join(missing)


def test_each_extra_installs_its_module() -> None:
    extras = _extras()
    for module, extra in OPTIONAL_MODULES.items():
        distribution = DISTRIBUTION.get(module, module)
        assert any(
            entry.split(">=")[0].split("<")[0].split("[")[0].strip() == distribution
            for entry in extras[extra]
        ), f"[{extra}] does not install {distribution}: {extras[extra]}"


def test_all_pulls_every_extra() -> None:
    all_of = " ".join(_extras()["all"])
    for extra in sorted(set(OPTIONAL_MODULES.values())):
        assert f"querycleaner[{extra}]" in all_of, f"all does not include [{extra}]"


def test_optional_imports_stay_lazy() -> None:
    """An optional module must not be imported at module scope, or `qc` breaks
    for everyone who did not install its extra."""
    offenders: list[str] = []
    for path in (REPO / "src").rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if not line.startswith(("import ", "from ")):  # indented = inside a function
                continue
            module = line.split()[1].split(".")[0]
            if module in OPTIONAL_MODULES:
                offenders.append(f"{path.relative_to(REPO)}:{lineno} imports {module}")
    assert not offenders, "optional imports at module scope:\n" + "\n".join(offenders)
