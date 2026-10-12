"""Fidelity validation (SPEC 9.5, 9.6).

Hard constraints (types, uniqueness, referential integrity, formats)
are distinct from soft distribution tolerances. Reports never include
raw source values.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from qc.domain.enums import LogicalType, Measurement
from qc.domain.models import ColumnProfile, FidelityMetric, ProfileArtifact

DEFAULT_TOLERANCES: dict[str, float] = {
    "null_rate": 0.05,
    "numeric_quantile": 0.10,
    "categorical_tvd": 0.15,
    "length_distance": 0.15,
    "format_validity": 0.95,
    "uniqueness_rate": 0.01,
    "referential_integrity": 0.0,
}


def _null_rate(rows: list[dict[str, Any]], column: str) -> float | None:
    if not rows:
        return None
    return sum(1 for r in rows if r.get(column) is None) / len(rows)


def _total_variation(p: dict[str, float], q: dict[str, float]) -> float:
    keys = set(p) | set(q)
    return 0.5 * sum(abs(p.get(k, 0.0) - q.get(k, 0.0)) for k in keys)


def validate_generation(
    rows: list[dict[str, Any]],
    artifact: ProfileArtifact,
    *,
    tolerances: dict[str, float] | None = None,
) -> list[FidelityMetric]:
    """Compare generated rows against a profile; returns metrics.

    Every metric records observed vs generated, its distance, the
    tolerance, pass/fail, and whether it is a hard constraint.
    """
    tol = {**DEFAULT_TOLERANCES, **(tolerances or {})}
    metrics: list[FidelityMetric] = []

    if not rows:
        metrics.append(
            FidelityMetric(
                name="row_count",
                observed=float(artifact.row_count or 0),
                generated=0.0,
                distance=abs(float(artifact.row_count or 0)),
                tolerance=0.0,
                passed=False,
                hard=True,
            )
        )
        return metrics

    for col in artifact.columns:
        # ---- hard: type validity -------------------------------------------
        bad = sum(1 for r in rows if not _type_ok(r.get(col.name), col))
        type_rate = 1.0 - bad / len(rows)
        metrics.append(
            FidelityMetric(
                name=f"{col.name}:type_validity",
                logical_type=col.logical_type,
                observed=1.0,
                generated=round(type_rate, 4),
                distance=round(1.0 - type_rate, 4),
                tolerance=tol["format_validity"],
                passed=type_rate >= tol["format_validity"],
                hard=True,
            )
        )

        # ---- soft: null rate -----------------------------------------------
        obs_total = col.row_count_seen or 0
        obs_null_rate = (col.falsy.null_count or 0) / obs_total if obs_total else None
        gen_null_rate = _null_rate(rows, col.name)
        if obs_null_rate is not None and gen_null_rate is not None:
            drift = abs(obs_null_rate - gen_null_rate)
            metrics.append(
                FidelityMetric(
                    name=f"{col.name}:null_rate",
                    logical_type=col.logical_type,
                    observed=round(obs_null_rate, 4),
                    generated=round(gen_null_rate, 4),
                    distance=round(drift, 4),
                    tolerance=tol["null_rate"],
                    passed=drift <= tol["null_rate"],
                    hard=False,
                )
            )

        # ---- hard: uniqueness when required --------------------------------
        if col.unique is True and col.unique_measurement == Measurement.EXACT:
            values = [r.get(col.name) for r in rows if r.get(col.name) is not None]
            distinct = len(set(map(_key, values)))
            rate = distinct / len(values) if values else 1.0
            metrics.append(
                FidelityMetric(
                    name=f"{col.name}:uniqueness",
                    observed=1.0,
                    generated=round(rate, 4),
                    distance=round(1.0 - rate, 4),
                    tolerance=tol["uniqueness_rate"],
                    passed=rate >= 1.0 - tol["uniqueness_rate"],
                    hard=True,
                )
            )

        # ---- soft: numeric range -------------------------------------------
        if col.numeric and col.numeric.minimum is not None:
            obs_lo = _f(col.numeric.minimum)
            obs_hi = _f(col.numeric.maximum)
            nums = [
                fval
                for r in rows
                if isinstance(r.get(col.name), (int, float))
                and not isinstance(r.get(col.name), bool)
                for fval in [_f(r.get(col.name))]
                if fval is not None
            ]
            if nums and obs_lo is not None and obs_hi is not None:
                below = sum(1 for v in nums if v < obs_lo)
                above = sum(1 for v in nums if v > obs_hi)
                out_rate = (below + above) / len(nums)
                metrics.append(
                    FidelityMetric(
                        name=f"{col.name}:range",
                        logical_type=col.logical_type,
                        observed=0.0,
                        generated=round(out_rate, 4),
                        distance=round(out_rate, 4),
                        tolerance=tol["numeric_quantile"],
                        passed=out_rate <= tol["numeric_quantile"],
                        hard=False,
                    )
                )

        # ---- soft: categorical TVD (rare categories grouped) ---------------
        # Numeric/temporal columns are covered by range checks; applying a
        # categorical distance to them would just measure sampling noise.
        _categorical_types = {
            LogicalType.STRING,
            LogicalType.CATEGORICAL_STRING,
            LogicalType.BOOLEAN,
            LogicalType.COUNTRY_CODE,
            LogicalType.POSTAL_CODE,
        }
        if (
            col.top_categories
            and col.distinct_count is not None
            and col.logical_type in _categorical_types
        ):
            obs_counts = Counter({c.display or c.value_key: c.count for c in col.top_categories})
            obs_total_cat = sum(obs_counts.values()) or 1
            obs_dist = {k: v / obs_total_cat for k, v in obs_counts.items()}
            gen_counts = Counter(_falsy_key(r.get(col.name)) for r in rows)
            gen_total = sum(gen_counts.values()) or 1
            gen_dist = {k: v / gen_total for k, v in gen_counts.items()}
            # only compare overlapping keys + an OTHER bucket
            obs_keys = set(obs_dist)
            gen_keys = set(gen_dist)
            shared = obs_keys & gen_keys
            obs_shared = {k: obs_dist[k] for k in shared}
            gen_shared = {k: gen_dist.get(k, 0.0) for k in shared}
            obs_other = 1.0 - sum(obs_shared.values())
            gen_other = 1.0 - sum(gen_shared.values())
            obs_shared["__other__"] = obs_other
            gen_shared["__other__"] = gen_other
            tvd = _total_variation(obs_shared, gen_shared)
            metrics.append(
                FidelityMetric(
                    name=f"{col.name}:categorical_tvd",
                    logical_type=col.logical_type,
                    observed=0.0,
                    generated=round(tvd, 4),
                    distance=round(tvd, 4),
                    tolerance=tol["categorical_tvd"],
                    passed=tvd <= tol["categorical_tvd"],
                    hard=False,
                )
            )
            del gen_keys

    return metrics


def _type_ok(value: Any, col: ColumnProfile) -> bool:
    """Type-validity check honoring nulls and falsy variants."""
    if value is None:
        return (col.falsy.null_count or 0) > 0 or col.nullable_declared is not False
    lt = col.logical_type
    if lt == LogicalType.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if lt in (LogicalType.DECIMAL, LogicalType.FLOAT):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if lt == LogicalType.BOOLEAN:
        return isinstance(value, bool)
    if lt in (LogicalType.JSON,):
        return isinstance(value, (dict, list))
    return isinstance(value, str)


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _key(v: Any) -> str:
    return f"{type(v).__name__}:{v}"


def _falsy_key(v: Any) -> str:
    """Preserve falsy distinctions in categorical comparison (SPEC 7.5)."""
    if v is None:
        return "__null__"
    if v is False:
        return "__false__"
    if v == 0:
        return "__zero__"
    if v == "":
        return "__empty__"
    return str(v)
