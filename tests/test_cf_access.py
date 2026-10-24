import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from trader.security.cf_access import CFAccessError, CFAccessVerifier, parse_jwks

NOW = int(time.time())  # fixed "now" for tests that check time claims
TEAM = "team.cloudflareaccess.com"
ISSUER = f"https://{TEAM}"
AUD = "aud-tag-1234567890"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(key: rsa.RSAPrivateKey, kid: str) -> dict:
    numbers = key.public_key().public_numbers()
    n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    return {"kty": "RSA", "alg": "RS256", "use": "sig", "kid": kid, "n": b64url(n), "e": b64url(e)}


def make_token(
    key: rsa.RSAPrivateKey,
    kid: str = "k1",
    alg: str = "RS256",
    now: float | None = None,
    **claims: object,
) -> str:
    now = int(time.time() if now is None else now)
    body = {"iss": ISSUER, "aud": [AUD], "exp": now + 300, "nbf": now - 5, "iat": now - 5}
    body.update(claims)
    header = b64url(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    payload = b64url(json.dumps(body).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{b64url(signature)}"


class FakeJwks:
    """Stub for the JWKS fetch: returns the current key set and counts calls."""

    def __init__(self, *keys: dict):
        self.keys = list(keys)
        self.calls: list[str] = []
        self.fail = False

    async def __call__(self, url: str) -> dict:
        self.calls.append(url)
        if self.fail:
            raise OSError("network down")
        return {"keys": list(self.keys)}


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return new_key()


def verifier(fetcher, clock=None) -> CFAccessVerifier:
    return CFAccessVerifier(TEAM, AUD, fetcher=fetcher, clock=clock or time.time)


async def test_valid_token(key):
    fetch = FakeJwks(jwk(key, "k1"))
    claims = await verifier(fetch).verify(make_token(key, email="owner@example.com"))
    assert claims["email"] == "owner@example.com"
    assert fetch.calls == [f"{ISSUER}/cdn-cgi/access/certs"]


async def test_audience_may_be_a_string(key):
    fetch = FakeJwks(jwk(key, "k1"))
    await verifier(fetch).verify(make_token(key, aud=AUD))


async def test_keys_are_cached(key):
    fetch = FakeJwks(jwk(key, "k1"))
    v = verifier(fetch)
    for _ in range(3):
        await v.verify(make_token(key))
    assert len(fetch.calls) == 1


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ({"aud": ["someone-else"]}, "audience"),
        ({"aud": "someone-else"}, "audience"),
        ({"iss": "https://evil.cloudflareaccess.com"}, "issuer"),
        ({"exp": NOW - 120}, "expired"),
        ({"exp": None}, "expiry"),
        ({"nbf": NOW + 300}, "not valid yet"),
    ],
)
async def test_rejected_claims(key, claims, message):
    fetch = FakeJwks(jwk(key, "k1"))
    with pytest.raises(CFAccessError, match=message):
        await verifier(fetch, clock=lambda: NOW).verify(make_token(key, now=NOW, **claims))


async def test_leeway_of_sixty_seconds(key):
    fetch = FakeJwks(jwk(key, "k1"))
    v = verifier(fetch, clock=lambda: NOW)
    await v.verify(make_token(key, now=NOW, exp=NOW - 30))  # within leeway
    await v.verify(make_token(key, now=NOW, nbf=NOW + 30))
    with pytest.raises(CFAccessError, match="expired"):
        await v.verify(make_token(key, now=NOW, exp=NOW - 90))
    with pytest.raises(CFAccessError, match="not valid yet"):
        await v.verify(make_token(key, now=NOW, nbf=NOW + 90))


async def test_bad_signature(key):
    fetch = FakeJwks(jwk(key, "k1"))
    forged = make_token(new_key(), kid="k1")  # signed by a key that is not in the JWKS
    with pytest.raises(CFAccessError, match="signature"):
        await verifier(fetch).verify(forged)
    header, payload, signature = make_token(key).split(".")
    tampered_claims = b64url(json.dumps({"iss": ISSUER, "aud": [AUD], "exp": 2**40}).encode())
    with pytest.raises(CFAccessError, match="signature"):
        await verifier(fetch).verify(f"{header}.{tampered_claims}.{signature}")


@pytest.mark.parametrize("token", ["", "abc", "a.b", "a.b.c.d", "!!.@@.##", "e30.e30.e30"])
async def test_malformed_tokens(key, token):
    with pytest.raises(CFAccessError):
        await verifier(FakeJwks(jwk(key, "k1"))).verify(token)


async def test_other_algorithms_refused(key):
    header = b64url(json.dumps({"alg": "none", "kid": "k1"}).encode())
    payload = b64url(json.dumps({"iss": ISSUER, "aud": [AUD], "exp": 2**40}).encode())
    with pytest.raises(CFAccessError, match="algorithm"):
        await verifier(FakeJwks(jwk(key, "k1"))).verify(f"{header}.{payload}.")
    hs256 = make_token(key, alg="HS256")
    with pytest.raises(CFAccessError, match="algorithm"):
        await verifier(FakeJwks(jwk(key, "k1"))).verify(hs256)


async def test_unknown_kid_triggers_one_refetch(key):
    rotated = new_key()
    fetch = FakeJwks(jwk(key, "k1"))
    clock = Clock()
    v = verifier(fetch, clock)
    await v.verify(make_token(key, kid="k1", now=clock.now))
    assert len(fetch.calls) == 1

    # Cloudflare rotates its keys: the new kid is unknown to the cache -> refetch once.
    fetch.keys = [jwk(key, "k1"), jwk(rotated, "k2")]
    clock.now += 600
    await v.verify(make_token(rotated, kid="k2", now=clock.now))
    assert len(fetch.calls) == 2

    # A kid that is not in the JWKS at all fails after a single refetch, and refetches are
    # throttled so random kids cannot make the app hammer the JWKS endpoint.
    clock.now += 600
    with pytest.raises(CFAccessError, match="unknown signing key"):
        await v.verify(make_token(key, kid="nope", now=clock.now))
    assert len(fetch.calls) == 3
    with pytest.raises(CFAccessError, match="unknown signing key"):
        await v.verify(make_token(key, kid="nope", now=clock.now))
    assert len(fetch.calls) == 3


async def test_cache_expires_after_an_hour(key):
    fetch = FakeJwks(jwk(key, "k1"))
    clock = Clock()
    v = verifier(fetch, clock)
    await v.verify(make_token(key, now=clock.now))
    clock.now += 3601
    await v.verify(make_token(key, now=clock.now))
    assert len(fetch.calls) == 2


async def test_fetch_failure_keeps_previous_keys(key):
    fetch = FakeJwks(jwk(key, "k1"))
    clock = Clock()
    v = verifier(fetch, clock)
    await v.verify(make_token(key, now=clock.now))

    fetch.fail = True
    clock.now += 3601
    await v.verify(make_token(key, now=clock.now))  # stale but still the team's key


async def test_no_keys_fails_closed(key):
    fetch = FakeJwks(jwk(key, "k1"))
    fetch.fail = True
    with pytest.raises(CFAccessError, match="unknown signing key"):
        await verifier(fetch).verify(make_token(key))


def test_parse_jwks_skips_unusable_keys(key):
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)  # noqa: S505
    keys = parse_jwks(
        {
            "keys": [
                jwk(key, "good"),
                jwk(weak, "weak"),
                {**jwk(key, "enc"), "use": "enc"},
                {**jwk(key, "ps"), "alg": "PS256"},
                {"kty": "EC", "kid": "ec"},
                {"kty": "RSA", "kid": "broken", "n": "!!", "e": "AQAB"},
                "junk",
            ]
        }
    )
    assert list(keys) == ["good"]
    assert parse_jwks({"keys": "nope"}) == {}


def test_team_domain_is_normalized():
    v = CFAccessVerifier("https://Team.CloudflareAccess.com/", AUD, fetcher=FakeJwks())
    assert v.issuer == ISSUER
    assert v.certs_url == f"{ISSUER}/cdn-cgi/access/certs"
    with pytest.raises(ValueError):
        CFAccessVerifier("", AUD)
