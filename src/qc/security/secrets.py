"""Secret resolution (SPEC 5.1, 19.4).

Secrets are referenced, never stored. Supported references:

- ``env:VAR_NAME``        environment variable
- ``file:/path/to/secret`` file (first line, restrictive permissions checked)
- ``keyring:service/user`` OS credential store (optional dependency)
- ``cmd:program args``    stdout of a helper (no shell, no secrets in argv logs)

Resolution happens in-process only; resolved values never appear in
artifacts, logs, or error messages.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Protocol

from qc.domain.errors import AuthError, InvalidArgumentsError


class SecretResolver(Protocol):
    def resolve(self, ref: str) -> str: ...


class EnvSecretResolver:
    """Resolve ``env:NAME`` references."""

    def resolve(self, ref: str) -> str:
        if not ref.startswith("env:"):
            raise InvalidArgumentsError("not an env: reference", detail={"ref_kind": "invalid"})
        name = ref[4:]
        if not name or not name.replace("_", "").isalnum():
            raise InvalidArgumentsError("invalid environment variable name")
        value = os.environ.get(name)
        if not value:
            raise AuthError(f"environment variable {name} is not set")
        return value


class FileSecretResolver:
    """Resolve ``file:/path`` references; refuse group/world-readable files."""

    def resolve(self, ref: str) -> str:
        if not ref.startswith("file:"):
            raise InvalidArgumentsError("not a file: reference")
        path = Path(ref[5:]).expanduser()
        if not path.is_file():
            raise AuthError(f"secret file not found: {path.name}")
        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise AuthError("cannot read secret file metadata") from exc
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            raise AuthError(
                f"secret file {path.name} is readable by group/others; run chmod 600 on it"
            )
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AuthError(f"cannot read secret file {path.name}") from exc
        if not value:
            raise AuthError(f"secret file {path.name} is empty")
        return value


class CmdSecretResolver:
    """Resolve ``cmd:program`` by executing a fixed helper (no shell)."""

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def resolve(self, ref: str) -> str:
        if not ref.startswith("cmd:"):
            raise InvalidArgumentsError("not a cmd: reference")
        argv = ref[4:].split()
        if not argv:
            raise InvalidArgumentsError("empty cmd: reference")
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AuthError("secret helper failed to run") from exc
        if proc.returncode != 0:
            raise AuthError(f"secret helper exited with {proc.returncode}")
        value = proc.stdout.strip()
        if not value:
            raise AuthError("secret helper produced no output")
        return value


class KeyringSecretResolver:
    """Resolve ``keyring:service/user`` via the OS credential store."""

    def resolve(self, ref: str) -> str:
        if not ref.startswith("keyring:"):
            raise InvalidArgumentsError("not a keyring: reference")
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError as exc:
            raise AuthError("keyring support not installed (pip install keyring)") from exc
        body = ref[8:]
        if "/" not in body:
            raise InvalidArgumentsError("keyring ref must be keyring:service/user")
        service, user = body.split("/", 1)
        value: str | None = keyring.get_password(service, user)
        if not value:
            raise AuthError(f"no credential stored for {service}/{user}")
        return value


class SecretResolverChain:
    """Dispatcher over supported reference schemes."""

    def __init__(self) -> None:
        self._resolvers: dict[str, SecretResolver] = {
            "env:": EnvSecretResolver(),
            "file:": FileSecretResolver(),
            "cmd:": CmdSecretResolver(),
            "keyring:": KeyringSecretResolver(),
        }

    def resolve(self, ref: str) -> str:
        for prefix, resolver in self._resolvers.items():
            if ref.startswith(prefix):
                return resolver.resolve(ref)
        raise InvalidArgumentsError(
            "unsupported connection_ref scheme; use env:, file:, cmd:, or keyring:"
        )


_default = SecretResolverChain()


def resolve_secret(ref: str) -> str:
    """Resolve a secret reference using the default chain."""
    return _default.resolve(ref)
