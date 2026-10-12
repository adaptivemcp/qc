"""JDBC bridge: versioned stdio protocol + subprocess client (SPEC 4.3)."""

from qc.jdbc_bridge.client import BridgeConfig, JdbcBridge, read_driver_lock, verify_driver_jar
from qc.jdbc_bridge.protocol import (
    PROTOCOL_VERSION,
    BridgeProtocolError,
    BridgeRequest,
    BridgeResponse,
    build_execute,
)

__all__ = [
    "PROTOCOL_VERSION",
    "BridgeConfig",
    "BridgeProtocolError",
    "BridgeRequest",
    "BridgeResponse",
    "JdbcBridge",
    "build_execute",
    "read_driver_lock",
    "verify_driver_jar",
]
