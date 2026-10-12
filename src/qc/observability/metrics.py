"""Metrics collection (SPEC 15).

Lightweight, in-process counters/gauges/histograms emitted as structured
log events and exposed for diagnostics. Never records row values,
credentials, or raw query parameters — only counts, sizes, and timings.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from qc.observability.logging import get_logger

log = get_logger("metrics")


@dataclass
class OperationMetrics:
    """Per-operation tallies (SPEC 15 metric set)."""

    operation: str = ""
    queries: int = 0
    rows_scanned: int = 0
    rows_returned: int = 0
    bytes_processed: int = 0
    retries: int = 0
    cancellations: int = 0
    budget_rejections: int = 0
    rows_generated: int = 0
    validation_failures: int = 0
    # connector name -> healthy (bool)
    connector_health: dict[str, bool] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    _elapsed: float | None = None

    def stop(self) -> OperationMetrics:
        self._elapsed = time.monotonic() - self.started_at
        return self

    @property
    def elapsed_seconds(self) -> float:
        if self._elapsed is not None:
            return self._elapsed
        return time.monotonic() - self.started_at

    def budget_utilisation(self, budget: dict[str, float | None]) -> dict[str, float]:
        """Fraction of each budget dimension consumed (0..1+, None = unset)."""
        out: dict[str, float] = {}
        pairs = {
            "queries": (self.queries, budget.get("max_queries")),
            "rows": (self.rows_scanned, budget.get("max_rows")),
            "bytes": (self.bytes_processed, budget.get("max_bytes")),
            "runtime": (self.elapsed_seconds, budget.get("max_runtime_seconds")),
        }
        for name, (used, limit) in pairs.items():
            if limit is None or limit == 0:
                out[name] = 0.0 if used == 0 else 1.0
            else:
                out[name] = round(used / float(limit), 4)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "queries": self.queries,
            "rows_scanned": self.rows_scanned,
            "rows_returned": self.rows_returned,
            "bytes_processed": self.bytes_processed,
            "retries": self.retries,
            "cancellations": self.cancellations,
            "budget_rejections": self.budget_rejections,
            "rows_generated": self.rows_generated,
            "validation_failures": self.validation_failures,
            "connector_health": dict(self.connector_health),
            "elapsed_seconds": round(self.elapsed_seconds, 6),
        }


class MetricsRecorder:
    """Thread-safe registry of recent operation metrics."""

    def __init__(self, *, keep: int = 100) -> None:
        self._lock = threading.Lock()
        self._recent: list[OperationMetrics] = []
        self._keep = keep

    def start(self, operation: str) -> OperationMetrics:
        return OperationMetrics(operation=operation)

    def record(self, metrics: OperationMetrics) -> OperationMetrics:
        metrics.stop()
        with self._lock:
            self._recent.append(metrics)
            if len(self._recent) > self._keep:
                self._recent.pop(0)
        log.info(
            "operation metrics",
            fields={"event": "metrics", **metrics.as_dict()},
        )
        return metrics

    def recent(self) -> list[OperationMetrics]:
        with self._lock:
            return list(self._recent)

    def totals(self) -> dict[str, int]:
        with self._lock:
            acc = {
                "queries": 0,
                "rows_scanned": 0,
                "rows_returned": 0,
                "bytes_processed": 0,
                "retries": 0,
                "cancellations": 0,
                "budget_rejections": 0,
                "rows_generated": 0,
                "validation_failures": 0,
            }
            for m in self._recent:
                for k in acc:
                    acc[k] += getattr(m, k)
            return acc


#: Process-wide recorder for CLI/MCP diagnostics.
RECORDER = MetricsRecorder()


__all__ = ["RECORDER", "MetricsRecorder", "OperationMetrics"]
