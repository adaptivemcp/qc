#!/usr/bin/env python3
"""Sensitivity-detector precision/recall on a small labelled corpus.

`docs/profiling.md` says detectors are name/format/entropy based and that their error rate
on real corpora is unknown. This is the smallest honest measurement of that: a curated set
of columns with a known correct label, run through the real `detect_sensitivity`, scored
per label.

    uv run python tools/bench/sensitivity_bench.py
    uv run python tools/bench/sensitivity_bench.py --json

It is a *fixture*, not a benchmark of the world. The columns are chosen to cover each
detector layer (name, format, entropy) plus deliberate negatives — free text, an id, a
quantity — because a corpus of only sensitive columns would report a perfect recall and
tell you nothing about precision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass

from qc.domain.enums import SensitivityLabel
from qc.security.sensitivity import detect_sensitivity


@dataclass(frozen=True)
class Case:
    """One column: its name, its values, and the labels a reviewer says are correct."""

    name: str
    values: list[str]
    labels: frozenset[SensitivityLabel]

    @property
    def note(self) -> str:
        return "sensitive" if self.labels else "control"


EMAILS = [f"user{i}@example.com" for i in range(12)]
PHONES = [f"+1 555 010 {i:04d}" for i in range(12)]
NAMES = ["Alice Johnson", "Bob Smith", "Carla Diaz", "Dan Okafor", "Eve Martin"] * 2
ADDRESSES = [f"{i} Market Street" for i in range(12)]
SSNS = [f"{100 + i}-{10 + i:02d}-{1000 + i}" for i in range(12)]
CARDS = ["4111111111111111", "4242424242424242", "5555555555554444"] * 4
TOKENS = [f"sk_live_{i:02d}abcdefghijklmnopqrstuvwx" for i in range(12)]
DATES = [f"19{70 + i:02d}-04-12" for i in range(12)]
IPS = [f"10.0.{i}.{i + 1}" for i in range(12)]

CASES: list[Case] = [
    # --- name layer
    Case("email", EMAILS, frozenset({SensitivityLabel.EMAIL})),
    Case("customer_email", EMAILS, frozenset({SensitivityLabel.EMAIL})),
    Case("phone_number", PHONES, frozenset({SensitivityLabel.PHONE})),
    Case("full_name", NAMES, frozenset({SensitivityLabel.NAME})),
    Case("street_address", ADDRESSES, frozenset({SensitivityLabel.ADDRESS})),
    Case(
        "postal_code",
        ["90210", "10001", "SW1A 1AA", "K1A 0B1"] * 3,
        frozenset({SensitivityLabel.ADDRESS}),
    ),
    Case("ssn", SSNS, frozenset({SensitivityLabel.GOVERNMENT_ID})),
    Case("credit_card", CARDS, frozenset({SensitivityLabel.FINANCIAL})),
    Case("api_key", TOKENS, frozenset({SensitivityLabel.CREDENTIAL})),
    Case("date_of_birth", DATES, frozenset({SensitivityLabel.DATE_OF_BIRTH})),
    Case("client_ip", IPS, frozenset({SensitivityLabel.IP_ADDRESS})),
    Case(
        "diagnosis",
        ["type 2 diabetes", "asthma", "hypertension"] * 4,
        frozenset({SensitivityLabel.HEALTH}),
    ),
    # --- value layer only: bland names, sensitive content
    Case("notes", EMAILS, frozenset({SensitivityLabel.EMAIL})),
    Case("reference", TOKENS, frozenset({SensitivityLabel.CREDENTIAL})),
    # --- controls: must not be flagged
    Case("order_id", [f"ord_{i:05d}" for i in range(12)], frozenset()),
    Case("quantity", [str(i) for i in range(12)], frozenset()),
    Case("status", ["open", "closed", "pending"] * 4, frozenset()),
    Case("country", ["US", "CA", "GB"] * 4, frozenset()),
    Case("amount", [f"{i}.50" for i in range(12)], frozenset()),
    Case("description", ["standard delivery", "express delivery", "pickup"] * 4, frozenset()),
    # --- adversarial controls: a sensitive *word* in the name, harmless values. The name
    # layer is the one that fires here, and that is the point of measuring precision.
    Case("phone_model", ["iPhone 12", "Pixel 7", "Galaxy S23"] * 4, frozenset()),
    Case("token_count", ["12", "7", "0"] * 4, frozenset()),
    Case("email_verified", ["true", "false"] * 6, frozenset()),
    # --- realistic formats the first fixture did not cover
    Case(
        "masked_email",
        ["u***@example.com", "j***@corp.io"] * 6,
        frozenset({SensitivityLabel.EMAIL}),
    ),
    Case(
        "contact_number",
        [f"+1415555{i:04d}" for i in range(12)],
        frozenset({SensitivityLabel.PHONE}),
    ),
    Case(
        "spaced_phone",
        ["+44 20 7946 0958", "+61 2 9876 5432", "+49 30 123456"] * 4,
        frozenset({SensitivityLabel.PHONE}),
    ),
    Case(
        "session_token",
        [
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        ]
        * 6,
        frozenset({SensitivityLabel.CREDENTIAL}),
    ),
    Case(
        "webhook_secret",
        [f"MZXW6YTBOI{i:02d}GEZDGNBVGY3TQOJQGEZDGNBV" for i in range(12)],
        frozenset({SensitivityLabel.CREDENTIAL}),
    ),
    Case(
        "notes_free_text",
        [
            "Customer called about order 4471 and left an email address, reachable at "
            "alice@example.com; would like a callback on +1 415 555 0100 after 5pm."
        ]
        * 6,
        frozenset({SensitivityLabel.FREE_TEXT, SensitivityLabel.EMAIL, SensitivityLabel.PHONE}),
    ),
    # --- near-miss controls: structured, but not personal data
    Case("sku", [f"SKU-A{i:03d}B{i:02d}" for i in range(12)], frozenset()),
    Case(
        "commit_sha",
        [hashlib.sha1(f"commit-{i}".encode()).hexdigest() for i in range(12)],
        frozenset(),
    ),
    Case(
        "request_id",
        [f"req-{i:08x}-{i * 7:04x}-4a1b-9c2d-{i:012x}" for i in range(12)],
        frozenset(),
    ),
    Case("recorded_at", [f"2025-05-{i + 1:02d}T12:00:00Z" for i in range(12)], frozenset()),
    # --- free text that looks busy but carries nothing personal: the noise guard for
    # embedded extraction. Long prose, digits that are not phone numbers.
    Case(
        "notes_order_only",
        [
            "Warehouse note: order 12345678 was repacked twice, the pallet label was damaged "
            "in transit, and the customer asked for the delivery window to be confirmed."
        ]
        * 6,
        frozenset({SensitivityLabel.FREE_TEXT}),
    ),
    Case(
        "notes_schedule",
        [
            "Maintenance window moved: the batch job now starts at 2025-05-01T12:00:00Z "
            "instead of 03:00 UTC, because the upstream export finishes later than expected."
        ]
        * 6,
        frozenset({SensitivityLabel.FREE_TEXT}),
    ),
    # --- unseparated numbers in prose: the sentence decides, not the digits. Same digit
    # count, four different meanings.
    Case(
        "notes_call_number",
        [
            "Customer asked us to call 4155550100 to confirm the delivery window, because the "
            "pallet label was damaged in transit and the driver needs a new time slot."
        ]
        * 6,
        frozenset({SensitivityLabel.FREE_TEXT, SensitivityLabel.PHONE}),
    ),
    Case(
        "notes_order_ref",
        [
            "Warehouse note: order 12345678 was repacked twice, the pallet label was damaged in "
            "transit, and the customer asked for the delivery window to be confirmed."
        ]
        * 6,
        frozenset({SensitivityLabel.FREE_TEXT}),
    ),
    Case(
        "notes_invoice_ref",
        [
            "Finance note: invoice 90123456 covers the repack and the replacement labels, and "
            "the credit note will follow once the audit is signed off by the controller."
        ]
        * 6,
        frozenset({SensitivityLabel.FREE_TEXT}),
    ),
    Case(
        "notes_compact_date",
        [
            "Maintenance note: as of 20250501 the batch job starts later because the upstream "
            "export finishes after the billing window closes each night in the region."
        ]
        * 6,
        frozenset({SensitivityLabel.FREE_TEXT}),
    ),
    Case(
        "notes_call_and_order",
        [
            "Customer asked us to call about order 4471 on 4155550100 before the window closes, "
            "because the pallet label was damaged and needs replacing before dispatch."
        ]
        * 6,
        frozenset({SensitivityLabel.FREE_TEXT, SensitivityLabel.PHONE}),
    ),
]


@dataclass
class Score:
    label: str
    expected: int
    predicted: int
    true_positive: int

    @property
    def precision(self) -> float:
        return self.true_positive / self.predicted if self.predicted else 0.0

    @property
    def recall(self) -> float:
        return self.true_positive / self.expected if self.expected else 0.0


def evaluate(cases: list[Case] = CASES) -> list[Score]:
    expected: dict[SensitivityLabel, int] = {}
    predicted: dict[SensitivityLabel, int] = {}
    hits: dict[SensitivityLabel, int] = {}

    for case in cases:
        found = {finding.label for finding in detect_sensitivity(case.name, case.values)}
        for label in case.labels:
            expected[label] = expected.get(label, 0) + 1
            if label in found:
                hits[label] = hits.get(label, 0) + 1
        for label in found:
            predicted[label] = predicted.get(label, 0) + 1

    return sorted(
        (
            Score(
                label=label.value,
                expected=expected.get(label, 0),
                predicted=predicted.get(label, 0),
                true_positive=hits.get(label, 0),
            )
            for label in set(expected) | set(predicted)
        ),
        key=lambda score: score.label,
    )


def markdown(scores: list[Score], cases: list[Case] = CASES) -> str:
    lines = [
        "| label | expected columns | predicted columns | TP | precision | recall |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for score in scores:
        lines.append(
            f"| {score.label} | {score.expected} | {score.predicted} | {score.true_positive} "
            f"| {score.precision:.2f} | {score.recall:.2f} |"
        )
    flagged_controls = [
        case.name
        for case in cases
        if not case.labels and detect_sensitivity(case.name, case.values)
    ]
    lines.append("")
    lines.append(
        f"{len(cases)} labelled columns ({sum(1 for case in cases if case.labels)} sensitive, "
        f"{sum(1 for case in cases if not case.labels)} controls)."
    )
    if flagged_controls:
        lines.append(f"False positives on controls: {', '.join(flagged_controls)}")
    else:
        lines.append("False positives on controls: none.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    scores = evaluate()
    if args.json:
        print(
            json.dumps(
                {
                    "columns": len(CASES),
                    "scores": [
                        {
                            **score.__dict__,
                            "precision": round(score.precision, 4),
                            "recall": round(score.recall, 4),
                        }
                        for score in scores
                    ],
                },
                indent=2,
            )
        )
        return 0

    print("qc sensitivity detector — labelled fixture\n")
    print(markdown(scores))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
