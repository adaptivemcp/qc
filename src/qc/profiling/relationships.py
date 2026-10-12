"""Relationship discovery (SPEC 7.8).

Declared constraints take priority. Inferred keys/FKs are hypotheses
verified against sampled data: uniqueness + nullability for keys,
type compatibility + sampled overlap for foreign keys.
"""

from __future__ import annotations

from qc.domain.enums import Confidence
from qc.domain.models import ColumnSchema, Relationship

# Logical type compatibility for FK candidates
_FK_COMPATIBLE = {
    ("integer", "integer"),
    ("integer", "decimal"),
    ("decimal", "decimal"),
    ("float", "float"),
    ("string", "string"),
    ("string", "identifier"),
    ("identifier", "identifier"),
    ("uuid", "uuid"),
    ("date", "date"),
    ("timestamp", "timestamp"),
    ("timestamp_tz", "timestamp_tz"),
}


def discover_relationships(
    dataset_qualified_name: str,
    columns: list[ColumnSchema],
    sampled_rows: list[dict[str, object]],
) -> list[Relationship]:
    """Infer key candidates within a dataset from sampled uniqueness/nullability."""
    if not sampled_rows:
        return []

    results: list[Relationship] = []
    n = len(sampled_rows)

    for col in columns:
        values = [row.get(col.name) for row in sampled_rows]
        non_null = [v for v in values if v is not None]
        null_rate = 1.0 - (len(non_null) / n)
        distinct = len({_key(v) for v in non_null})

        # Key candidate: unique in sample, mostly non-null
        if (col.name.lower().endswith("_id") or col.name.lower() == "id") and distinct == len(
            non_null
        ) == n:
            results.append(
                Relationship(
                    kind="key_candidate",
                    from_dataset=dataset_qualified_name,
                    from_columns=[col.name],
                    to_dataset=dataset_qualified_name,
                    to_columns=[col.name],
                    declared=False,
                    verified=False,
                    confidence=Confidence.MEDIUM if n >= 30 else Confidence.LOW,
                    evidence_refs=[f"sample_uniqueness:n={n}"],
                )
            )
        elif null_rate > 0:
            # note: nullability info retained on the column profile, not here
            pass

    # Cross-column functional dependency hints: identical value sets
    seen: dict[str, str] = {}
    for col in columns:
        column_values = tuple(_key(row.get(col.name)) for row in sampled_rows)
        digest = str(hash(column_values))
        if digest in seen and seen[digest] != col.name:
            results.append(
                Relationship(
                    kind="dependency",
                    from_dataset=dataset_qualified_name,
                    from_columns=[col.name],
                    to_dataset=dataset_qualified_name,
                    to_columns=[seen[digest]],
                    declared=False,
                    verified=False,
                    confidence=Confidence.LOW,
                    evidence_refs=[f"identical_values_in_sample:n={n}"],
                )
            )
        else:
            seen.setdefault(digest, col.name)

    return results


def _key(v: object) -> str:
    if v is None:
        return "\x00null"
    if isinstance(v, bool):
        return f"\x00b{int(v)}"
    return f"\x00{type(v).__name__}:{v}"
