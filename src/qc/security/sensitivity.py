"""Layered sensitivity detection (SPEC 8.3).

Layers: schema/name heuristics → format detectors → entropy/token
heuristics → configurable org detectors. Results carry label, evidence
type, and confidence. Matched values are never logged or persisted.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

from qc.domain.enums import ColumnAction, Confidence, SensitivityLabel
from qc.domain.models import SensitivityFinding
from qc.profiling.type_inference import EMAIL_RE, PHONE_RE

# ------------------------------------------------------------------ name layer

_NAME_RULES: list[tuple[str, SensitivityLabel, Confidence]] = [
    (r"(^|_)(email|mail|e_mail|email_address)($|_)", SensitivityLabel.EMAIL, Confidence.HIGH),
    (r"(^|_)(phone|mobile|tel|telephone|cell)($|_)", SensitivityLabel.PHONE, Confidence.HIGH),
    (
        r"(^|_)(first_name|last_name|full_name|name|customer_name|user_name|username)($|_)",
        SensitivityLabel.NAME,
        Confidence.MEDIUM,
    ),
    (
        r"(^|_)(address|street|city|zip|postal|zipcode)($|_)",
        SensitivityLabel.ADDRESS,
        Confidence.MEDIUM,
    ),
    (
        r"(ssn|social_security|passport|national_id|id_card|drivers_license|tax_id)",
        SensitivityLabel.GOVERNMENT_ID,
        Confidence.HIGH,
    ),
    (
        r"(^|_)(password|passwd|secret|token|api_key|apikey|private_key|credential)($|_)",
        SensitivityLabel.CREDENTIAL,
        Confidence.HIGH,
    ),
    (
        r"(^|_)(dob|birth_date|date_of_birth|birthdate)($|_)",
        SensitivityLabel.DATE_OF_BIRTH,
        Confidence.HIGH,
    ),
    (
        r"(^|_)(ip_address|ipaddr|last_ip|client_ip)($|_)",
        SensitivityLabel.IP_ADDRESS,
        Confidence.MEDIUM,
    ),
    (
        r"(^|_)(credit_card|card_number|cc_num|iban|account_number|routing)($|_)",
        SensitivityLabel.FINANCIAL,
        Confidence.HIGH,
    ),
    (
        r"(^|_)(diagnosis|medical|health|patient|insurance_id)($|_)",
        SensitivityLabel.HEALTH,
        Confidence.HIGH,
    ),
    (
        r"(^|_)(latitude|longitude|geo|location|gps)($|_)",
        SensitivityLabel.LOCATION,
        Confidence.MEDIUM,
    ),
    (r"(^|_)(ssn|sin|nino|cpf|dni)($|_)", SensitivityLabel.GOVERNMENT_ID, Confidence.HIGH),
]

# ----------------------------------------------------------------- format layer

# Credential-shaped tokens: long base64/hex with high entropy
_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=_-]{24,}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{32,}$")
# A hash digest is not a secret. Git SHAs (40) and digest sizes 32/64/128 are everywhere in
# ordinary tables, and treating them as credentials was a measured false positive
# (tools/bench/sensitivity_bench.py, qc#37). A hex *secret* is still caught when the column
# name says so, because the name layer adds CREDENTIAL first.
_DIGEST_RE = re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64}|[0-9a-fA-F]{128})$")
_JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")
_BEARER_RE = re.compile(r"(?i)^(bearer|basic|token)\s+\S+")
# Government ID shapes
_SSN_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$")
# ...and the shapes that would otherwise also look like a phone number: `123-45-6789`
# satisfies the loose phone pattern, so an SSN column reported PHONE as well (measured by
# tools/bench/sensitivity_bench.py, qc#37).
_NOT_PHONE_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$|^\d{4}[-/]\d{1,2}[-/]\d{1,2}$")
# Date shapes that the loose phone regex would otherwise match
_DATE_SHAPE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$|^\d{1,2}[-/]\d{1,2}[-/]\d{2,4}$")
# Embedded identifiers: found *inside* free text, not as the whole value. A notes column
# holding "call +1 415 555 0100 or email alice@example.com" is a disclosure risk that the
# free_text label alone does not describe (qc#37 follow-up).
_EMBEDDED_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
_EMBEDDED_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{5,20}\d")

# Card-like: 13-19 digits (with or without separators) — Luhn checked
_CARD_RE = re.compile(r"^\d{13,19}$|^\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}$")


@dataclass(frozen=True)
class DetectorConfig:
    """Organization-specific detector hooks (SPEC 8.3 layer 4)."""

    name_patterns: tuple[tuple[str, SensitivityLabel], ...] = ()
    value_patterns: tuple[tuple[str, str, SensitivityLabel], ...] = ()  # (id, regex, label)


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def detect_sensitivity(
    column_name: str,
    values: list[str],
    *,
    sample_limit: int = 200,
    config: DetectorConfig | None = None,
) -> list[SensitivityFinding]:
    """Run layered detection; returns findings without matched values.

    The ``evidence`` field names the evidence type only (e.g.
    "format:email_format"), never the matched value.
    """
    findings: dict[SensitivityLabel, SensitivityFinding] = {}

    def add(
        label: SensitivityLabel,
        confidence: Confidence,
        evidence: str,
        detector: str,
        action: ColumnAction = ColumnAction.MANUAL_REVIEW,
    ) -> None:
        existing = findings.get(label)
        if existing is None or _rank(confidence) > _rank(existing.confidence):
            findings[label] = SensitivityFinding(
                label=label,
                confidence=confidence,
                evidence=evidence,
                detector=detector,
                recommended_action=action,
            )

    # Layer 1: name heuristics. A sensitive word in a column name is evidence, not proof —
    # `email_verified` is a boolean, `token_count` is a count, and `phone_model` names a
    # product. Those were measured as false positives by tools/bench/sensitivity_bench.py
    # (qc#37), so name-derived labels are recorded separately and re-checked against the
    # values before they are returned.
    name_labels: set[SensitivityLabel] = set()
    lowered = column_name.lower()
    for pattern, label, conf in _NAME_RULES:
        if re.search(pattern, lowered):
            action = (
                ColumnAction.SYNTHETIC
                if label
                in (
                    SensitivityLabel.CREDENTIAL,
                    SensitivityLabel.GOVERNMENT_ID,
                    SensitivityLabel.FINANCIAL,
                )
                else ColumnAction.MANUAL_REVIEW
            )
            add(label, conf, "schema_name_pattern", "name_heuristic", action)
            name_labels.add(label)

    # Org-specific name rules
    if config:
        for pattern, label in config.name_patterns:
            if re.search(pattern.lower(), lowered):
                add(label, Confidence.MEDIUM, "org_name_pattern", "org_detector")

    observed = [v for v in values if v][:sample_limit]
    if not observed:
        return list(findings.values())

    n = len(observed)

    # Drop a name-derived label the values clearly contradict. Only the contradictions we
    # can state with evidence are used, and only for name-layer findings: a masked email
    # (`u***@example.com`) or a numeric phone still survives, because neither condition
    # applies to it.
    for label in name_labels:
        if _contradicts(label, observed):
            findings.pop(label, None)

    # Layer 2: format detectors
    email_hits = sum(1 for v in observed if EMAIL_RE.match(v))
    if email_hits / n >= 0.8:
        add(
            SensitivityLabel.EMAIL,
            Confidence.HIGH,
            "format:email",
            "format_detector",
            ColumnAction.SYNTHETIC,
        )

    phone_like = sum(
        1
        for v in observed
        if PHONE_RE.match(v)
        and 7 <= sum(c.isdigit() for c in v) <= 15
        and not _DATE_SHAPE_RE.match(v)  # dates are not phone numbers
        and not _NOT_PHONE_RE.match(v)  # government IDs are not phone numbers
    )
    if phone_like / n >= 0.8:
        add(
            SensitivityLabel.PHONE,
            Confidence.HIGH,
            "format:phone",
            "format_detector",
            ColumnAction.SYNTHETIC,
        )

    ssn_hits = sum(1 for v in observed if _SSN_RE.match(v))
    if ssn_hits / n >= 0.9 and n >= 3:
        add(
            SensitivityLabel.GOVERNMENT_ID,
            Confidence.MEDIUM,
            "format:ssn_like",
            "format_detector",
            ColumnAction.SYNTHETIC,
        )

    card_hits = sum(1 for v in observed if _CARD_RE.match(v) and _luhn_ok(v))
    if card_hits / n >= 0.9 and n >= 3:
        add(
            SensitivityLabel.FINANCIAL,
            Confidence.HIGH,
            "format:card_like_luhn",
            "format_detector",
            ColumnAction.SYNTHETIC,
        )

    jwt_hits = sum(1 for v in observed if _JWT_RE.match(v) or _BEARER_RE.match(v))
    if jwt_hits:
        add(
            SensitivityLabel.CREDENTIAL,
            Confidence.HIGH,
            "format:token_shape",
            "format_detector",
            ColumnAction.REDACT,
        )

    # Layer 3: entropy / token heuristics for secret-looking strings. A digest-shaped value
    # only counts when the column name already claims a credential; otherwise `commit_sha`
    # reads as a leaked token.
    digest_needs_name = SensitivityLabel.CREDENTIAL not in name_labels
    tokenish = 0
    for v in observed:
        if digest_needs_name and _DIGEST_RE.match(v):
            continue
        if (_TOKEN_RE.match(v) or _HEX_RE.match(v)) and _entropy(v) >= 3.5:
            tokenish += 1
    if tokenish / n >= 0.8 and n >= 3:
        add(
            SensitivityLabel.CREDENTIAL,
            Confidence.MEDIUM,
            "entropy:high_token",
            "entropy_detector",
            ColumnAction.REDACT,
        )

    # Org-specific value patterns
    if config:
        for pid, regex, label in config.value_patterns:
            try:
                pat = re.compile(regex)
            except re.error:
                continue
            hits = sum(1 for v in observed if pat.match(v))
            if hits / n >= 0.8:
                add(label, Confidence.MEDIUM, f"org_format:{pid}", "org_detector")

    # Free-text columns carry disclosure risk (SPEC 8.3)
    avg_len = sum(len(v) for v in observed) / n
    if avg_len > 120:
        add(
            SensitivityLabel.FREE_TEXT,
            Confidence.MEDIUM,
            "shape:long_text",
            "shape_detector",
            ColumnAction.REDACT,
        )

        # ...and the identifiers inside them are what makes that risk concrete. Only for
        # free text: a structured column's values are already classified whole.
        embedded_emails = sum(1 for v in observed if _EMBEDDED_EMAIL_RE.search(v))
        if embedded_emails:
            add(
                SensitivityLabel.EMAIL,
                Confidence.MEDIUM,
                "embedded:email_in_text",
                "embedded_detector",
                ColumnAction.MANUAL_REVIEW,
            )
        embedded_phones = sum(1 for v in observed if _embedded_phone(v))
        if embedded_phones:
            add(
                SensitivityLabel.PHONE,
                Confidence.MEDIUM,
                "embedded:phone_in_text",
                "embedded_detector",
                ColumnAction.MANUAL_REVIEW,
            )

    return list(findings.values())


def _embedded_phone(value: str) -> bool:
    """Whether free text contains something phone-shaped.

    Requires a separator or a country code: a bare eight-digit run inside prose is an
    order number far more often than a phone, and guessing would trade a real miss for a
    noisy false positive.
    """
    for match in _EMBEDDED_PHONE_RE.finditer(value):
        text = match.group(0)
        # Dates and times are the shape's worst neighbours: `2025-05-01` has eight digits
        # and a separator, and a schedule note was measured as a phone column because of it.
        if ":" in text or re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            continue
        if sum(c.isdigit() for c in text) >= 8 and re.search(r"[+\s().-]", text):
            return True
    return False


_BOOLEANISH = frozenset({"true", "false", "yes", "no", "y", "n", "t", "f", "0", "1"})
_NUMERIC_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)*$")


def _all_booleanish(observed: list[str]) -> bool:
    return all(value.strip().lower() in _BOOLEANISH for value in observed)


def _all_numeric(observed: list[str]) -> bool:
    return all(_NUMERIC_RE.match(value.strip()) for value in observed)


def _phone_share(observed: list[str]) -> float:
    """Share of values with enough digits to plausibly be a phone number."""
    hits = sum(1 for value in observed if sum(c.isdigit() for c in value) >= 7)
    return hits / len(observed)


def _contradicts(label: SensitivityLabel, observed: list[str]) -> bool:
    """Whether observed values make a name-derived label implausible.

    Deliberately narrow: each rule names the *shape* that cannot be the label, rather than
    guessing what the column is instead.
    """
    if _all_booleanish(observed):
        # A boolean column says `email_verified`, not `email`.
        return label in (
            SensitivityLabel.EMAIL,
            SensitivityLabel.PHONE,
            SensitivityLabel.CREDENTIAL,
        )
    if label in (SensitivityLabel.EMAIL, SensitivityLabel.CREDENTIAL) and _all_numeric(observed):
        # `token_count` is a count; an email address is never a bare number.
        return True
    # `phone_model` holds product names; fewer than a fifth look like numbers at all.
    return label == SensitivityLabel.PHONE and _phone_share(observed) < 0.2


def _rank(c: Confidence) -> int:
    return {Confidence.LOW: 1, Confidence.MEDIUM: 2, Confidence.HIGH: 3, Confidence.UNKNOWN: 0}[c]


def _luhn_ok(value: str) -> bool:
    digits = [int(c) for c in value if c.isdigit()]
    if len(digits) < 13:
        return False
    total = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def recommended_action_for(findings: Iterable[SensitivityFinding]) -> ColumnAction | None:
    """Strongest recommended action across findings (credential > others)."""
    priority = [
        ColumnAction.MANUAL_REVIEW,
        ColumnAction.REDACT,
        ColumnAction.SYNTHETIC,
        ColumnAction.TOKENIZE,
    ]
    best: ColumnAction | None = None
    for f in findings:
        if best is None or priority.index(f.recommended_action) < priority.index(best):
            best = f.recommended_action
    return best
