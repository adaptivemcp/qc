"""Security: secrets, redaction, policy evaluation, audit.

``AuditLog`` is exported lazily to avoid an import cycle with
``qc.observability.logging`` (audit logging needs the log formatter).
"""

from typing import TYPE_CHECKING, Any

from qc.security.policy import (
    Ceiling,
    DecisionContext,
    Operation,
    PolicyDecision,
    PolicyEngine,
    engine_for,
)
from qc.security.redaction import REDACTED, redact_mapping, redact_text, safe_error_message
from qc.security.secrets import SecretResolverChain, resolve_secret

if TYPE_CHECKING:
    from qc.security.audit import AuditLog

__all__ = [
    "REDACTED",
    "AuditLog",
    "Ceiling",
    "DecisionContext",
    "Operation",
    "PolicyDecision",
    "PolicyEngine",
    "SecretResolverChain",
    "engine_for",
    "redact_mapping",
    "redact_text",
    "resolve_secret",
    "safe_error_message",
]


def __getattr__(name: str) -> Any:
    if name == "AuditLog":
        from qc.security.audit import AuditLog

        return AuditLog
    raise AttributeError(name)
