"""Push notifications through ntfy (https://ntfy.sh or a self-hosted server). Optional.

`send` never raises: a notification failure must not interrupt trading. The topic URL
and the access token are secrets (anyone with the topic can read it), so neither is
ever logged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable

import httpx

from trader.config import AppConfig

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
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = (config.ntfy_url or "").strip()
        self._token = config.ntfy_token
        self._transport = transport
        self._clock = clock
        self._last_sent: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def send(
        self,
        title: str,
        message: str,
        priority: str = "default",
        tags: Iterable[str] | None = None,
    ) -> bool:
        """Publish a notification. Returns True when ntfy accepted it; False when disabled,
        rate limited (same title within 60 s) or failed (logged)."""
        if not self._url:
            return False
        now = self._clock()
        last = self._last_sent.get(title)
        if last is not None and now - last < DUPLICATE_WINDOW_S:
            log.debug("notification %r suppressed (sent %.0fs ago)", title, now - last)
            return False
        self._last_sent = {
            t: ts for t, ts in self._last_sent.items() if now - ts < DUPLICATE_WINDOW_S
        }
        self._last_sent[title] = now

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
            if response.status_code >= 400:
                log.warning("ntfy rejected notification %r: HTTP %d", title, response.status_code)
                return False
            return True
        except Exception as exc:  # never raise: notifications are best effort
            log.warning("ntfy notification %r failed: %s", title, type(exc).__name__)
            return False
