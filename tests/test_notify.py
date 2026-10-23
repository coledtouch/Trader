"""Failed deliveries must remain retryable without duplicating concurrent alerts."""

import asyncio

import httpx
import pytest

from trader.config import AppConfig
from trader.notify import Notifier


@pytest.mark.parametrize("failure", ["connection", "server", "redirect"])
async def test_failed_delivery_can_retry_immediately(failure: str) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if failure == "connection":
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(503 if failure == "server" else 302)
        return httpx.Response(202)

    notifier = Notifier(
        AppConfig(_env_file=None, ntfy_url="https://ntfy.example/alerts"),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1.0,
    )
    assert not await notifier.send("Trading halted", "risk limit")
    assert await notifier.send("Trading halted", "risk limit")
    assert not await notifier.send("Trading halted", "risk limit")
    assert attempts == 2


async def test_concurrent_duplicate_is_suppressed_and_failed_send_can_retry() -> None:
    started, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            await release.wait()
            return httpx.Response(500)
        return httpx.Response(200)

    notifier = Notifier(
        AppConfig(_env_file=None, ntfy_url="https://ntfy.example/alerts"),
        transport=httpx.MockTransport(handler),
    )
    first = asyncio.create_task(notifier.send("Stop missing", "check broker"))
    await started.wait()
    assert not await notifier.send("Stop missing", "check broker")
    assert attempts == 1
    release.set()
    assert not await first
    assert await notifier.send("Stop missing", "check broker")
    assert attempts == 2


async def test_cancellation_releases_inflight_notification() -> None:
    started = asyncio.Event()
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            await asyncio.Event().wait()
        return httpx.Response(200)

    notifier = Notifier(
        AppConfig(_env_file=None, ntfy_url="https://ntfy.example/alerts"),
        transport=httpx.MockTransport(handler),
    )
    first = asyncio.create_task(notifier.send("Disconnected", "broker unreachable"))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await notifier.send("Disconnected", "broker unreachable")


async def test_dedupe_window_starts_after_acceptance() -> None:
    now = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        now[0] += 5
        return httpx.Response(200)

    notifier = Notifier(
        AppConfig(_env_file=None, ntfy_url="https://ntfy.example/alerts"),
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
    )
    assert await notifier.send("Halt", "a")
    now[0] = 61
    assert not await notifier.send("Halt", "b")  # only 56 seconds after acceptance
    now[0] = 65
    assert await notifier.send("Halt", "c")
