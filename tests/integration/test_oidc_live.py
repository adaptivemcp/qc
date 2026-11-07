"""Live OIDC verification against a real identity provider (SPEC 12.4, ADR-003).

The unit tests pin the logic and a localhost stub exercises the HTTP paths. This is the
third layer: a **real** provider, run on demand, because interoperability is a property no
stub can demonstrate — it was Keycloak's light access token that showed `azp` is the client
identity when `sub` is absent.

Skipped unless the environment points at a provider and a token:

    export QC_OIDC_ISSUER=https://idp.example.com/realms/qc
    export QC_OIDC_AUDIENCE=qc-mcp
    export QC_OIDC_TOKEN=$(curl -s … /protocol/openid-connect/token … | jq -r .access_token)
    export QC_OIDC_SCOPE=qc:mcp          # optional; used as required_scopes

See `docs/operations.md` for the measured Keycloak run.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from qc.mcp.oidc import JwtTokenVerifier

ISSUER = os.environ.get("QC_OIDC_ISSUER")
AUDIENCE = os.environ.get("QC_OIDC_AUDIENCE", "")
TOKEN = os.environ.get("QC_OIDC_TOKEN")
SCOPE = os.environ.get("QC_OIDC_SCOPE")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (ISSUER and AUDIENCE and TOKEN),
        reason="set QC_OIDC_ISSUER / QC_OIDC_AUDIENCE / QC_OIDC_TOKEN to run live",
    ),
]


def _verify(issuer: str, audience: str, scopes: list[str] | None = None):  # type: ignore[no-untyped-def]
    verifier = JwtTokenVerifier(
        issuer=issuer,
        audience=audience,
        required_scopes=scopes or [],
    )
    return asyncio.run(verifier.verify_token(TOKEN or ""))


def test_a_real_provider_token_is_accepted() -> None:
    access = _verify(ISSUER or "", AUDIENCE, [SCOPE] if SCOPE else None)

    assert access is not None
    assert access.scopes
    assert access.resource == AUDIENCE
    # The client identity comes from `azp` on a provider whose access token has no `sub`.
    assert access.client_id and access.client_id != "oidc-client"


def test_a_real_provider_token_for_another_audience_is_refused() -> None:
    assert _verify(ISSUER or "", f"{AUDIENCE}-other") is None


def test_a_real_provider_token_from_another_issuer_is_refused() -> None:
    assert _verify(f"{ISSUER}-other", AUDIENCE) is None


def test_a_real_provider_token_without_the_required_scope_is_refused() -> None:
    assert _verify(ISSUER or "", AUDIENCE, ["qc:scope-that-does-not-exist"]) is None
