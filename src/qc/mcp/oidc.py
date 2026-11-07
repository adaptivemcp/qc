"""OIDC bearer-token verification for the MCP HTTP server (SPEC 12.4, ADR-003).

The static shared token in `server.py` is the v1 stopgap; this is the shape
`docs/operations.md` specified for a real deployment: the client obtains a token from an
identity provider, QC validates it locally against the provider's keys.

Validation, per request, in this order:

1. **Signature** — the token's `alg` must be RS256 or ES256, the key is selected by `kid`
   from the provider's JWKS, and the JWKS is cached with a TTL. An unknown `kid` forces one
   refetch, which is how key rotation is picked up without a restart.
2. **Claims** — `iss` must equal the configured issuer, `aud` must contain QC's audience,
   and `exp`/`nbf` must be current (30s leeway for clock skew).
3. **Scopes** — every configured required scope must be present in `scope` (space-delimited
   string or list).

A failure returns `None`, which the MCP auth middleware renders as a `401` with
`invalid_token`. The reason goes to the log, never to the client: a verifier that explains
itself to an unauthenticated caller is an oracle.

The verifier is deliberately synchronous inside `verify_token` (the JWKS fetch uses
`httpx.Client`): the MCP framework calls it from async code, but the work is one cached
lookup plus one crypto check, and a thread hop per cached token would buy nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

from qc.observability.logging import get_logger

log = get_logger(__name__)

#: Algorithms this verifier accepts. `none` and HMAC are refused by construction: a
#: symmetric key in a JWKS is a deployment error, and the classic `alg: none` bypass is
#: only possible when the algorithm is taken from the token without an allowlist.
ALLOWED_ALGORITHMS = ("RS256", "ES256")

#: Clock skew tolerated on `exp`/`nbf`, in seconds.
LEEWAY_SECONDS = 30


@dataclass
class _JwksCache:
    """A JWKS document and the instant it stops being trusted."""

    keys: dict[str, Any] = field(default_factory=dict)
    expires_at: float = 0.0

    def fresh(self) -> bool:
        return bool(self.keys) and time.monotonic() < self.expires_at


class JwtTokenVerifier:
    """Verify OIDC-issued access tokens against a provider's JWKS."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        required_scopes: list[str] | None = None,
        jwks_uri: str | None = None,
        cache_seconds: float = 300.0,
        timeout_seconds: float = 5.0,
        fetcher: Any | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self.required_scopes = list(required_scopes or [])
        self._jwks_uri = jwks_uri
        self._cache_seconds = cache_seconds
        self._timeout = timeout_seconds
        self._cache = _JwksCache()
        # Injectable for tests: `(url) -> dict`. The default uses httpx.
        self._fetch: Any = fetcher or self._fetch_json

    # ------------------------------------------------------------------ protocol

    async def verify_token(self, token: str) -> Any:
        from mcp.server.auth.provider import AccessToken

        try:
            claims = self._verify(token)
        except Exception as exc:
            log.warning(
                "mcp token rejected",
                fields={"detail": f"{type(exc).__name__}: {exc}"},
            )
            return None

        scopes = _scopes(claims)
        missing = [scope for scope in self.required_scopes if scope not in scopes]
        if missing:
            log.warning(
                "mcp token missing required scope",
                fields={"detail": ", ".join(missing)},
            )
            return None

        subject = claims.get("sub")
        # `client_id` is the OAuth client; `azp` is Keycloak's authorized-party claim and is
        # what a light access token carries when `sub` is omitted. Without `azp` first, every
        # Keycloak token was attributed to the constant below, losing per-client identity.
        client_id = claims.get("client_id") or claims.get("azp") or subject or "oidc-client"
        return AccessToken(
            token=token,
            client_id=str(client_id),
            scopes=scopes,
            expires_at=int(claims["exp"]) if claims.get("exp") else None,
            resource=self.audience,
            subject=str(subject) if subject else None,
            claims=dict(claims),
        )

    # ------------------------------------------------------------------ internals

    def _verify(self, token: str) -> dict[str, Any]:
        header = jwt.get_unverified_header(token)
        algorithm = header.get("alg")
        if algorithm not in ALLOWED_ALGORITHMS:
            raise ValueError(f"algorithm {algorithm!r} is not allowed")
        key = self._key_for(header.get("kid"), algorithm)
        return jwt.decode(
            token,
            key=key,
            algorithms=[algorithm],
            audience=self.audience,
            issuer=self.issuer,
            options={"require": ["exp", "iss", "aud"]},
            leeway=LEEWAY_SECONDS,
        )

    def _key_for(self, kid: str | None, algorithm: str) -> Any:
        key = self._cached_key(kid)
        if key is not None:
            return key
        # Unknown kid: the provider may have rotated keys since the last fetch. One
        # refetch, then give up — looping on an attacker-chosen kid is a denial of service.
        self._refresh()
        key = self._cached_key(kid)
        if key is None:
            raise ValueError(f"no key for kid {kid!r} (algorithm {algorithm})")
        return key

    def _cached_key(self, kid: str | None) -> Any:
        if not self._cache.fresh():
            self._refresh()
        if kid is None:
            # A single-key JWKS is valid without `kid`; pick it rather than guessing among many.
            if len(self._cache.keys) == 1:
                return next(iter(self._cache.keys.values()))
            raise ValueError("token has no kid and the provider publishes several keys")
        return self._cache.keys.get(kid)

    def _refresh(self) -> None:
        uri = self._jwks_uri or self._discover_jwks_uri()
        document = self._fetch(uri)
        keys: dict[str, Any] = {}
        for entry in document.get("keys", []):
            kid = entry.get("kid")
            if isinstance(kid, str):
                keys[kid] = _public_key(entry)
        if not keys:
            raise ValueError("JWKS document has no usable keys")
        self._cache = _JwksCache(keys=keys, expires_at=time.monotonic() + self._cache_seconds)
        self._jwks_uri = uri

    def _discover_jwks_uri(self) -> str:
        document = self._fetch(f"{self.issuer}/.well-known/openid-configuration")
        uri = document.get("jwks_uri")
        if not isinstance(uri, str) or not uri:
            raise ValueError("identity provider publishes no jwks_uri")
        return uri

    def _fetch_json(self, url: str) -> dict[str, Any]:
        response = httpx.get(url, timeout=self._timeout, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"{url} did not return a JSON object")
        return payload


def _public_key(jwk: dict[str, Any]) -> Any:
    """A `cryptography` public key from one JWKS entry."""
    from jwt.algorithms import ECAlgorithm, RSAAlgorithm

    kty = jwk.get("kty")
    if kty == "RSA":
        return RSAAlgorithm.from_jwk(jwk)
    if kty == "EC":
        return ECAlgorithm.from_jwk(jwk)
    raise ValueError(f"unsupported JWKS key type {kty!r}")


def _scopes(claims: dict[str, Any]) -> list[str]:
    """Scopes from `scope` (space-delimited, per RFC 6749/7662) or a `scopes` list."""
    raw = claims.get("scope")
    if isinstance(raw, str):
        return [scope for scope in raw.split() if scope]
    listed = claims.get("scopes")
    if isinstance(listed, list):
        return [str(scope) for scope in listed]
    return []
