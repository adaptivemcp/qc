"""Python-side bridge client: subprocess lifecycle, deadlines, caps,
cancellation, and structured errors (SPEC 4.3, ADR-006).

The sidecar is a trusted local process: stdout carries protocol messages
only, diagnostics stream to stderr, secrets arrive via the inherited
environment, and argv never contains credentials.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qc.domain.errors import SourceError, UnsupportedError
from qc.jdbc_bridge.protocol import (
    OP_CONNECT,
    OP_METADATA_COLUMNS,
    OP_METADATA_DATASETS,
    OP_PING,
    OP_SHUTDOWN,
    BridgeProtocolError,
    BridgeRequest,
    BridgeResponse,
    build_execute,
    validate_request_payload,
)
from qc.observability.logging import get_logger
from qc.security.redaction import safe_error_message

log = get_logger("jdbc_bridge")

DEFAULT_MAX_ROWS = 10_000
DEFAULT_MAX_BYTES = 16 * 1024 * 1024


@dataclass
class BridgeConfig:
    """How to launch the sidecar."""

    java_bin: str = "java"
    jar_path: Path | None = None  # pinned, checksum-verified sidecar jar
    main_class: str = "qc.jdbc.Main"
    classpath: Path | None = None  # for dev: compiled classes dir
    extra_jvm_args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    startup_timeout: float = 15.0


class JdbcBridge:
    """Manages one sidecar subprocess and issues request/response calls."""

    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.proc: asyncio.subprocess.Process | None = None
        self._pending: dict[str, asyncio.Future[BridgeResponse]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._seq = 0
        self._stderr_tail: list[str] = []

    # ------------------------------------------------------------ lifecycle

    async def start(self, env_overrides: dict[str, str] | None = None) -> None:
        """Launch the sidecar; argv carries no secrets (SPEC 4.3)."""
        argv = self._argv()
        if shutil.which(self.config.java_bin) is None and self.config.classpath is None:
            raise UnsupportedError(
                "java runtime not found; install Java 17+ or use a non-MaxCompute connector",
                detail={"binary": self.config.java_bin},
            )
        env = dict(os.environ)
        env.update(self.config.env)
        if env_overrides:
            env.update(env_overrides)  # secrets go here, not argv
        env["QC_BRIDGE_PROTOCOL"] = "1"
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=8 * 1024 * 1024,
            )
        except OSError as exc:
            raise SourceError(f"failed to launch JDBC sidecar: {safe_error_message(exc)}") from exc
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        await self._wait_ready()

    def _argv(self) -> list[str]:
        argv = [self.config.java_bin]
        argv.extend(self.config.extra_jvm_args)
        if self.config.jar_path is not None:
            argv += ["-cp", str(self.config.jar_path), self.config.main_class]
        elif self.config.classpath is not None:
            argv += ["-cp", str(self.config.classpath), self.config.main_class]
        else:
            raise UnsupportedError(
                "JDBC sidecar jar/classpath not configured",
                detail={"hint": "set qc jdbc_bridge.jar_path"},
            )
        return argv

    async def _wait_ready(self) -> None:
        """Ping until the sidecar is up or fail with stderr diagnostics."""
        deadline = time.monotonic() + self.config.startup_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                resp = await self.call(OP_PING, {}, timeout=2.0)
                if resp.ok:
                    return
            except Exception as exc:  # not up yet
                last_error = exc
                await asyncio.sleep(0.15)
        detail = "; ".join(self._stderr_tail[-3:])
        raise SourceError(
            "JDBC sidecar did not become ready" + (f": {detail}" if detail else ""),
            detail={"startup": safe_error_message(last_error) if last_error else "timeout"},
        )

    async def close(self) -> None:
        if self.proc is None:
            return
        with contextlib.suppress(Exception):
            await self.call(OP_SHUTDOWN, {}, timeout=2.0)
        if self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=3.0)
            except (TimeoutError, ProcessLookupError):
                try:
                    self.proc.kill()
                    await self.proc.wait()
                except ProcessLookupError:
                    pass
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
        self.proc = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    # --------------------------------------------------------------- calls

    async def call(
        self, op: str, payload: dict[str, Any], *, timeout: float = 30.0
    ) -> BridgeResponse:
        if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
            raise SourceError("JDBC sidecar is not running")
        self._seq += 1
        request_id = f"req_{self._seq}_{os.getpid()}"
        validate_request_payload(op, payload)
        request = BridgeRequest(
            op=op,
            request_id=request_id,
            payload=payload,
            deadline_ms=int(timeout * 1000),
        )
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[BridgeResponse] = loop.create_future()
        self._pending[request_id] = fut
        try:
            self.proc.stdin.write((request.encode() + "\n").encode("utf-8"))
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(request_id, None)
            raise SourceError("JDBC sidecar pipe closed unexpectedly") from exc
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            # tell the sidecar to cancel the job, best effort
            await self._send_cancel(request_id)
            raise SourceError(
                f"bridge request timed out after {timeout:.0f}s",
                detail={"op": op},
            ) from exc

    async def _send_cancel(self, request_id: str) -> None:
        if self.proc is None or self.proc.stdin is None:
            return
        try:
            cancel = BridgeRequest(
                op="job.cancel",
                request_id=f"{request_id}_cancel",
                payload={"target_request_id": request_id},
                deadline_ms=2000,
            )
            self.proc.stdin.write((cancel.encode() + "\n").encode("utf-8"))
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass

    # ----------------------------------------------------------- high-level

    async def ping(self) -> bool:
        resp = await self.call(OP_PING, {}, timeout=3.0)
        return resp.ok

    async def connect(
        self, *, driver: str, jdbc_url_env: str, options: dict[str, str] | None = None
    ) -> None:
        """Connect; the JDBC URL/credentials are *environment references*.

        ``jdbc_url_env`` names an environment variable holding the JDBC
        URL — the URL itself never appears in argv or protocol payloads.
        """
        payload: dict[str, Any] = {
            "jdbc_url_env": jdbc_url_env,
            "driver": driver,
        }
        if options:
            payload["options"] = options
        resp = await self.call(OP_CONNECT, payload, timeout=20.0)
        if not resp.ok:
            raise SourceError(_err_message(resp), detail=_err_detail(resp))

    async def list_datasets(self, schema: str | None = None) -> list[dict[str, Any]]:
        resp = await self.call(OP_METADATA_DATASETS, {"schema": schema}, timeout=15.0)
        if not resp.ok:
            raise SourceError(_err_message(resp), detail=_err_detail(resp))
        datasets = resp.payload.get("datasets", [])
        return [d for d in datasets if isinstance(d, dict)]

    async def list_columns(self, namespace: str, table: str) -> list[dict[str, Any]]:
        resp = await self.call(
            OP_METADATA_COLUMNS, {"schema": namespace, "table": table}, timeout=15.0
        )
        if not resp.ok:
            raise SourceError(_err_message(resp), detail=_err_detail(resp))
        columns = resp.payload.get("columns", [])
        return [c for c in columns if isinstance(c, dict)]

    async def execute(
        self,
        sql: str,
        params: list[Any] | None = None,
        *,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout: float = 30.0,
    ) -> tuple[list[str], list[list[Any]], bool]:
        """Execute a bounded SELECT; returns (columns, rows, truncated)."""
        request = build_execute(
            f"req_{self._seq + 1}",
            sql=sql,
            params=params or [],
            max_rows=min(max_rows, DEFAULT_MAX_ROWS),
            max_bytes=min(max_bytes, DEFAULT_MAX_BYTES),
            deadline_ms=int(timeout * 1000),
        )
        resp = await self.call(request.op, request.payload, timeout=timeout)
        if not resp.ok:
            raise SourceError(_err_message(resp), detail=_err_detail(resp))
        columns = [str(c) for c in resp.payload.get("columns", [])]
        rows = resp.payload.get("rows", [])
        if not isinstance(rows, list):
            raise BridgeProtocolError("bridge rows must be a list")
        str_rows = [list(row) for row in rows if isinstance(row, list)]
        truncated = bool(resp.payload.get("truncated", False))
        return columns, str_rows, truncated

    # --------------------------------------------------------------- loops

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue  # keep-alive / stray newline: not protocol
                try:
                    resp = BridgeResponse.decode(text)
                except BridgeProtocolError as exc:
                    # stdout must carry protocol only: log and continue
                    log.warning("bad bridge line", fields={"detail": str(exc)[:120]})
                    continue
                fut = self._pending.pop(resp.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(resp)
        except asyncio.CancelledError:
            raise
        finally:
            # fail all in-flight requests when stdout closes
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(SourceError("JDBC sidecar exited"))
            self._pending.clear()

    async def _stderr_loop(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                # keep a bounded, redacted tail for diagnostics (SPEC 4.3)
                from qc.security.redaction import redact_text

                self._stderr_tail.append(redact_text(text)[:200])
                if len(self._stderr_tail) > 20:
                    self._stderr_tail.pop(0)
        except asyncio.CancelledError:
            raise


def _err_message(resp: BridgeResponse) -> str:
    if resp.error:
        return str(resp.error.get("message", "bridge error"))
    return "bridge request failed"


def _err_detail(resp: BridgeResponse) -> dict[str, str]:
    if not resp.error:
        return {}
    code = resp.error.get("code")
    detail = resp.error.get("detail") or {}
    out = {"bridge_code": str(code)} if code else {}
    if isinstance(detail, dict):
        out.update({str(k): str(v) for k, v in detail.items()})
    return out


def read_driver_lock(path: Path) -> dict[str, str]:
    """Read pinned JDBC driver version + checksum (SPEC 4.3).

    Format: JSON {"driver": name, "version": x, "sha256": hex}.
    The sidecar build verifies the jar against this before loading.
    """
    if not path.is_file():
        raise UnsupportedError(f"driver lock file missing: {path.name}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BridgeProtocolError(f"corrupt driver lock: {exc}") from exc
    for key in ("driver", "version", "sha256"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise BridgeProtocolError(f"driver lock missing '{key}'")
    digest = raw["sha256"]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
        raise BridgeProtocolError("driver lock sha256 must be 64 hex chars")
    return {k: str(raw[k]) for k in ("driver", "version", "sha256")}


def verify_driver_jar(jar: Path, expected_sha256: str) -> bool:
    """Checksum-verify the driver jar before it can be loaded."""
    import hashlib

    if not jar.is_file():
        return False
    h = hashlib.sha256()
    with jar.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower() == expected_sha256.lower()
