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
_JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")
_BEARER_RE = re.compile(r"(?i)^(bearer|basic|token)\s+\S+")
# Government ID shapes
_SSN_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$")
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

    # Layer 1: name heuristics
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

    # Org-specific name rules
    if config:
        for pattern, label in config.name_patterns:
            if re.search(pattern.lower(), lowered):
                add(label, Confidence.MEDIUM, "org_name_pattern", "org_detector")

    observed = [v for v in values if v][:sample_limit]
    if not observed:
        return list(findings.values())

    n = len(observed)

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
        1 for v in observed if PHONE_RE.match(v) and 7 <= sum(c.isdigit() for c in v) <= 15
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

    # Layer 3: entropy / token heuristics for secret-looking strings
    tokenish = 0
    for v in observed:
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

    return list(findings.values())


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
