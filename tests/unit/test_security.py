"""Secret resolution and redaction tests (SPEC 5.1, 8.2, 15)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from qc.domain.errors import AuthError, InvalidArgumentsError
from qc.security.redaction import REDACTED, redact_mapping, redact_text, safe_error_message
from qc.security.secrets import EnvSecretResolver, resolve_secret


class TestEnvSecrets:
    def test_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("QC_TEST_SECRET", "s3cr3t")
        assert resolve_secret("env:QC_TEST_SECRET") == "s3cr3t"

    def test_missing_var(self) -> None:
        with pytest.raises(AuthError):
            resolve_secret("env:QC_DEFINITELY_NOT_SET_12345")

    def test_bad_name(self) -> None:
        with pytest.raises(InvalidArgumentsError):
            EnvSecretResolver().resolve("env:bad-name!")

    def test_no_inline_credentials_in_connection_ref(self) -> None:
        import pydantic

        from qc.domain.models import SourceConfig

        with pytest.raises(pydantic.ValidationError):
            SourceConfig(
                name="x",
                connector="postgres",
                connection_ref="postgres://user:pass@host/db",
            )

    def test_inline_password_rejected(self) -> None:
        import pydantic

        from qc.domain.models import SourceConfig

        with pytest.raises(pydantic.ValidationError):
            SourceConfig(name="x", connector="postgres", connection_ref="env:DB_PASSWORD=oops")


class TestFileSecrets:
    def test_reads_restricted_file(self, tmp_path: Path) -> None:
        secret = tmp_path / "secret"
        secret.write_text("hunter2\n", encoding="utf-8")
        secret.chmod(0o600)
        assert resolve_secret(f"file:{secret}") == "hunter2"

    def test_rejects_world_readable(self, tmp_path: Path) -> None:
        secret = tmp_path / "open-secret"
        secret.write_text("hunter2", encoding="utf-8")
        secret.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)
        with pytest.raises(AuthError):
            resolve_secret(f"file:{secret}")

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(AuthError):
            resolve_secret(f"file:{tmp_path / 'nope'}")


class TestRedaction:
    def test_password_value(self) -> None:
        assert "hunter2" not in redact_text("password=hunter2")

    def test_url_credentials(self) -> None:
        out = redact_text("connect postgres://alice:hunter2@db.example.com/x")
        assert "hunter2" not in out
        assert REDACTED in out

    def test_bearer_token(self) -> None:
        out = redact_text("Authorization: Bearer abc.def.ghi")
        assert "abc.def.ghi" not in out

    def test_jwt(self) -> None:
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N65IhcXlyV3"
        assert jwt not in redact_text(f"token {jwt}")

    def test_email(self) -> None:
        out = redact_text("contact jane.doe@example.com now")
        assert "jane.doe@example.com" not in out

    def test_mapping_redaction(self) -> None:
        data = {"password": "x", "nested": {"api_key": "y", "ok": "z"}, "list": ["a@b.co"]}
        out = redact_mapping(data)
        assert out["password"] == REDACTED
        assert out["nested"]["api_key"] == REDACTED
        assert out["nested"]["ok"] == "z"
        assert out["list"][0] == REDACTED

    def test_safe_error_message(self) -> None:
        exc = RuntimeError("failed auth password=hunter2 for db")
        assert "hunter2" not in safe_error_message(exc)

    def test_query_param(self) -> None:
        out = redact_text("jdbc:db://h/db?password=secret123&user=bob")
        assert "secret123" not in out


class TestLoggingRedaction:
    def test_json_formatter_redacts(self, caplog: pytest.LogCaptureFixture) -> None:
        import json
        import logging

        from qc.observability.logging import RedactingJsonFormatter

        record = logging.LogRecord(
            "qc.test", logging.INFO, "f.py", 1, "login password=hunter2", (), None
        )
        line = RedactingJsonFormatter().format(record)
        assert "hunter2" not in line
        payload = json.loads(line)
        assert payload["level"] == "INFO"
