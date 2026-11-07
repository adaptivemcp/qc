"""OIDC verification over real HTTP (SPEC 12.4, ADR-003).

`test_mcp_oidc.py` injects the JWKS document, which pins the validation logic but never
exercises the network paths: discovery of `{issuer}/.well-known/openid-configuration` and the
JWKS fetch through `httpx`. This module runs a stub identity provider on localhost and uses the
**default** fetcher, so those paths are real — and then drives the MCP HTTP app with a valid
and an expired token to prove the 200/401 boundary rather than just the verifier's return
value.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from qc.config.loader import McpHttpConfig, OidcConfig
from qc.mcp.oidc import JwtTokenVerifier

AUDIENCE = "https://qc.example.com"

# Starlette deprecated httpx in TestClient; the dependency is pinned by the MCP SDK, and the
# replacement is not installable yet.
# Match by message: naming the class in the filter imports the module to resolve it, which
# emits the very warning the filter is meant to silence.
pytestmark = pytest.mark.filterwarnings("ignore:Using .httpx. with .starlette.testclient.")


@dataclass
class StubIdp:
    base_url: str
    issuer: str
    state: dict[str, Any]
    requests: Counter[str]
    _server: ThreadingHTTPServer

    @property
    def private_key(self) -> Any:
        return self.state["private"]

    @property
    def kid(self) -> str:
        return self.state["kid"]

    def token(self, **overrides: Any) -> str:
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": AUDIENCE,
            "sub": "user-1",
            "exp": now + 300,
            "iat": now,
            "scope": "qc:mcp profile:read",
            **overrides,
        }
        return jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": self.kid})

    def rotate(self) -> None:
        """Publish a new key under a new kid, as a provider rotation would."""
        private, kid = _keypair()
        self.state.update({"private": private, "kid": kid})

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _keypair() -> tuple[Any, str]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = f"rsa-{time.monotonic_ns()}"
    return private, kid


def _jwk(private: Any, kid: str) -> dict[str, Any]:
    jwk = RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return jwk


@pytest.fixture
def idp() -> Any:
    private, kid = _keypair()
    state: dict[str, Any] = {"private": private, "kid": kid}
    requests: Counter[str] = Counter()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests[self.path] += 1
            if self.path == "/.well-known/openid-configuration":
                body = {
                    "issuer": base,
                    "authorization_endpoint": f"{base}/authorize",
                    "token_endpoint": f"{base}/token",
                    "jwks_uri": f"{base}/.well-known/jwks.json",
                }
            elif self.path == "/.well-known/jwks.json":
                body = {"keys": [_jwk(state["private"], state["kid"])]}
            else:
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:  # silence test output
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    stub = StubIdp(
        base_url=base,
        issuer=base,
        state=state,
        requests=requests,
        _server=server,
    )
    yield stub
    stub.stop()


def _verifier(idp: StubIdp, **kwargs: Any) -> JwtTokenVerifier:
    # No `fetcher=`: discovery and JWKS go through httpx to the stub.
    return JwtTokenVerifier(
        issuer=idp.issuer,
        audience=AUDIENCE,
        required_scopes=["qc:mcp"],
        **kwargs,
    )


def _verify(verifier: JwtTokenVerifier, token: str) -> Any:
    import asyncio

    return asyncio.run(verifier.verify_token(token))


def test_a_valid_token_is_accepted_over_real_http(idp: StubIdp) -> None:
    verifier = _verifier(idp)

    token = _verify(verifier, idp.token())

    assert token is not None
    assert token.client_id == "user-1"
    assert token.scopes == ["qc:mcp", "profile:read"]
    assert token.resource == AUDIENCE
    # Discovery happened once, then the JWKS; neither is re-fetched per token.
    assert idp.requests["/.well-known/openid-configuration"] == 1
    assert idp.requests["/.well-known/jwks.json"] == 1

    assert _verify(verifier, idp.token()) is not None
    assert idp.requests["/.well-known/openid-configuration"] == 1
    assert idp.requests["/.well-known/jwks.json"] == 1


def test_an_expired_token_is_refused_over_real_http(idp: StubIdp) -> None:
    verifier = _verifier(idp)

    assert _verify(verifier, idp.token(exp=int(time.time()) - 600)) is None


def test_a_wrong_audience_is_refused_over_real_http(idp: StubIdp) -> None:
    verifier = _verifier(idp)

    assert _verify(verifier, idp.token(aud="https://other.example.com")) is None


def test_a_missing_scope_is_refused_over_real_http(idp: StubIdp) -> None:
    verifier = _verifier(idp)

    assert _verify(verifier, idp.token(scope="profile:read")) is None


def test_a_rotated_key_is_fetched_when_the_kid_is_unknown(idp: StubIdp) -> None:
    verifier = _verifier(idp)
    assert _verify(verifier, idp.token()) is not None
    fetches = idp.requests["/.well-known/jwks.json"]

    old_token = idp.token()
    idp.rotate()
    rotated = idp.token()

    # The old key is still in the cached document, so the old token keeps verifying until
    # the cache expires — revocation is a TTL property, not an unknown-kid one.
    assert _verify(verifier, old_token) is not None
    # A new kid is unknown, which forces exactly one refetch and then verifies.
    assert _verify(verifier, rotated) is not None
    assert idp.requests["/.well-known/jwks.json"] == fetches + 1
    # Discovery is not repeated on rotation.
    assert idp.requests["/.well-known/openid-configuration"] == 1


def test_the_mcp_http_app_accepts_a_valid_token_and_rejects_an_expired_one(
    idp: StubIdp, tmp_path: Path
) -> None:
    from starlette.testclient import TestClient

    from qc.application.runtime import Runtime
    from qc.mcp.server import build_server_http

    cfg = McpHttpConfig(
        enabled=True,
        mode="oidc",
        oidc=OidcConfig(issuer_url=idp.issuer, audience=AUDIENCE, required_scopes=["qc:mcp"]),
    )
    server = build_server_http(Runtime(root=tmp_path), "", cfg)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    headers = {"accept": "application/json, text/event-stream"}

    # The Host header must be a localhost name: the MCP transport's DNS-rebinding guard
    # rejects TestClient's default `testserver` with 421.
    with TestClient(server.streamable_http_app(), base_url="http://127.0.0.1:8765") as client:
        accepted = client.post(
            "/mcp",
            json=request,
            headers={**headers, "authorization": f"Bearer {idp.token()}"},
        )
        expired = client.post(
            "/mcp",
            json=request,
            headers={
                **headers,
                "authorization": f"Bearer {idp.token(exp=int(time.time()) - 600)}",
            },
        )
        anonymous = client.post("/mcp", json=request, headers=headers)

    assert accepted.status_code == 200
    assert expired.status_code == 401
    assert anonymous.status_code == 401
