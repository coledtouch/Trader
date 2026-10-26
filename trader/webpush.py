"""Web Push: alerts delivered by the browser's own push service to the installed app.

The owner turns alerts on per device in Settings. The browser hands over a subscription
(an endpoint URL at its push service plus two keys) and the server keeps it. For each
alert the server encrypts a small JSON message for that device (RFC 8291, aes128gcm, via
the reference `http_ece` library), signs the request with the app's VAPID key (RFC 8292)
and posts it to the endpoint. The push service wakes the service worker, which shows the
notification, even when the app is closed.

The VAPID private key is created on first use, stored encrypted with TRADER_SECRET_KEY
and shared by the app and the crypto watchdog through the database. An endpoint is a
capability URL (anyone holding it can message that device), so endpoints are never
logged and the API only exposes a short hash of each one.

`WebPush.send` never raises: an alert failure must not interrupt trading.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import http_ece
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from trader.config import AppConfig
from trader.db import Database
from trader.security import crypto

log = logging.getLogger(__name__)

VAPID_KV_KEY = "push_vapid_private"
MAX_DEVICES = 10
TIMEOUT_S = 10.0
TTL_S = 12 * 3600  # an alert older than this is not worth waking the phone for
JWT_LIFETIME_S = 12 * 3600  # push services reject VAPID tokens valid for more than 24 h
MAX_TITLE = 120
MAX_BODY = 1500
MAX_PAYLOAD_BYTES = 3000  # a push message is at most 4 KB once encrypted
MAX_ENDPOINT = 2048
# Push services of the browsers the owner uses: Chrome and Android (FCM), Firefox, Edge on
# Windows, Safari. The server only ever posts to these hosts, so a subscription cannot turn
# it into a client for an internal address.
PUSH_HOSTS = (
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "notify.windows.com",
    "push.apple.com",
)
# Every alert is a visible notification and they are rare, so they go out "high" (FCM then
# delivers at once even to a phone in Doze); only explicitly low ones may wait.
URGENCY = {"min": "low", "low": "normal"}
EMOJI = {
    "rotating_light": "🚨",
    "warning": "⚠️",
    "stop_sign": "🛑",
    "white_check_mark": "✅",
    "question": "❓",
}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Decode base64url with or without padding. Raises ValueError."""
    if not isinstance(text, str) or not text or len(text) > 256:
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise ValueError("not base64url") from None


def subscription_id(endpoint: str) -> str:
    """A short, stable name for a device that does not reveal its endpoint."""
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]


def validate_subscription(endpoint: str, p256dh: str, auth: str) -> tuple[str, str, str]:
    """Check a browser's subscription. Returns it normalized; raises ValueError (the
    message is safe to show and never contains the endpoint)."""
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > MAX_ENDPOINT:
        raise ValueError("the push endpoint is missing or too long")
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        raise ValueError("the push endpoint is not a valid URL") from None
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or not any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)
    ):
        raise ValueError("this browser's push service is not supported")
    try:
        key = b64url_decode(p256dh)
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), key)
    except ValueError:
        raise ValueError("the subscription's public key is invalid") from None
    if len(key) != 65:
        raise ValueError("the subscription's public key is invalid")
    try:
        secret = b64url_decode(auth)
    except ValueError:
        raise ValueError("the subscription's auth secret is invalid") from None
    if len(secret) != 16:
        raise ValueError("the subscription's auth secret is invalid")
    return endpoint, b64url(key), b64url(secret)


def device_label(user_agent: str | None) -> str:
    """'Android · Chrome', 'Windows · Edge' and so on, from the User-Agent header."""
    ua = user_agent or ""
    if "Android" in ua:
        system = "Android"
    elif "iPhone" in ua or "iPad" in ua:
        system = "iPhone" if "iPhone" in ua else "iPad"
    elif "Windows" in ua:
        system = "Windows"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        system = "Mac"
    elif "Linux" in ua:
        system = "Linux"
    else:
        system = "Unknown device"
    if "Edg/" in ua or "EdgA/" in ua:
        browser = "Edge"
    elif "SamsungBrowser/" in ua:
        browser = "Samsung Internet"
    elif "Firefox/" in ua or "FxiOS/" in ua:
        browser = "Firefox"
    elif "Chrome/" in ua or "CriOS/" in ua:
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = ""
    return f"{system} · {browser}" if browser else system


# ---------------------------------------------------------------------- VAPID key
def _secret_key_set(config: AppConfig) -> bool:
    return bool(config.secret_key.get_secret_value().strip())


def load_vapid_key(
    config: AppConfig, db: Database, *, create: bool = False
) -> ec.EllipticCurvePrivateKey | None:
    """The app's VAPID signing key. None when TRADER_SECRET_KEY is not set, or when no key
    exists yet and `create` is False. Raises crypto.SecretKeyError when the stored key
    cannot be decrypted with TRADER_SECRET_KEY."""
    if not _secret_key_set(config):
        return None
    stored = db.get_kv(VAPID_KV_KEY)
    if stored is None:
        if not create:
            return None
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
        db.add_kv_if_absent(VAPID_KV_KEY, crypto.encrypt(config.secret_key, pem))
        stored = db.get_kv(VAPID_KV_KEY)  # another process may have won the race
        if stored is None:
            return None
    pem = crypto.decrypt(config.secret_key, stored)
    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise crypto.SecretKeyError("the stored push key is not an EC key")
    return key


def push_key_matches(config: AppConfig, db: Database) -> bool | None:
    """None: no key yet (or no TRADER_SECRET_KEY). False: the stored key cannot be read
    with the current TRADER_SECRET_KEY (it was replaced), so push needs a reset."""
    try:
        return None if load_vapid_key(config, db) is None else True
    except crypto.SecretKeyError:
        return False


def reset_push(db: Database) -> int:
    """Forget the push key and every device. The next visit to Settings creates a new key;
    each device turns alerts on again. A browser's old subscription is dropped when it
    sees the new key, so whoever held the old key can no longer reach it. Returns the
    number of devices removed."""
    db.delete_kv(VAPID_KV_KEY)
    return db.delete_all_push_subscriptions()


def reset_if_unreadable(config: AppConfig, db: Database) -> int | None:
    """After TRADER_SECRET_KEY changed: reset push if its key no longer decrypts. Returns
    the number of devices removed, or None when nothing needed resetting."""
    if push_key_matches(config, db) is False:
        return reset_push(db)
    return None


def public_key_b64(key: ec.EllipticCurvePrivateKey) -> str:
    """The applicationServerKey the browser subscribes with (uncompressed P-256 point)."""
    raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64url(raw)


def vapid_subject(config: AppConfig) -> str:
    """Contact for the push service operator: the app's public https origin if known."""
    for origin in config.public_origins_list:
        if origin.startswith("https://"):
            return origin
    return "mailto:webpush@localhost"


def vapid_authorization(
    key: ec.EllipticCurvePrivateKey, endpoint: str, subject: str, now: float
) -> str:
    """`Authorization: vapid t=<ES256 JWT>, k=<public key>` for one push service."""
    parts = urlsplit(endpoint)
    header = {"typ": "JWT", "alg": "ES256"}
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int(now) + JWT_LIFETIME_S,
        "sub": subject,
    }
    signing_input = (
        b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    der = key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"vapid t={signing_input}.{b64url(signature)}, k={public_key_b64(key)}"


# ---------------------------------------------------------------------- messages
def build_payload(
    title: str,
    message: str,
    priority: str = "default",
    tags: Iterable[str] | None = None,
    *,
    now: datetime | None = None,
) -> bytes:
    """The JSON the service worker turns into a notification, at most MAX_PAYLOAD_BYTES."""
    emoji = next((EMOJI[t] for t in tags or () if t in EMOJI), "")
    clean_title = " ".join((title or "").split())[:MAX_TITLE] or "AI Trader"
    body = (message or "").strip()[:MAX_BODY]
    data: dict[str, Any] = {
        "title": f"{emoji} {clean_title}" if emoji else clean_title,
        "body": body,
        "priority": priority,
        "ts": (now or datetime.now(UTC)).isoformat(),
    }
    encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
    while len(encoded) > MAX_PAYLOAD_BYTES and data["body"]:
        data["body"] = data["body"][: max(0, len(data["body"]) - 200)].rstrip() + "…"
        if len(data["body"]) <= 1:
            data["body"] = ""
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return encoded


def encrypt_payload(payload: bytes, p256dh: str, auth: str) -> bytes:
    """aes128gcm body for one device (RFC 8291), with a fresh sender key and salt."""
    sender = ec.generate_private_key(ec.SECP256R1())
    return http_ece.encrypt(
        payload,
        private_key=sender,
        dh=b64url_decode(p256dh),
        auth_secret=b64url_decode(auth),
        version="aes128gcm",
    )


class WebPush:
    """Sends alerts to every subscribed device. Thread-safe enough for one event loop."""

    def __init__(
        self,
        config: AppConfig,
        db: Database,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._db = db
        self._transport = transport
        self._clock = clock
        self._key_error_logged = False

    @property
    def enabled(self) -> bool:
        """At least one device subscribed (and a key to sign with)."""
        try:
            return self._db.count_push_subscriptions() > 0 and self._key() is not None
        except Exception:  # never raise from a status check
            return False

    def _key(self) -> ec.EllipticCurvePrivateKey | None:
        try:
            return load_vapid_key(self._config, self._db)
        except crypto.SecretKeyError:
            if not self._key_error_logged:
                log.warning(
                    "phone push disabled: the stored push key does not match TRADER_SECRET_KEY. "
                    "Run `python -m trader push-reset` (or Reset in Settings > Phone alerts), "
                    "then turn alerts on again on each device"
                )
                self._key_error_logged = True
            return None

    async def send(
        self,
        title: str,
        message: str,
        priority: str = "default",
        tags: Iterable[str] | None = None,
        *,
        only: str | None = None,
    ) -> bool:
        """Deliver to every device (or only the device with id `only`). True when at least
        one push service accepted the message."""
        try:
            devices = self._db.list_push_subscriptions()
            if only is not None:
                devices = [d for d in devices if d["id"] == only]
            if not devices:
                return False
            key = self._key()
            if key is None:
                return False
            payload = build_payload(title, message, priority, tags)
        except Exception as exc:  # never raise: alerts are best effort
            log.warning("phone push %r failed: %s", title, type(exc).__name__)
            return False
        headers = {
            "TTL": str(TTL_S),
            "Urgency": URGENCY.get(priority, "high"),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
        }
        subject = vapid_subject(self._config)
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT_S, transport=self._transport, follow_redirects=False
            ) as client:
                results = await asyncio.gather(
                    *(
                        self._deliver(client, key, subject, headers, payload, d, title)
                        for d in devices
                    )
                )
        except Exception as exc:
            log.warning("phone push %r failed: %s", title, type(exc).__name__)
            return False
        return any(results)

    async def _deliver(
        self,
        client: httpx.AsyncClient,
        key: ec.EllipticCurvePrivateKey,
        subject: str,
        headers: dict[str, str],
        payload: bytes,
        device: dict[str, Any],
        title: str,
    ) -> bool:
        label = device.get("label") or "a device"
        try:
            body = encrypt_payload(payload, device["p256dh"], device["auth"])
            auth = vapid_authorization(key, device["endpoint"], subject, self._clock())
            response = await client.post(
                device["endpoint"], content=body, headers={**headers, "Authorization": auth}
            )
        except Exception as exc:
            log.warning("phone push %r to %s failed: %s", title, label, type(exc).__name__)
            self._note(device["id"], ok=False)
            return False
        if response.status_code in (200, 201, 202):
            self._note(device["id"], ok=True)
            return True
        if response.status_code in (404, 410):
            # The browser dropped this subscription (app uninstalled, permission revoked,
            # data cleared). It will never work again.
            log.info("phone push: %s is no longer subscribed; removed it", label)
            try:
                self._db.delete_push_subscription(device["id"])
            except Exception as exc:
                log.warning("phone push: could not remove %s: %s", label, type(exc).__name__)
            return False
        log.warning("phone push %r to %s rejected: HTTP %d", title, label, response.status_code)
        self._note(device["id"], ok=False)
        return False

    def _note(self, sub_id: str, *, ok: bool) -> None:
        try:
            self._db.note_push_result(sub_id, ok)
        except Exception as exc:
            log.warning("phone push: could not record a result: %s", type(exc).__name__)
