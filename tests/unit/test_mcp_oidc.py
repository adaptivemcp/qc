"""OIDC bearer verification for the MCP HTTP server (SPEC 12.4, ADR-003).

Offline: the JWKS document is injected. What these tests pin is the validation order the
operations docs promise — signature, then claims, then scopes — plus the two properties a
naive verifier gets wrong: an algorithm taken from the token (``none``/HS256 bypass) and a
rotated key that a cached JWKS would keep rejecting.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from qc.mcp.oidc import JwtTokenVerifier

ISSUER = "https://idp.example.com"
AUDIENCE = "https://qc.example.com"
JWKS_URI = f"{ISSUER}/.well-known/jwks.json"


def _rsa_key() -> tuple[Any, dict[str, Any]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwk.update({"kid": "rsa-1", "use": "sig", "alg": "RS256"})
    return private, jwk


def _ec_key() -> tuple[Any, dict[str, Any]]:
    private = ec.generate_private_key(ec.SECP256R1())
    jwk = ECAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwk.update({"kid": "ec-1", "use": "sig", "alg": "ES256"})
    return private, jwk


def _token(private: Any, *, kid: str, alg: str = "RS256", **overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-1",
        "exp": now + 300,
        "iat": now,
        "scope": "qc:mcp profile:read",
        **overrides,
    }
    return jwt.encode(claims, private, algorithm=alg, headers={"kid": kid})


def _verifier(document: dict[str, Any], **kwargs: Any) -> JwtTokenVerifier:
    calls: list[str] = []

    def fetcher(url: str) -> dict[str, Any]:
        calls.append(url)
        if url.endswith("openid-configuration"):
            return {"issuer": ISSUER, "jwks_uri": JWKS_URI}
        return document

    verifier = JwtTokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        required_scopes=["qc:mcp"],
        jwks_uri=JWKS_URI,
        fetcher=fetcher,
        **kwargs,
    )
    verifier.calls = calls  # type: ignore[attr-defined]
    return verifier


def _verify(verifier: JwtTokenVerifier, token: str) -> Any:
    return asyncio.run(verifier.verify_token(token))


def test_a_valid_rs256_token_is_accepted_with_its_scopes() -> None:
    private, jwk = _rsa_key()
    verifier = _verifier({"keys": [jwk]})

    token = _verify(verifier, _token(private, kid="rsa-1"))

    assert token is not None
    assert token.client_id == "user-1"
    assert token.subject == "user-1"
    assert token.scopes == ["qc:mcp", "profile:read"]
    assert token.resource == AUDIENCE
    assert token.expires_at and token.expires_at > time.time()


def test_a_valid_es256_token_is_accepted() -> None:
    private, jwk = _ec_key()
    verifier = _verifier({"keys": [jwk]})

    token = _verify(verifier, _token(private, kid="ec-1", alg="ES256"))

    assert token is not None
    assert token.client_id == "user-1"


def test_an_expired_token_is_refused() -> None:
    private, jwk = _rsa_key()
    verifier = _verifier({"keys": [jwk]})

    expired = _token(private, kid="rsa-1", exp=int(time.time()) - 3600)

    assert _verify(verifier, expired) is None


def test_a_token_for_another_audience_is_refused() -> None:
    private, jwk = _rsa_key()
    verifier = _verifier({"keys": [jwk]})

    wrong = _token(private, kid="rsa-1", aud="https://other.example.com")

    assert _verify(verifier, wrong) is None


def test_a_token_from_another_issuer_is_refused() -> None:
    private, jwk = _rsa_key()
    verifier = _verifier({"keys": [jwk]})

    wrong = _token(private, kid="rsa-1", iss="https://evil.example.com")

    assert _verify(verifier, wrong) is None


def test_a_token_missing_a_required_scope_is_refused() -> None:
    private, jwk = _rsa_key()
    verifier = _verifier({"keys": [jwk]})

    token = _token(private, kid="rsa-1", scope="profile:read")

    assert _verify(verifier, token) is None


def test_an_unsigned_or_hmac_token_is_refused_even_with_a_valid_claim_set() -> None:
    _, jwk = _rsa_key()
    verifier = _verifier({"keys": [jwk]})
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "attacker",
        "exp": int(time.time()) + 300,
        "scope": "qc:mcp",
    }

    unsigned = jwt.encode(claims, key="", algorithm="none")
    hmac = jwt.encode(
        claims,
        key="a-shared-secret-long-enough-for-hs256",
        algorithm="HS256",
        headers={"kid": "rsa-1"},
    )

    assert _verify(verifier, unsigned) is None
    assert _verify(verifier, hmac) is None


def test_the_jwks_is_cached_between_requests() -> None:
    private, jwk = _rsa_key()
    verifier = _verifier({"keys": [jwk]})
    token = _token(private, kid="rsa-1")

    assert _verify(verifier, token) is not None
    assert _verify(verifier, token) is not None

    assert verifier.calls == [JWKS_URI]  # type: ignore[attr-defined]


def test_a_rotated_key_is_picked_up_by_refetching_once() -> None:
    old_private, old_jwk = _rsa_key()
    new_private, new_jwk = _rsa_key()
    new_jwk["kid"] = "rsa-2"
    documents = [{"keys": [old_jwk]}, {"keys": [new_jwk]}]
    calls: list[str] = []

    def fetcher(url: str) -> dict[str, Any]:
        calls.append(url)
        return documents[0] if len(calls) == 1 else documents[-1]

    verifier = JwtTokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        required_scopes=["qc:mcp"],
        jwks_uri=JWKS_URI,
        fetcher=fetcher,
    )

    assert _verify(verifier, _token(old_private, kid="rsa-1")) is not None
    # The provider rotated: a new kid arrives while the cached document is still fresh.
    rotated = _token(new_private, kid="rsa-2")
    assert _verify(verifier, rotated) is not None
    assert len(calls) == 2


def test_the_jwks_uri_is_discovered_when_not_configured() -> None:
    private, jwk = _rsa_key()
    verifier = _verifier({"keys": [jwk]})
    # Drop the configured URI: the verifier must read openid-configuration first.
    verifier._jwks_uri = None  # type: ignore[attr-defined]

    assert _verify(verifier, _token(private, kid="rsa-1")) is not None
    assert verifier.calls[:2] == [  # type: ignore[attr-defined]
        f"{ISSUER}/.well-known/openid-configuration",
        JWKS_URI,
    ]


def test_an_unknown_kid_does_not_loop_refetching() -> None:
    stranger_private, jwk = _rsa_key()
    verifier = _verifier({"keys": [jwk]})

    token = _token(stranger_private, kid="does-not-exist")

    assert _verify(verifier, token) is None
    # One fetch for the document plus one rotation attempt, then stop.
    assert len(verifier.calls) == 2  # type: ignore[attr-defined]


def test_unexpected_errors_are_reported_as_unauthenticated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    verifier = _verifier({"keys": []})

    assert _verify(verifier, "not-a-jwt") is None


class TestConfig:
    def test_oidc_section_is_parsed(self, tmp_path) -> None:
        from qc.config.loader import load_config

        path = tmp_path / "qc.toml"
        path.write_text(
            "version = 1\n"
            "[mcp.http]\n"
            "enabled = true\n"
            'mode = "oidc"\n'
            "[mcp.http.oidc]\n"
            f'issuer_url = "{ISSUER}"\n'
            f'audience = "{AUDIENCE}"\n'
            'required_scopes = ["qc:mcp"]\n',
            encoding="utf-8",
        )
        cfg = load_config(path)
        assert cfg.mcp_http.mode == "oidc"
        assert cfg.mcp_http.oidc is not None
        assert cfg.mcp_http.oidc.issuer_url == ISSUER
        assert cfg.mcp_http.oidc.required_scopes == ["qc:mcp"]

    def test_oidc_mode_without_the_block_is_refused(self, tmp_path) -> None:
        from qc.config.loader import load_config
        from qc.domain.errors import InvalidArgumentsError

        path = tmp_path / "qc.toml"
        path.write_text(
            'version = 1\n[mcp.http]\nenabled = true\nmode = "oidc"\n', encoding="utf-8"
        )
        with pytest.raises(InvalidArgumentsError, match=r"mcp.http.oidc"):
            load_config(path)

    def test_the_http_server_wires_the_oidc_verifier_and_resource_binding(self, tmp_path) -> None:
        from mcp.server.auth.settings import AuthSettings
        from pydantic import AnyHttpUrl

        from qc.config.loader import load_config
        from qc.mcp.oidc import JwtTokenVerifier
        from qc.mcp.server import _auth_for

        path = tmp_path / "qc.toml"
        path.write_text(
            "version = 1\n"
            "[mcp.http]\n"
            "enabled = true\n"
            'mode = "oidc"\n'
            "[mcp.http.oidc]\n"
            f'issuer_url = "{ISSUER}"\n'
            f'audience = "{AUDIENCE}"\n',
            encoding="utf-8",
        )
        cfg = load_config(path).mcp_http

        verifier, auth = _auth_for("", cfg, AnyHttpUrl, AuthSettings)

        assert isinstance(verifier, JwtTokenVerifier)
        assert verifier.audience == AUDIENCE
        assert str(auth.resource_server_url).rstrip("/") == AUDIENCE
        # The binding that matters: a token minted for another service is refused.
        assert auth.validate_token_resource is True
