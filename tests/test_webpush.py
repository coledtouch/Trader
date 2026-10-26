"""Web Push: message encryption, VAPID signing, subscription checks, delivery."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

from trader import webpush
from trader.config import AppConfig
from trader.db import Database
from trader.notify import Notifier
from trader.security import crypto

FCM = "https://fcm.googleapis.com/fcm/send/abc123:APA91bExample"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Device:
    """A browser's side of a subscription: its key pair and auth secret."""

    def __init__(self, endpoint: str = FCM) -> None:
        self.endpoint = endpoint
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.auth = b"0123456789abcdef"

    @property
    def keys(self) -> tuple[str, str]:
        return b64(self.public), b64(self.auth)

    def decrypt(self, body: bytes) -> bytes:
        """RFC 8291 section 3.4 and RFC 8188, written from the RFCs (not with http_ece) so the
        test checks the app's messages against the standard, not against itself."""
        salt, rs, idlen = body[:16], struct.unpack(">I", body[16:20])[0], body[20]
        sender_public = body[21 : 21 + idlen]
        ciphertext = body[21 + idlen :]
        assert idlen == 65 and rs >= 18 and len(ciphertext) <= rs

        def hmac256(key: bytes, data: bytes) -> bytes:
            return hmac.new(key, data, hashlib.sha256).digest()

        sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_public)
        ecdh = self.key.exchange(ec.ECDH(), sender)
        prk_key = hmac256(self.auth, ecdh)
        ikm = hmac256(prk_key, b"WebPush: info\x00" + self.public + sender_public + b"\x01")
        prk = hmac256(salt, ikm)
        cek = hmac256(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
        nonce = hmac256(prk, b"Content-Encoding: nonce\x00\x01")[:12]
        plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
        plain = plain.rstrip(b"\x00")
        assert plain.endswith(b"\x02")  # the last (only) record's delimiter
        return plain[:-1]


def config(**overrides) -> AppConfig:
    values = {"secret_key": SecretStr(crypto.generate_secret_key())}
    values.update(overrides)
    return AppConfig(_env_file=None, **values)


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


def subscribe(db: Database, device: Device, label: str = "Android · Chrome") -> str:
    endpoint, p256dh, auth = webpush.validate_subscription(device.endpoint, *device.keys)
    sub_id = webpush.subscription_id(endpoint)
    db.save_push_subscription(sub_id, endpoint, p256dh, auth, label)
    return sub_id


# ---------------------------------------------------------------------- encryption
def test_messages_decrypt_per_rfc_8291_with_a_fresh_key_and_salt_each_time() -> None:
    device = Device()
    payload = json.dumps({"title": "Bought AAPL", "body": "2 shares"}).encode()
    first = webpush.encrypt_payload(payload, *device.keys)
    second = webpush.encrypt_payload(payload, *device.keys)
    assert device.decrypt(first) == payload
    assert device.decrypt(second) == payload
    assert first[:16] != second[:16] and first[21:86] != second[21:86]  # salt, sender key


def test_vapid_header_is_a_valid_es256_token_for_the_push_service() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    header = webpush.vapid_authorization(key, FCM, "https://desk.example", now=1_000_000)
    assert header.startswith("vapid t=")
    token, k = header[len("vapid t=") :].split(", k=")
    assert k == webpush.public_key_b64(key)
    head, claims, signature = token.split(".")
    assert json.loads(unb64(head)) == {"typ": "JWT", "alg": "ES256"}
    body = json.loads(unb64(claims))
    assert body == {
        "aud": "https://fcm.googleapis.com",
        "exp": 1_000_000 + webpush.JWT_LIFETIME_S,
        "sub": "https://desk.example",
    }
    assert webpush.JWT_LIFETIME_S <= 24 * 3600
    raw = unb64(signature)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.public_key().verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_vapid_subject_is_the_public_https_origin() -> None:
    assert webpush.vapid_subject(config(public_origins="https://desk.example")) == (
        "https://desk.example"
    )
    assert webpush.vapid_subject(config()).startswith("mailto:")


# ---------------------------------------------------------------------- subscriptions
@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/x",
        "https://updates.push.services.mozilla.com/wpush/v2/x",
        "https://wns2-par02p.notify.windows.com/w/?token=x",
        "https://web.push.apple.com/x",
    ],
)
def test_known_push_services_are_accepted(endpoint: str) -> None:
    device = Device(endpoint)
    assert webpush.validate_subscription(endpoint, *device.keys)[0] == endpoint


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/x",  # not https
        "https://fcm.googleapis.com.evil.example/x",
        "https://evilfcm.googleapis.com.example/x",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/x",
        "https://ib-gateway:4004/x",
        "https://user:pw@fcm.googleapis.com/x",
        "https://fcm.googleapis.com:8443/x",
        "https://[::1]/x",
        "",
        "https://fcm.googleapis.com/" + "x" * 3000,
    ],
)
def test_other_endpoints_are_refused_so_the_server_only_posts_to_push_services(
    endpoint: str,
) -> None:
    with pytest.raises(ValueError, match="push") as info:
        webpush.validate_subscription(endpoint, *Device().keys)
    assert "evil" not in str(info.value) and "169.254" not in str(info.value)  # not echoed


def test_bad_keys_are_refused() -> None:
    device = Device()
    p256dh, auth = device.keys
    for bad_key in ("", "not base64!", b64(b"\x04" + b"\x01" * 64), b64(device.public[:33])):
        with pytest.raises(ValueError, match="public key"):
            webpush.validate_subscription(FCM, bad_key, auth)
    for bad_auth in ("", b64(b"short"), b64(b"x" * 32)):
        with pytest.raises(ValueError, match="auth secret"):
            webpush.validate_subscription(FCM, p256dh, bad_auth)
    padded = webpush.validate_subscription(FCM, p256dh + "=", auth + "==")
    assert padded == (FCM, p256dh, auth)  # normalized


def test_device_labels() -> None:
    android = (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Mobile Safari/537.36"
    )
    edge = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
    )
    assert webpush.device_label(android) == "Android · Chrome"
    assert webpush.device_label(edge) == "Windows · Edge"
    assert webpush.device_label(None) == "Unknown device"


# ---------------------------------------------------------------------- VAPID key
def test_vapid_key_is_created_once_stored_encrypted_and_shared(db: Database) -> None:
    cfg = config()
    assert webpush.load_vapid_key(cfg, db) is None  # not created by a sender
    key = webpush.load_vapid_key(cfg, db, create=True)
    stored = db.get_kv(webpush.VAPID_KV_KEY)
    assert key is not None and stored is not None and "PRIVATE KEY" not in stored
    again = webpush.load_vapid_key(cfg, db, create=True)
    assert again is not None and webpush.public_key_b64(again) == webpush.public_key_b64(key)
    with pytest.raises(crypto.SecretKeyError):
        webpush.load_vapid_key(config(), db)  # a different TRADER_SECRET_KEY cannot read it
    assert webpush.load_vapid_key(config(secret_key=SecretStr("")), db, create=True) is None


def test_a_key_stored_by_another_process_first_wins(db: Database) -> None:
    cfg = config()
    other = ec.generate_private_key(ec.SECP256R1())
    pem = other.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    db.add_kv_if_absent(webpush.VAPID_KV_KEY, crypto.encrypt(cfg.secret_key, pem))
    db.add_kv_if_absent(webpush.VAPID_KV_KEY, "ignored")
    key = webpush.load_vapid_key(cfg, db, create=True)
    assert key is not None and webpush.public_key_b64(key) == webpush.public_key_b64(other)


# ---------------------------------------------------------------------- payload
def test_payload_has_an_emoji_title_and_fits_a_push_message() -> None:
    payload = json.loads(webpush.build_payload("Halted", "Daily loss limit", "high", ["warning"]))
    assert payload["title"] == "⚠️ Halted" and payload["body"] == "Daily loss limit"
    assert payload["priority"] == "high" and payload["ts"]
    huge = webpush.build_payload("x" * 500, "é" * 5000)
    assert len(huge) <= webpush.MAX_PAYLOAD_BYTES
    data = json.loads(huge)
    assert len(data["title"]) == webpush.MAX_TITLE
    assert json.loads(webpush.build_payload("  ", ""))["title"] == "AI Trader"


# ---------------------------------------------------------------------- delivery
def recorder(status: dict[str, int]):
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(status.get(str(request.url), 201))

    return sent, handler


async def test_send_delivers_an_encrypted_signed_message_to_every_device(db: Database) -> None:
    cfg = config(public_origins="https://desk.example")
    webpush.load_vapid_key(cfg, db, create=True)
    phone, laptop = Device(FCM), Device("https://wns2-par02p.notify.windows.com/w/?token=x")
    subscribe(db, phone)
    subscribe(db, laptop, "Windows · Edge")
    sent, handler = recorder({})
    push = webpush.WebPush(cfg, db, transport=httpx.MockTransport(handler))
    assert push.enabled
    assert await push.send("Bought AAPL", "2 shares at $250", "high", ["white_check_mark"])
    assert sorted(str(r.url) for r in sent) == sorted([phone.endpoint, laptop.endpoint])
    for request in sent:
        device = phone if str(request.url) == phone.endpoint else laptop
        assert request.headers["Content-Encoding"] == "aes128gcm"
        assert request.headers["Urgency"] == "high"
        assert int(request.headers["TTL"]) == webpush.TTL_S
        assert request.headers["Authorization"].startswith("vapid t=")
        data = json.loads(device.decrypt(request.content))
        assert data["title"] == "✅ Bought AAPL" and data["body"] == "2 shares at $250"
    assert all(d["last_ok_at"] for d in db.list_push_subscriptions())


async def test_a_gone_device_is_removed_and_a_failing_one_is_counted(db: Database) -> None:
    cfg = config()
    webpush.load_vapid_key(cfg, db, create=True)
    gone, failing, fine = Device(FCM + "gone"), Device(FCM + "fail"), Device(FCM + "ok")
    gone_id, failing_id = subscribe(db, gone), subscribe(db, failing)
    subscribe(db, fine)
    sent, handler = recorder({gone.endpoint: 410, failing.endpoint: 500})
    push = webpush.WebPush(cfg, db, transport=httpx.MockTransport(handler))
    assert await push.send("Halted", "x")  # one device still got it
    devices = {d["id"]: d for d in db.list_push_subscriptions()}
    assert gone_id not in devices
    assert devices[failing_id]["failures"] == 1 and devices[failing_id]["last_ok_at"] is None
    sent.clear()
    assert await push.send("Only one", "x", only=failing_id) is False
    assert [str(r.url) for r in sent] == [failing.endpoint]


async def test_send_never_raises(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    cfg = config()
    webpush.load_vapid_key(cfg, db, create=True)
    device = Device()
    subscribe(db, device)

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    push = webpush.WebPush(cfg, db, transport=httpx.MockTransport(broken))
    assert await push.send("Halted", "x") is False
    assert device.endpoint not in caplog.text  # endpoints are never logged
    assert db.list_push_subscriptions()[0]["failures"] == 1
    # Without TRADER_SECRET_KEY there is nothing to sign with: disabled, not an error.
    assert not webpush.WebPush(config(secret_key=SecretStr("")), db).enabled
    assert await webpush.WebPush(config(secret_key=SecretStr("")), db).send("x", "y") is False


async def test_notifier_sends_on_both_channels_once(db: Database) -> None:
    topic = "https://ntfy.sh/secret-topic"
    cfg = config(ntfy_url=topic)
    webpush.load_vapid_key(cfg, db, create=True)
    device = Device()
    subscribe(db, device)
    sent, handler = recorder({topic: 500})  # ntfy is down; push still delivers
    notifier = Notifier(cfg, db, transport=httpx.MockTransport(handler))
    assert notifier.enabled and notifier.push_enabled and notifier.ntfy_enabled
    assert await notifier.send("Bought F", "10 shares")
    assert sorted(str(r.url) for r in sent) == sorted([topic, device.endpoint])
    sent.clear()
    assert await notifier.send("Bought F", "again") is False  # same title within 60 s
    assert sent == []
    assert not Notifier(config()).enabled  # no ntfy, no database: no channel


async def test_alerts_are_sent_urgent_unless_marked_low(db: Database) -> None:
    cfg = config()
    webpush.load_vapid_key(cfg, db, create=True)
    subscribe(db, Device())
    sent, handler = recorder({})
    push = webpush.WebPush(cfg, db, transport=httpx.MockTransport(handler))
    for priority in ("default", "min", "low", "urgent"):
        assert await push.send(f"t-{priority}", "x", priority)
    # "default" covers approval requests that expire in 10 minutes: never held in Doze.
    assert [r.headers["Urgency"] for r in sent] == ["high", "low", "normal", "high"]


# ---------------------------------------------------------------------- resets
def test_reset_forgets_the_key_and_every_device(db: Database) -> None:
    cfg = config()
    old = webpush.load_vapid_key(cfg, db, create=True)
    sub_id = subscribe(db, Device())
    assert webpush.push_key_matches(cfg, db) is True
    assert webpush.resubscribe_ids(db) == []
    assert webpush.reset_push(db) == 1
    assert db.list_push_subscriptions() == [] and db.get_kv(webpush.VAPID_KV_KEY) is None
    # The devices subscribed at the reset subscribe again on their own; others stay off.
    assert webpush.resubscribe_ids(db) == [sub_id]
    assert webpush.push_key_matches(cfg, db) is None
    new = webpush.load_vapid_key(cfg, db, create=True)
    assert new is not None and old is not None
    assert webpush.public_key_b64(new) != webpush.public_key_b64(old)


def test_a_replaced_secret_key_resets_push_instead_of_disabling_it_for_good(db: Database) -> None:
    old_cfg, new_cfg = config(), config()
    webpush.load_vapid_key(old_cfg, db, create=True)
    subscribe(db, Device())
    assert webpush.reset_if_unreadable(old_cfg, db) is None  # still readable: kept
    assert webpush.push_key_matches(new_cfg, db) is False
    assert not webpush.WebPush(new_cfg, db).enabled
    assert webpush.reset_if_unreadable(new_cfg, db) == 1
    assert webpush.load_vapid_key(new_cfg, db, create=True) is not None


def _cli_env(tmp_path, monkeypatch, key: str):
    from trader.security.auth import Authenticator, new_totp_secret

    env_file = tmp_path / ".env"
    env_file.write_text(f"TRADER_SECRET_KEY={key}\n", encoding="utf-8")
    monkeypatch.delenv("TRADER_SECRET_KEY", raising=False)
    monkeypatch.setenv("TRADER_DATA_DIR", str(tmp_path / "data"))
    cfg = AppConfig(_env_file=env_file)
    database = Database(cfg.db_path)
    from tests.test_auth import FAST, PASSWORD

    Authenticator(cfg, database, hasher=FAST).set_owner(PASSWORD, new_totp_secret())
    return env_file, cfg, database


def test_cli_removes_one_lost_device_and_resets_all(tmp_path, monkeypatch, capsys):
    from trader import cli

    env_file, cfg, database = _cli_env(tmp_path, monkeypatch, crypto.generate_secret_key())
    try:
        webpush.load_vapid_key(cfg, database, create=True)
        phone = subscribe(database, Device(FCM + "phone"), "Android · Chrome")
        laptop = subscribe(database, Device(FCM + "laptop"), "Windows · Edge")
        # logout-all signs out sessions only: the laptop must keep its alerts while the
        # lost phone's 2FA cannot be replaced yet. It points at the per-device commands.
        assert cli.main(["logout-all", "--env-file", str(env_file)]) == 0
        assert "push-remove" in capsys.readouterr().out
        assert len(database.list_push_subscriptions()) == 2

        assert cli.main(["push-devices", "--env-file", str(env_file)]) == 0
        listed = capsys.readouterr().out
        assert phone in listed and "Android · Chrome" in listed and laptop in listed
        assert FCM not in listed  # endpoints are never printed
        assert cli.main(["push-remove", phone, "--env-file", str(env_file)]) == 0
        assert [d["id"] for d in database.list_push_subscriptions()] == [laptop]
        assert database.list_audit(1)[0]["action"] == "push_device_removed"
        assert cli.main(["push-remove", phone, "--env-file", str(env_file)]) == 1
        assert "no device" in capsys.readouterr().err

        assert cli.main(["push-reset", "--env-file", str(env_file)]) == 0
        assert "1 device(s) removed" in capsys.readouterr().out
        assert database.get_kv(webpush.VAPID_KV_KEY) is None
        assert database.list_push_subscriptions() == []
        assert webpush.resubscribe_ids(database) == [laptop]
        assert database.list_audit(1)[0]["action"] == "push_reset"
    finally:
        database.close()


def test_cli_reset_2fa_with_a_new_secret_key_resets_push(tmp_path, monkeypatch, capsys):
    from trader import cli

    env_file, cfg, database = _cli_env(tmp_path, monkeypatch, crypto.generate_secret_key())
    try:
        webpush.load_vapid_key(cfg, database, create=True)
        subscribe(database, Device())
        env_file.write_text(f"TRADER_SECRET_KEY={crypto.generate_secret_key()}\n", "utf-8")
        # A wrong key (2FA does not decrypt either): restore it, do not reset anything.
        assert cli.main(["status", "--env-file", str(env_file)]) == 0
        status = capsys.readouterr().out
        assert "restore the original TRADER_SECRET_KEY" in status and "push-reset" not in status
        out = tmp_path / "totp.txt"
        assert (
            cli.main(["reset-2fa", "--env-file", str(env_file), "--totp-secret-out", str(out)]) == 0
        )
        assert "Phone alerts were reset" in capsys.readouterr().out
        assert database.get_kv(webpush.VAPID_KV_KEY) is None
        assert database.list_push_subscriptions() == []
    finally:
        database.close()


def test_cli_status_suggests_push_reset_only_when_the_key_is_right(tmp_path, monkeypatch, capsys):
    from trader import cli

    old_key = crypto.generate_secret_key()
    env_file, cfg, database = _cli_env(tmp_path, monkeypatch, crypto.generate_secret_key())
    try:
        # 2FA decrypts with the current key; the push key was made under an older one.
        old = config(secret_key=SecretStr(old_key))
        webpush.load_vapid_key(old, database, create=True)
        assert cli.main(["status", "--env-file", str(env_file)]) == 0
        assert "older TRADER_SECRET_KEY (run push-reset)" in capsys.readouterr().out
    finally:
        database.close()
