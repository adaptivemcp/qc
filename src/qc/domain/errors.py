"""Structured errors with stable exit codes and MCP error codes (SPEC 11.3, 12.4)."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class ExitCode(IntEnum):
    """Stable process exit codes for the CLI (SPEC 11.3)."""

    SUCCESS = 0
    GENERAL_FAILURE = 1
    INVALID_ARGUMENTS = 2
    AUTH_FAILURE = 3
    POLICY_DENIED = 4
    BUDGET_EXHAUSTED = 5
    SOURCE_ERROR = 6
    VALIDATION_FAILURE = 7
    UNSUPPORTED = 8
    DESTINATION_WRITE_FAILURE = 9


class ErrorCode(StrEnum):
    """Stable machine-readable error codes (SPEC 12.4)."""

    POLICY_DENIED = "POLICY_DENIED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    AUTH_FAILED = "AUTH_FAILED"
    CANCELLED = "CANCELLED"
    INTERNAL = "INTERNAL"
    WRITE_FAILED = "WRITE_FAILED"
    GENERATION_FAILED = "GENERATION_FAILED"


class QCError(Exception):
    """Base error carrying a stable code and exit code.

    ``detail`` is safe for display; it must never contain credentials,
    raw rows, or unredacted query parameters (SPEC 8.2).
    """

    code: ErrorCode = ErrorCode.INTERNAL
    exit_code: ExitCode = ExitCode.GENERAL_FAILURE

    def __init__(self, message: str, *, detail: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict[str, object]:
        return {"error": self.code.value, "message": self.message, "detail": self.detail}


class PolicyDeniedError(QCError):
    code = ErrorCode.POLICY_DENIED
    exit_code = ExitCode.POLICY_DENIED


class BudgetExceededError(QCError):
    code = ErrorCode.BUDGET_EXCEEDED
    exit_code = ExitCode.BUDGET_EXHAUSTED

    def __init__(self, message: str, *, dimension: str | None = None) -> None:
        detail = {"dimension": dimension} if dimension else {}
        super().__init__(message, detail=detail)
        self.dimension = dimension


class SourceError(QCError):
    code = ErrorCode.SOURCE_UNAVAILABLE
    exit_code = ExitCode.SOURCE_ERROR


class UnsupportedError(QCError):
    code = ErrorCode.UNSUPPORTED_CAPABILITY
    exit_code = ExitCode.UNSUPPORTED


class ValidationError(QCError):
    code = ErrorCode.VALIDATION_FAILED
    exit_code = ExitCode.VALIDATION_FAILURE


class NotFoundError(QCError):
    code = ErrorCode.NOT_FOUND
    exit_code = ExitCode.GENERAL_FAILURE


class InvalidArgumentsError(QCError):
    code = ErrorCode.INVALID_ARGUMENTS
    exit_code = ExitCode.INVALID_ARGUMENTS


class AuthError(QCError):
    code = ErrorCode.AUTH_FAILED
    exit_code = ExitCode.AUTH_FAILURE


class CancelledError(QCError):
    code = ErrorCode.CANCELLED
    exit_code = ExitCode.BUDGET_EXHAUSTED


class GenerationError(QCError):
    code = ErrorCode.GENERATION_FAILED
    exit_code = ExitCode.VALIDATION_FAILURE


class WriteError(QCError):
    code = ErrorCode.WRITE_FAILED
    exit_code = ExitCode.DESTINATION_WRITE_FAILURE
