"""Shared profiling test fixtures: edge-case JSONL dataset (SPEC 16.1)."""

import json
from pathlib import Path

import pytest


@pytest.fixture
def edge_rows() -> list[dict]:
    """Rows covering the null/falsy/type edge cases from SPEC 7.5."""
    return [
        # id: zero-padded identifier, unique
        {
            "id": "00123",
            "email": "alice@example.com",
            "age": 34,
            "score": 0.5,
            "active": True,
            "note": "hello world",
            "country": "US",
            "signup": "2025-01-05",
            "balance": "10.50",
            "ref_code": "A1",
            "phone": "+1 555 123 4567",
        },
        {
            "id": "00124",
            "email": "bob@example.com",
            "age": 0,
            "score": 0.0,
            "active": False,
            "note": "",
            "country": "US",
            "signup": "2025-02-10",
            "balance": "0.00",
            "ref_code": "A2",
            "phone": "+1 555 987 6543",
        },
        {
            "id": "00125",
            "email": "carol@example.com",
            "age": None,
            "score": None,
            "active": False,
            "note": "   ",
            "country": "CA",
            "signup": "2025-03-15",
            "balance": "-1",
            "ref_code": "A1",
            "phone": "+1 555 111 2222",
        },
        {
            "id": "00126",
            "email": "dave@example.com",
            "age": 29,
            "score": 0.9,
            "active": True,
            "note": "some longer free text here with many words",
            "country": "US",
            "signup": "2025-04-01",
            "balance": "99.99",
            "ref_code": "B1",
            "phone": "+44 20 7946 0958",
        },
        {
            "id": "00127",
            "email": "erin@example.com",
            "age": None,
            "score": 0.1,
            "active": True,
            "note": "null",
            "country": "GB",
            "signup": "2025-05-20",
            "balance": "1234.56",
            "ref_code": "B1",
            "phone": "+1 555 333 4444",
        },
        {
            "id": "00128",
            "email": "frank@example.com",
            "age": 41,
            "score": 0.75,
            "active": False,
            "note": "another note",
            "country": "US",
            "signup": "2025-06-30",
            "balance": "-9999",
            "ref_code": "C1",
            "phone": "+1 555 654 3210",
        },
        {
            "id": "00129",
            "email": "grace@example.com",
            "age": None,
            "score": None,
            "active": True,
            "note": "N/A",
            "country": "CA",
            "signup": "2025-07-04",
            "balance": "77.77",
            "ref_code": "C1",
            "phone": "+1 555 000 1111",
        },
        {
            "id": "00130",
            "email": "heidi@example.com",
            "age": 0,
            "score": 0.25,
            "active": False,
            "note": "free text again here okay",
            "country": "GB",
            "signup": "2025-08-15",
            "balance": "0.01",
            "ref_code": "A2",
            "phone": "+1 555 222 3333",
        },
    ]


@pytest.fixture
def fixture_root(tmp_path: Path, edge_rows: list[dict]) -> Path:
    root = tmp_path / "fixtures"
    root.mkdir()
    p = root / "people.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in edge_rows), encoding="utf-8")
    # null-heavy sparse dataset
    sparse = root / "sparse.jsonl"
    sparse.write_text(
        "\n".join(json.dumps({"a": None, "b": None, "c": "x"}) for _ in range(5)),
        encoding="utf-8",
    )
    # empty dataset
    (root / "empty.jsonl").write_text("", encoding="utf-8")
    return root
