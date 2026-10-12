"""Connector interfaces (SPEC section 6).

All connectors implement the same async, application-facing contract.
Source connectors are read-only; writing goes through
:class:`WritableConnector` with a separately configured destination.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from qc.domain.cancellation import CancellationToken
from qc.domain.enums import DatasetKind, Measurement
from qc.domain.models import (
    ConnectionTest,
    DatasetRef,
    DatasetSchema,
    EphemeralRow,
    SizeEstimate,
    SourceConfig,
)

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class ProfileQuery:
    """A bounded aggregate/metadata query issued by the profiler (SPEC 7.3).

    Connectors translate the structured request into provider SQL/API
    calls. ``sql`` is only for connector-internal use after validation;
    it is never sent from callers directly.
    """

    kind: str  # "row_count" | "null_count" | "distinct" | "min_max" | "top_k" | ...
    dataset: DatasetRef
    params: dict[str, Any] = field(default_factory=dict)
    estimated_rows: int = 0
    estimated_bytes: int = 0
    needs_full_scan: bool = False
    description: str = ""


@dataclass
class ProfileQueryResult:
    """Structured result of a profile query (never raw rows by default)."""

    kind: str
    data: dict[str, Any]
    rows_returned: int = 0
    bytes_returned: int = 0
    duration_seconds: float = 0.0
    truncated: bool = False
    estimated: bool = False
    note: str | None = None


@dataclass
class SampleRequest:
    """Bounded sampling request (SPEC 7.4)."""

    dataset: DatasetRef
    limit: int = 100
    columns: list[str] | None = None
    method: str = "stable_key"  # stable_key | random | stratified | predicate
    predicate: dict[str, Any] | None = None  # typed, safe predicate
    offset: int = 0
    seed: int = 0
    timeout_seconds: float = 30.0
    #: cooperative cancel signal checked between fetched batches (SPEC 6.1)
    cancel: CancellationToken | None = None


@dataclass
class ConnectorCapabilities:
    """What a connector can do; planner skips probes it cannot run (SPEC 6.1)."""

    metadata: bool = True
    aggregates: bool = True
    sampling: bool = True
    cancellation: bool = True
    writes: bool = False
    transactions: bool = False
    partitions: bool = False
    tablesample: bool = False
    statistics: bool = False  # e.g. pg_stats
    cost_estimates: bool = False
    named: tuple[str, ...] = ()

    def supports(self, name: str) -> bool:
        return name in self.named or getattr(self, name, False)


@runtime_checkable
class DataConnector(Protocol):
    """Async application-facing connector contract (SPEC 6)."""

    name: str
    capabilities: ConnectorCapabilities

    async def test_connection(self, config: SourceConfig) -> ConnectionTest: ...

    async def list_datasets(self, config: SourceConfig) -> list[DatasetRef]: ...

    async def get_schema(self, config: SourceConfig, dataset: DatasetRef) -> DatasetSchema: ...

    async def estimate_size(self, config: SourceConfig, dataset: DatasetRef) -> SizeEstimate: ...

    async def execute_profile_query(
        self, config: SourceConfig, query: ProfileQuery
    ) -> ProfileQueryResult: ...

    def sample_rows(
        self, config: SourceConfig, request: SampleRequest
    ) -> AsyncIterator[EphemeralRow]: ...

    async def close(self) -> None: ...


@runtime_checkable
class WritableConnector(Protocol):
    """Approved destination writes only (SPEC 6, 10.3)."""

    async def write_rows(
        self,
        config: SourceConfig,
        table: str,
        columns: list[str],
        rows: list[dict[str, Any]],
        *,
        mode: str = "create",  # create | append | replace
    ) -> int: ...


class BaseConnector:
    """Convenience base with capability defaults and lifecycle bookkeeping."""

    name: str = "base"
    capabilities: ConnectorCapabilities = ConnectorCapabilities()

    def __init__(self) -> None:
        self._closed = False
        #: set by the profiler so inner fetches observe cancellation
        self.cancel: CancellationToken | None = None
        #: count of transient retries performed (SPEC 15 metrics)
        self.retry_count = 0

    async def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def require_capability(self, capability: str) -> None:
        from qc.domain.errors import UnsupportedError

        if not self.capabilities.supports(capability):
            raise UnsupportedError(
                f"connector '{self.name}' does not support {capability}",
                detail={"capability": capability, "connector": self.name},
            )


__all__ = [
    "BaseConnector",
    "ConnectorCapabilities",
    "DataConnector",
    "DatasetKind",
    "Measurement",
    "ProfileQuery",
    "ProfileQueryResult",
    "SampleRequest",
    "WritableConnector",
]
