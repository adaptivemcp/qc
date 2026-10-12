"""Shared CLI presentation: JSON/text output, exit-code mapping (SPEC 11)."""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, NoReturn

import typer

from qc.domain.errors import ExitCode, QCError
from qc.observability.logging import setup_logging
from qc.security.redaction import safe_error_message


@dataclass
class OutputOptions:
    as_json: bool = False
    quiet: bool = False
    verbose: bool = False
    format: str = "text"

    def apply(self) -> None:
        level = "debug" if self.verbose else ("warning" if self.quiet else "info")
        setup_logging(level=level)


def emit(payload: Any, *, opts: OutputOptions, text: str | None = None) -> None:
    """Print machine-readable JSON or human text per output mode."""
    if opts.as_json:
        data = _jsonable(payload)
        typer.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    elif text is not None and not opts.quiet:
        typer.echo(text)


def _jsonable(payload: Any) -> Any:
    if hasattr(payload, "json_safe"):
        return payload.json_safe()
    if isinstance(payload, list):
        return [_jsonable(p) for p in payload]
    if isinstance(payload, dict):
        return {k: _jsonable(v) for k, v in payload.items()}
    return payload


def handle_errors[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """Wrap a command: map QCError to documented exit codes (SPEC 11.3).

    Uses ``functools.wraps`` so Typer inspects the *original* signature
    (the wrapper's own annotations are not evaluation-safe under
    ``from __future__ import annotations``).
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> R:
        try:
            return fn(*args, **kwargs)
        except QCError as exc:
            _fail(exc)
            raise AssertionError("unreachable") from exc
        except typer.Exit:
            raise
        except Exception as exc:
            opts = kwargs.get("opts")
            as_json = bool(kwargs.get("json_output")) or (
                isinstance(opts, OutputOptions) and opts.as_json
            )
            if as_json:
                typer.echo(
                    json.dumps({"error": "INTERNAL", "message": safe_error_message(exc)}),
                    err=True,
                )
            else:
                typer.echo(f"error: {safe_error_message(exc)}", err=True)
            raise typer.Exit(code=int(ExitCode.GENERAL_FAILURE)) from None

    return wrapper


def _fail(exc: QCError) -> NoReturn:
    typer.echo(f"error [{exc.code.value}]: {exc.message}", err=True)
    for k, v in exc.detail.items():
        typer.echo(f"  {k}: {v}", err=True)
    raise typer.Exit(code=int(exc.exit_code)) from None


def arun[R](coro: Coroutine[Any, Any, R]) -> R:
    """Run an async use case from sync CLI code."""
    result: R = asyncio.run(coro)
    return result


__all__ = [
    "OutputOptions",
    "arun",
    "emit",
    "handle_errors",
]
