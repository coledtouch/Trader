"""Phone alerts: Web Push to the installed app (trader.webpush) and/or ntfy
(https://ntfy.sh or a self-hosted server). Both optional; an alert goes to every channel
that is set up.

`send` never raises: a notification failure must not interrupt trading. The ntfy topic
URL and access token are secrets (anyone with the topic can read it), so neither is
ever logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable

import httpx

from trader.config import AppConfig
from trader.db import Database
from trader.webpush import WebPush

log = logging.getLogger(__name__)

TIMEOUT_S = 5.0
DUPLICATE_WINDOW_S = 60.0  # the same title is sent at most once per window
PRIORITIES = frozenset({"min", "low", "default", "high", "max", "urgent"})
MAX_TITLE = 120
MAX_MESSAGE = 3500


def _header_text(text: str, limit: int) -> str:
    """HTTP header values must be single-line ASCII: drop control characters and replace
    anything non-ASCII."""
    clean = "".join(ch if ch.isprintable() else " " for ch in text)
    return clean.encode("ascii", "replace").decode("ascii").strip()[:limit]


class Notifier:
    def __init__(
        self,
        config: AppConfig,
        db: Database | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = (config.ntfy_url or "").strip()
        self._token = config.ntfy_token
        self._transport = transport
        self._clock = clock
        self._push = WebPush(config, db, transport=transport) if db is not None else None
        self._last_sent: dict[str, float] = {}
        self._inflight: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def ntfy_enabled(self) -> bool:
        return bool(self._url)

    @property
    def push_enabled(self) -> bool:
        return self._push is not None and self._push.enabled

    @property
    def enabled(self) -> bool:
        return self.ntfy_enabled or self.push_enabled

    async def send(
        self,
        title: str,
        message: str,
        priority: str = "default",
        tags: Iterable[str] | None = None,
    ) -> bool:
        """Publish a notification on every channel. Returns True when at least one channel
        accepted it; False when none is set up, rate limited (same title within 60 s) or
        every channel failed (logged)."""
        if not self._url and self._push is None:
            return False
        async with self._lock:
            now = self._clock()
            self._last_sent = {
                t: ts for t, ts in self._last_sent.items() if now - ts < DUPLICATE_WINDOW_S
            }
            if title in self._last_sent or title in self._inflight:
                log.debug("notification %r suppressed (recently sent or in flight)", title)
                return False
            self._inflight.add(title)
        try:
            channels = []
            if self._url:
                channels.append(self._send_ntfy(title, message, priority, tags))
            if self._push is not None:
                channels.append(self._push.send(title, message, priority, tags))
            results = await asyncio.gather(*channels, return_exceptions=True)
            sent = any(result is True for result in results)
            if sent:
                async with self._lock:
                    self._last_sent[title] = self._clock()
            return sent
        finally:
            async with self._lock:
                self._inflight.discard(title)

    async def _send_ntfy(
        self, title: str, message: str, priority: str, tags: Iterable[str] | None
    ) -> bool:
        headers = {
            "Title": _header_text(title, MAX_TITLE) or "AI Trader",
            "Priority": priority if priority in PRIORITIES else "default",
        }
        tag_list = [t for t in (_header_text(tag, 40) for tag in tags or ()) if t]
        if tag_list:
            headers["Tags"] = ",".join(tag_list)
        if self._token is not None and self._token.get_secret_value():
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=self._transport) as client:
                response = await client.post(
                    self._url, content=message[:MAX_MESSAGE].encode("utf-8"), headers=headers
                )
            if not response.is_success:
                log.warning("ntfy rejected notification %r: HTTP %d", title, response.status_code)
                return False
            return True
        except Exception as exc:  # never raise: notifications are best effort
            log.warning("ntfy notification %r failed: %s", title, type(exc).__name__)
            return False
