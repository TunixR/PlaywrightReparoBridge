# pyright: reportAny=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
# (FakeReparo records frames as decoded JSON, whose values are any JSON.)
"""A stuck, dropped or unreachable Reparo never hangs the robot or starts a second Recovery."""

import asyncio
import contextlib
import logging
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Self
from urllib.parse import urlsplit

import pytest
from aiohttp import web
from playwright.async_api import BrowserContext

from reparo_playwright_bridge import EventContext, ReparoSettings, run
from reparo_playwright_bridge.testing import Close, Fail, FakeReparo, SendRaw, Silence
from tests.fixture_server import FixtureServer
from tests.test_recovery import PAGE, approval_process, settings

SHORT = timedelta(seconds=0.5)


async def click_disabled(ctx: EventContext) -> None:
    await ctx.page.get_by_role("button", name="Approve").click(timeout=300)


def warnings_from(caplog: pytest.LogCaptureFixture, logger: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == f"reparo_playwright_bridge.{logger}"
    ]


async def test_silence_ends_the_recovery_as_a_timeout_and_aborts(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    async with FakeReparo([Silence(10)]) as reparo:
        result = await run(
            approval_process(click_disabled),
            context=context,
            reparo=settings(reparo, silence_timeout=SHORT),
            case_id=1,
        )

    assert result.outcome == "recovery_unavailable"
    assert result.failure is not None
    assert result.failure.type == "timeout"
    assert result.failure.event_id == "click_approve"
    assert str(result).startswith("recovery_unavailable/timeout at click_approve")
    assert len(reparo.aborts) == 1
    abort = reparo.aborts[0]
    assert set(abort) == {"type", "error_code", "error_description"}
    assert abort["error_code"] == "RecoverySilenceTimeout"
    assert abort["error_description"]
    warnings = warnings_from(caplog, "recovery")
    assert any("silent" in w for w in warnings)
    assert any(w.startswith("abort sent") for w in warnings)


async def test_the_deadline_ends_the_recovery_even_while_reparo_pings(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    async with FakeReparo([Silence(10)], ping_interval=0.1) as reparo:
        result = await run(
            approval_process(click_disabled),
            context=context,
            reparo=settings(
                reparo, silence_timeout=SHORT, recovery_deadline=timedelta(seconds=1)
            ),
            case_id=1,
        )

    assert result.outcome == "recovery_unavailable"
    assert result.failure is not None
    assert result.failure.type == "timeout"
    assert [a["error_code"] for a in reparo.aborts] == ["RecoveryDeadlineExceeded"]
    assert any("deadline" in w for w in warnings_from(caplog, "recovery"))


async def test_a_drop_before_done_is_unavailable_and_never_reconnects(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    async with FakeReparo([Close()]) as reparo:
        result = await run(
            approval_process(click_disabled),
            context=context,
            reparo=settings(reparo, connect_attempts=3),
            case_id=1,
        )

    assert result.outcome == "recovery_unavailable"
    assert result.failure is not None
    assert result.failure.type == "recovery_unavailable"
    assert len(reparo.payloads) == 1
    assert reparo.aborts == []


_CONTENT = {
    "success": True,
    "process_finished": True,
    "continue_from_model_event_id": None,
    "loop_iteration": [],
    "variables": {},
}


@pytest.mark.parametrize(
    "done",
    [
        {"type": "done", "content": _CONTENT},
        {"type": "done", "content": {"continue_from_step": 2}, "id": str(uuid.uuid4())},
        {"type": "done", "content": "recovered", "id": str(uuid.uuid4())},
    ],
    ids=["no id", "legacy content", "not an object"],
)
async def test_a_done_without_the_continuation_shape_or_an_id_is_unavailable(
    context: BrowserContext, site: FixtureServer, done: dict[str, object]
) -> None:
    _ = site.page("/page", PAGE)

    async with FakeReparo([SendRaw(done), Close()]) as reparo:
        result = await run(
            approval_process(click_disabled),
            context=context,
            reparo=settings(reparo),
            case_id=1,
        )

    assert result.outcome == "recovery_unavailable"
    assert result.failure is not None
    assert result.failure.type == "recovery_unavailable"
    assert result.recovery_id is None
    assert reparo.reports == []


# --------------------------------------------------------------------------- connecting


class FlakyRelay:
    """A localhost TCP relay to `target` that drops its first `drops` connections."""

    def __init__(self, target_url: str, drops: int) -> None:
        self._target: tuple[str, int] = _host_port(target_url)
        self._drops_left: int = drops
        self.connections: int = 0
        self._server: asyncio.Server | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        port = int(self._server.sockets[0].getsockname()[1])
        return f"http://127.0.0.1:{port}"

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._relay, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        assert self._server is not None
        self._server.close()

    async def _relay(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        if self._drops_left > 0:
            self._drops_left -= 1
            writer.close()
            return
        up_reader, up_writer = await asyncio.open_connection(*self._target)
        _ = await asyncio.gather(
            _pipe(reader, up_writer), _pipe(up_reader, writer), return_exceptions=True
        )


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


def _host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    assert parts.hostname is not None and parts.port is not None
    return parts.hostname, parts.port


async def test_connecting_retries_with_backoff_and_logs_each_retry(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    async with FakeReparo([Fail("no")]) as reparo, FlakyRelay(reparo.url, 1) as relay:
        started = time.monotonic()
        result = await run(
            approval_process(click_disabled),
            context=context,
            reparo=ReparoSettings(
                base_url=relay.url, robot_key=reparo.robot_key, connect_attempts=3
            ),
            case_id=1,
        )
        elapsed = time.monotonic() - started

    assert result.outcome == "recovery_failed"
    assert len(reparo.payloads) == 1
    retries = [w for w in warnings_from(caplog, "recovery") if "retrying" in w]
    assert len(retries) == 1
    assert "1 s" in retries[0]
    assert elapsed >= 1


async def test_connecting_gives_up_after_connect_attempts(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    async with FakeReparo([Fail("no")]) as reparo, FlakyRelay(reparo.url, 5) as relay:
        result = await run(
            approval_process(click_disabled),
            context=context,
            reparo=ReparoSettings(
                base_url=relay.url, robot_key=reparo.robot_key, connect_attempts=2
            ),
            case_id=1,
        )

    assert result.outcome == "recovery_unavailable"
    assert result.failure is not None
    assert result.failure.type == "recovery_unavailable"
    assert relay.connections == 2
    assert reparo.payloads == []
    assert len([w for w in warnings_from(caplog, "recovery") if "retrying" in w]) == 1


async def test_a_key_refused_at_the_handshake_is_never_retried(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")
    handshakes = 0

    async def refuse(_request: web.Request) -> web.Response:
        nonlocal handshakes
        handshakes += 1
        return web.Response(status=403)

    app = web.Application()
    _ = app.router.add_get("/recovery/robot_exception/ws", refuse)
    runner = web.AppRunner(app)
    await runner.setup()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
        await web.SockSite(runner, sock).start()
        try:
            result = await run(
                approval_process(click_disabled),
                context=context,
                reparo=ReparoSettings(
                    base_url=f"http://127.0.0.1:{port}",
                    robot_key="not-the-key",
                    connect_attempts=3,
                ),
                case_id=1,
            )
        finally:
            await runner.cleanup()

    assert result.outcome == "local"
    assert handshakes == 1
    assert not [w for w in warnings_from(caplog, "recovery") if "retrying" in w]


# --------------------------------------------------------------------------- Environment failure


async def _close_page(context: BrowserContext) -> None:
    await context.pages[-1].close()


async def _close_context(context: BrowserContext) -> None:
    await context.close()


async def _crash_page(context: BrowserContext) -> None:
    with contextlib.suppress(Exception):  # the navigation itself fails as the tab dies
        _ = await context.pages[-1].goto("chrome://crash", timeout=5000)


@pytest.mark.parametrize(
    "interrupt",
    [_close_page, _close_context, _crash_page],
    ids=["page closed", "context closed", "page crashed"],
)
async def test_the_browser_going_away_during_a_recovery_ends_the_run_local(
    context: BrowserContext,
    site: FixtureServer,
    caplog: pytest.LogCaptureFixture,
    interrupt: Callable[[BrowserContext], Awaitable[None]],
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    async with FakeReparo([Silence(10)]) as reparo:

        async def interrupt_once_the_payload_is_in() -> None:
            while not reparo.payloads:
                await asyncio.sleep(0.05)
            await interrupt(context)

        interrupter = asyncio.create_task(interrupt_once_the_payload_is_in())
        started = time.monotonic()
        result = await run(
            approval_process(click_disabled),
            context=context,
            reparo=settings(reparo),
            case_id=1,
        )
        elapsed = time.monotonic() - started
        await interrupter

    assert result.outcome == "local"
    assert result.failure is not None
    assert result.failure.type == "local"
    assert result.failure.event_id == "click_approve"
    assert result.reason is not None
    assert result.reason.startswith("Environment failure")
    assert "Recovery of Event click_approve" in result.reason
    assert [a["error_code"] for a in reparo.aborts] == ["EnvironmentFailure"]
    assert elapsed < 8
    assert any(
        r.levelno == logging.ERROR and r.name.endswith(".recovery")
        for r in caplog.records
    )


async def test_the_page_closing_while_connecting_ends_the_run_local_without_a_payload(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    async with FakeReparo([Fail("no")]) as reparo, FlakyRelay(reparo.url, 1) as relay:

        async def close_during_the_backoff() -> None:
            while relay.connections == 0:
                await asyncio.sleep(0.02)
            await context.pages[-1].close()

        closer = asyncio.create_task(close_during_the_backoff())
        result = await run(
            approval_process(click_disabled),
            context=context,
            reparo=ReparoSettings(base_url=relay.url, robot_key=reparo.robot_key),
            case_id=1,
        )
        await closer

    assert result.outcome == "local"
    assert result.reason is not None
    assert result.reason.startswith("Environment failure: the page closed")
    assert "Recovery of Event click_approve" in result.reason
    assert reparo.payloads == []
