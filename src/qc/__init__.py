"""QueryCleaner/QC - local-first data profiling, sanitization, and synthetic data."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

try:
    # Single source of truth: the version declared in `pyproject.toml`, read from
    # the installed distribution. A literal here silently diverges from it — and
    # did: 0.2.0 shipped reporting 0.1.0 from `qc --version`, and stamped 0.1.0
    # into every artifact's `tool_version`, so the provenance in the data was
    # wrong too, not just the string.
    __version__ = _distribution_version("querycleaner")
except PackageNotFoundError:  # a source tree with no installed distribution
    __version__ = "0.0.0+unknown"

TOOL_VERSION = __version__
