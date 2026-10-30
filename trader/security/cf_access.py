"""Cloudflare Access JWT verification (defense in depth behind Cloudflare Tunnel).

When Access protects the app, Cloudflare adds a `Cf-Access-Jwt-Assertion` header to every
request it lets through: an RS256 JWT signed with the team's keys, published as a JWKS at
`https://<team_domain>/cdn-cgi/access/certs`. Verifying it here means a request that did not
pass the Access policy (for example one that reached the container some other way) is refused
even though the tunnel normally guarantees it. Implemented with `cryptography` only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

log = logging.getLogger(__name__)

HEADER = "cf-access-jwt-assertion"
CERTS_PATH = "/cdn-cgi/access/certs"
MAX_TOKEN_BYTES = 16 * 1024
_B64URL = re.compile(r"^[A-Za-z0-9_-]*$")

JwksFetcher = Callable[[str], Awaitable[dict[str, Any]]]


class CFAccessError(Exception):
    """The Access token is missing, malformed, expired, for another app, or not signed by
    the team's keys. Messages never contain the token."""


async def fetch_jwks(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        response = await client.get(url, headers={"Accept": "application/json"})
        response.raise_for_status()
        data = response.json()
    if not isinstance(data, dict):
        raise ValueError("JWKS response is not a JSON object")
    return data


def normalize_team_domain(team_domain: str) -> str:
    """Accept "team.cloudflareaccess.com" or a pasted "https://team.cloudflareaccess.com/"."""
    domain = team_domain.strip()
    if domain.lower().startswith("https://"):
        domain = domain[len("https://") :]
    return domain.rstrip("/").lower()


def _b64url_decode(data: str) -> bytes:
    if not _B64URL.match(data):
        raise CFAccessError("malformed token encoding")
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        raise CFAccessError("malformed token encoding") from None


def _b64url_uint(data: str) -> int:
    return int.from_bytes(_b64url_decode(data), "big")


def parse_jwks(jwks: dict[str, Any]) -> dict[str, rsa.RSAPublicKey]:
    """RSA signing keys by kid. Keys that are not RS256-capable or are too short are skipped."""
    keys: dict[str, rsa.RSAPublicKey] = {}
    entries = jwks.get("keys")
    if not isinstance(entries, list):
        return keys
    for jwk in entries:
        if not isinstance(jwk, dict) or jwk.get("kty") != "RSA":
            continue
        if jwk.get("alg") not in (None, "RS256") or jwk.get("use") not in (None, "sig"):
            continue
        kid, n, e = jwk.get("kid"), jwk.get("n"), jwk.get("e")
        if not (isinstance(kid, str) and kid and isinstance(n, str) and isinstance(e, str)):
            continue
        try:
            key = rsa.RSAPublicNumbers(_b64url_uint(e), _b64url_uint(n)).public_key()
        except (CFAccessError, ValueError):
            continue
        if key.key_size >= 2048:
            keys[kid] = key
    return keys


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class CFAccessVerifier:
    """Verifies Access JWTs for one team and application (audience tag)."""

    def __init__(
        self,
        team_domain: str,
        audience: str,
        *,
        fetcher: JwksFetcher | None = None,
        cache_seconds: float = 3600.0,
        leeway_seconds: float = 60.0,
        min_refetch_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ):
        domain = normalize_team_domain(team_domain)
        if not domain or not audience.strip():
            raise ValueError("Cloudflare Access needs both a team domain and an audience tag")
        self.issuer = f"https://{domain}"
        self.certs_url = f"{self.issuer}{CERTS_PATH}"
        self.audience = audience.strip()
        self._fetcher = fetcher
        self._cache_seconds = cache_seconds
        self._leeway = leeway_seconds
        self._min_refetch = min_refetch_seconds
        self._clock = clock
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._fetched_at: float | None = None  # last successful fetch
        self._attempted_at: float | None = None  # last fetch attempt (throttles refetches)
        self._lock = asyncio.Lock()

    async def verify(self, token: str) -> dict[str, Any]:
        """Return the token's claims, or raise CFAccessError."""
        if not token or len(token) > MAX_TOKEN_BYTES:
            raise CFAccessError("missing or oversized token")
        parts = token.split(".")
        if len(parts) != 3:
            raise CFAccessError("malformed token")
        try:
            header = json.loads(_b64url_decode(parts[0]))
            claims = json.loads(_b64url_decode(parts[1]))
        except (ValueError, UnicodeDecodeError):
            raise CFAccessError("malformed token") from None
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise CFAccessError("malformed token")
        if header.get("alg") != "RS256":
            raise CFAccessError("unexpected signing algorithm")
        if "crit" in header:
            raise CFAccessError("unsupported critical header")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise CFAccessError("token has no key id")
        signature = _b64url_decode(parts[2])
        key = await self._key(kid)
        try:
            key.verify(
                signature,
                f"{parts[0]}.{parts[1]}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature:
            raise CFAccessError("bad signature") from None
        self._check_claims(claims)
        return claims

    def _check_claims(self, claims: dict[str, Any]) -> None:
        now = self._clock()
        if claims.get("iss") != self.issuer:
            raise CFAccessError("wrong issuer")
        aud = claims.get("aud")
        audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
        if self.audience not in audiences:
            raise CFAccessError("wrong audience")
        exp = _number(claims.get("exp"))
        if exp is None:
            raise CFAccessError("token has no expiry")
        if now > exp + self._leeway:
            raise CFAccessError("token expired")
        if "nbf" in claims:
            nbf = _number(claims.get("nbf"))
            if nbf is None or now < nbf - self._leeway:
                raise CFAccessError("token not valid yet")

    async def _key(self, kid: str) -> rsa.RSAPublicKey:
        async with self._lock:
            now = self._clock()
            stale = self._fetched_at is None or now - self._fetched_at >= self._cache_seconds
            if stale and self._may_fetch(now):
                await self._refresh(now)
            key = self._keys.get(kid)
            if key is None and self._may_fetch(now):
                # Unknown kid: the team's keys may have rotated. Refetch once (throttled).
                await self._refresh(now)
                key = self._keys.get(kid)
        if key is None:
            raise CFAccessError("unknown signing key")
        return key

    def _may_fetch(self, now: float) -> bool:
        return self._attempted_at is None or now - self._attempted_at >= self._min_refetch

    async def _refresh(self, now: float) -> None:
        self._attempted_at = now
        fetcher = self._fetcher or fetch_jwks
        try:
            keys = parse_jwks(await fetcher(self.certs_url))
        except Exception as exc:  # network, HTTP or JSON errors: keep the previous keys
            log.warning(
                "Cloudflare Access: cannot fetch signing keys from %s: %s", self.certs_url, exc
            )
            return
        if not keys:
            log.warning("Cloudflare Access: no usable RS256 keys at %s", self.certs_url)
            return
        self._keys = keys
        self._fetched_at = now
