"""``python -m qc.mcp`` entry point: serve the MCP server on stdio.

Reads project config from the working directory (qc.toml). HTTP mode is
started with ``QC_MCP_TRANSPORT=http`` and requires explicit config +
auth (SPEC 12.1).
"""

from __future__ import annotations

import os
import sys

from qc.application.runtime import Runtime
from qc.domain.errors import QCError
from qc.mcp.server import run_http, run_stdio
from qc.observability.logging import setup_logging
from qc.security.redaction import safe_error_message


def main() -> None:
    transport = os.environ.get("QC_MCP_TRANSPORT", "stdio").lower()
    setup_logging(level="warning", json_output=True)
    try:
        runtime = Runtime()  # cwd-based project
        if transport == "http":
            run_http(runtime)
        else:
            run_stdio(runtime)
    except QCError as exc:
        print(f"error [{exc.code.value}]: {exc.message}", file=sys.stderr)
        raise SystemExit(int(exc.exit_code)) from None
    except KeyboardInterrupt:
        raise SystemExit(0) from None
    except Exception as exc:
        print(f"error: {safe_error_message(exc)}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
