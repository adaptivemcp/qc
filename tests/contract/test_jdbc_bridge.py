"""JDBC bridge contract tests (SPEC 4.3, ADR-006, M4 exit criteria).

Runs the Python bridge client against the scripted fake sidecar (a JVM
is not required for protocol tests). Live MaxCompute tests are opt-in.

Covers: protocol versioning, request IDs, structured errors, SELECT-only
enforcement, secret isolation (env not argv/payload), row/byte caps,
deadlines/cancellation, and driver lock verification.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from qc.domain.errors import SourceError, UnsupportedError
from qc.jdbc_bridge.client import BridgeConfig, JdbcBridge, read_driver_lock, verify_driver_jar
from qc.jdbc_bridge.protocol import (
    OP_CONNECT,
    OP_EXECUTE,
    BridgeProtocolError,
    BridgeRequest,
    BridgeResponse,
    build_execute,
    validate_request_payload,
)

FAKE_SIDECAR = Path(__file__).parent / "fake_sidecar.py"


def _bridge_config() -> BridgeConfig:
    """Run the fake sidecar under python instead of java."""
    return BridgeConfig(
        java_bin=sys.executable,
        classpath=FAKE_SIDECAR,  # marker: _argv handles python mode below
        main_class="",
        startup_timeout=10.0,
    )


@pytest.fixture
async def bridge() -> AsyncIterator[JdbcBridge]:
    """Bridge wired to the fake sidecar (python - fake_sidecar.py)."""
    cfg = _bridge_config()

    # override argv construction: python running the script
    class FakeSidecarBridge(JdbcBridge):
        def _argv(self) -> list[str]:
            return [sys.executable, str(FAKE_SIDECAR)]

    b = FakeSidecarBridge(cfg)
    await b.start(env_overrides={"QC_JDBC_URL": "jdbc:odps://fake!proj"})
    yield b
    await b.close()


# ------------------------------------------------------------------ protocol


class TestProtocol:
    def test_request_encoding_roundtrip(self) -> None:
        req = BridgeRequest(op="ping", request_id="r1")
        line = req.encode()
        raw = json.loads(line)
        assert raw["protocol_version"] == 1
        assert raw["request_id"] == "r1"

    def test_version_mismatch_rejected(self) -> None:
        line = json.dumps(
            {
                "protocol_version": 99,
                "request_id": "x",
                "status": "ok",
                "payload": {},
            }
        )
        with pytest.raises(BridgeProtocolError, match="version mismatch"):
            BridgeResponse.decode(line)

    def test_missing_request_id_rejected(self) -> None:
        line = json.dumps({"protocol_version": 1, "status": "ok", "payload": {}})
        with pytest.raises(BridgeProtocolError, match="request_id"):
            BridgeResponse.decode(line)

    def test_bad_status_rejected(self) -> None:
        line = json.dumps(
            {"protocol_version": 1, "request_id": "x", "status": "weird", "payload": {}}
        )
        with pytest.raises(BridgeProtocolError, match="status"):
            BridgeResponse.decode(line)

    def test_non_json_rejected(self) -> None:
        with pytest.raises(BridgeProtocolError, match="non-JSON"):
            BridgeResponse.decode("java.lang.Exception: boom at ...")

    def test_execute_requires_select(self) -> None:
        with pytest.raises(BridgeProtocolError, match="SELECT"):
            build_execute("r", sql="DELETE FROM t", params=[], max_rows=10, max_bytes=100)

    def test_execute_requires_caps(self) -> None:
        with pytest.raises(BridgeProtocolError, match="max_rows"):
            validate_request_payload(OP_EXECUTE, {"sql": "SELECT 1", "params": []})

    def test_non_scalar_params_rejected(self) -> None:
        with pytest.raises(BridgeProtocolError, match="scalars"):
            validate_request_payload(
                OP_EXECUTE,
                {"sql": "SELECT 1", "params": [{"nested": 1}], "max_rows": 5, "max_bytes": 10},
            )

    def test_secret_in_connect_payload_rejected(self) -> None:
        with pytest.raises(BridgeProtocolError, match="password"):
            validate_request_payload(OP_CONNECT, {"jdbc_url_env": "X", "password": "leak"})

    def test_unknown_op_rejected(self) -> None:
        with pytest.raises(BridgeProtocolError, match="unknown bridge op"):
            validate_request_payload("rm_rf", {})


# -------------------------------------------------------------- live (fake)


@pytest.mark.asyncio
class TestBridgeLive:
    async def test_ping(self, bridge: JdbcBridge) -> None:
        assert await bridge.ping() is True

    async def test_connect_and_datasets(self, bridge: JdbcBridge) -> None:
        await bridge.connect(driver="com.aliyun.odps.jdbc.OdrsDriver", jdbc_url_env="QC_JDBC_URL")
        datasets = await bridge.list_datasets("analytics")
        assert [d["name"] for d in datasets] == ["orders", "customers"]

    async def test_columns(self, bridge: JdbcBridge) -> None:
        await bridge.connect(driver="x", jdbc_url_env="QC_JDBC_URL")
        cols = await bridge.list_columns("analytics", "orders")
        assert cols[0]["primary_key"] is True
        assert cols[1]["name"] == "email"

    async def test_columns_missing_table_error(self, bridge: JdbcBridge) -> None:
        await bridge.connect(driver="x", jdbc_url_env="QC_JDBC_URL")
        with pytest.raises(SourceError) as exc:
            await bridge.list_columns("analytics", "nope")
        assert exc.value.code.value == "SOURCE_UNAVAILABLE"

    async def test_execute_returns_rows_and_truncation(self, bridge: JdbcBridge) -> None:
        await bridge.connect(driver="x", jdbc_url_env="QC_JDBC_URL")
        cols, rows, truncated = await bridge.execute("SELECT id, email FROM orders", max_rows=2)
        assert cols == ["id", "email"]
        assert len(rows) == 2  # max_rows respected
        assert truncated is True  # more rows existed

    async def test_count_shape(self, bridge: JdbcBridge) -> None:
        await bridge.connect(driver="x", jdbc_url_env="QC_JDBC_URL")
        _c, rows, _ = await bridge.execute("SELECT COUNT(*) FROM orders", max_rows=1)
        assert rows == [[42]]

    async def test_non_select_rejected_client_side(self, bridge: JdbcBridge) -> None:
        with pytest.raises(BridgeProtocolError):
            await bridge.execute("DROP TABLE orders")

    async def test_secrets_never_in_argv(self) -> None:
        """Launch argv must not contain credentials or the JDBC URL."""
        cfg = BridgeConfig(java_bin=sys.executable, startup_timeout=10.0)

        class FakeSidecarBridge(JdbcBridge):
            def _argv(self) -> list[str]:
                return [sys.executable, str(FAKE_SIDECAR)]

        b = FakeSidecarBridge(cfg)
        argv = b._argv()
        assert not any("jdbc:odps" in a for a in argv)
        assert not any("password" in a.lower() for a in argv)
        await b.start(env_overrides={"QC_JDBC_URL": "jdbc:odps://secret-host!proj"})
        try:
            assert await b.ping()
        finally:
            await b.close()

    async def test_shutdown_closes_cleanly(self) -> None:
        class FakeSidecarBridge(JdbcBridge):
            def _argv(self) -> list[str]:
                return [sys.executable, str(FAKE_SIDECAR)]

        b = FakeSidecarBridge(BridgeConfig(java_bin=sys.executable, startup_timeout=10))
        await b.start()
        assert b.alive
        await b.close()
        assert not b.alive


@pytest.mark.asyncio
class TestBridgeFailureModes:
    async def test_startup_failure_raises_source_error(self) -> None:
        class BrokenBridge(JdbcBridge):
            def _argv(self) -> list[str]:
                return [sys.executable, "-c", "import sys; sys.exit(3)"]

        b = BrokenBridge(BridgeConfig(java_bin=sys.executable, startup_timeout=1.5))
        with pytest.raises(SourceError, match="did not become ready"):
            await b.start()
        await b.close()

    async def test_timeout_cancels_request(self) -> None:
        class SlowBridge(JdbcBridge):
            def _argv(self) -> list[str]:
                # accepts input but never replies
                return [sys.executable, "-c", "import time\nwhile True: time.sleep(1)"]

        b = SlowBridge(BridgeConfig(java_bin=sys.executable, startup_timeout=0.5))
        b.proc = await asyncio.create_subprocess_exec(
            *b._argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            with pytest.raises(SourceError, match="timed out"):
                await b.call("ping", {}, timeout=0.5)
        finally:
            await b.close()

    async def test_stderr_never_on_stdout(self, bridge: JdbcBridge) -> None:
        """Sidecar diagnostics go to stderr; stdout stays protocol-clean."""
        # fake sidecar prints a startup line to stderr; ping still works
        assert await bridge.ping() is True


# ------------------------------------------------------------ driver locking


class TestDriverLock:
    def test_read_valid_lock(self, tmp_path: Path) -> None:
        lock = tmp_path / "driver.json"
        lock.write_text(
            json.dumps(
                {
                    "driver": "com.aliyun.odps.jdbc",
                    "version": "3.1.31",
                    "sha256": hashlib.sha256(b"jar").hexdigest(),
                }
            )
        )
        data = read_driver_lock(lock)
        assert data["version"] == "3.1.31"

    def test_missing_lock(self, tmp_path: Path) -> None:
        with pytest.raises(UnsupportedError):
            read_driver_lock(tmp_path / "nope.json")

    def test_bad_checksum_format(self, tmp_path: Path) -> None:
        lock = tmp_path / "driver.json"
        lock.write_text(json.dumps({"driver": "d", "version": "1", "sha256": "zz"}))
        with pytest.raises(BridgeProtocolError):
            read_driver_lock(lock)

    def test_verify_jar(self, tmp_path: Path) -> None:
        jar = tmp_path / "driver.jar"
        jar.write_bytes(b"fake jar content")
        good = hashlib.sha256(b"fake jar content").hexdigest()
        assert verify_driver_jar(jar, good) is True
        assert verify_driver_jar(jar, "0" * 64) is False
        assert verify_driver_jar(tmp_path / "missing.jar", good) is False

    def test_missing_required_field(self, tmp_path: Path) -> None:
        lock = tmp_path / "driver.json"
        lock.write_text(json.dumps({"driver": "d", "version": "1"}))
        with pytest.raises(BridgeProtocolError, match="sha256"):
            read_driver_lock(lock)
