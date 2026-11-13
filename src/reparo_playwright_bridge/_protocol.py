"""The protocol loop: one Recovery over Reparo's recovery WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar, Self, cast

from playwright.async_api import BrowserContext, Page
from pydantic import BaseModel, ConfigDict, ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from reparo_playwright_bridge._classify import FailureKind
from reparo_playwright_bridge._log import recovery_log
from reparo_playwright_bridge._payload import Payload
from reparo_playwright_bridge._result import FailedOutcome, FailureType
from reparo_playwright_bridge._settings import ReparoSettings

ROBOT_KEY_HEADER = "X-ROBOT-KEY"
INVALID_RECOVERY_PAYLOAD = "INVALID_RECOVERY_PAYLOAD"
REJECTED_KEY_CLOSE = 1008
REJECTED_KEY_STATUS = 403
"""Reparo closes with 1008 before accepting the socket, which reaches the robot as HTTP 403."""
CONNECT_BACKOFF = (1.0, 2.0, 4.0)
"""Seconds before each retry; later retries keep the last value."""

# Error codes of the robot-side `abort`, sent whenever the bridge ends a Recovery itself.
SILENCE_TIMEOUT = "RecoverySilenceTimeout"
DEADLINE_EXCEEDED = "RecoveryDeadlineExceeded"
ENVIRONMENT_FAILURE = "EnvironmentFailure"

# websockets logs handshake headers at DEBUG, which include the robot key: give it a logger
# that is never wired to any handler.
_SILENT = logging.Logger("reparo_playwright_bridge.websockets")  # noqa: LOG001  # deliberately outside the logger tree
_SILENT.disabled = True


class ContinuationContent(BaseModel):
    """The future `done.content` (Reparo#230 / #231)."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", strict=True)

    success: bool
    process_finished: bool
    continue_from_model_event_id: str | None
    loop_iteration: list[int]
    variables: dict[str, object]


@dataclass(frozen=True)
class Done:
    """Reparo ended the Recovery with a well-formed `done`."""

    content: ContinuationContent
    recovery_id: str


@dataclass(frozen=True)
class Ended:
    """The Recovery ended without a usable `done`."""

    outcome: FailedOutcome
    failure_type: FailureType
    reason: str

    @classmethod
    def unavailable(
        cls, reason: str, failure_type: FailureType = "recovery_unavailable"
    ) -> Self:
        return cls("recovery_unavailable", failure_type, reason)

    @classmethod
    def key_rejected(cls, how: str) -> Self:
        return cls("local", "local", f"Reparo rejected the robot key ({how})")

    @classmethod
    def environment(cls, reason: str) -> Self:
        return cls("local", "local", reason)


async def recover(
    settings: ReparoSettings,
    payload: Payload,
    extra: Mapping[str, object],
    *,
    context: BrowserContext,
    recovery: str,
) -> Done | Ended:
    """Connect, send `payload`, and serve Reparo until it ends the Recovery.

    Connecting is retried; once the payload is sent the bridge never reconnects, so a reconnect
    can never start a second Recovery. The page or `context` going away ends the Recovery as an
    Environment failure; `recovery` names the Recovery in that reason (`Event <id> #<pass>`).
    If it goes away before the payload is sent, no payload is sent.
    """
    with _EnvironmentWatch(context) as watch:
        ws = await _connect(settings, extra)
        if isinstance(ws, Ended):
            return ws
        async with ws:
            if watch.lost.is_set():
                return _environment_failure(watch, recovery, extra)
            try:
                await ws.send(json.dumps(payload.body))
                recovery_log.info(
                    "payload sent: %d logged events, model %.1f kB %s, %d variables",
                    payload.logged_events,
                    payload.model_size / 1000,
                    settings.model_format.upper(),
                    payload.variable_count,
                    extra=dict(extra),
                )
                timer = _Timer(settings.silence_timeout, settings.recovery_deadline)
                return await _serve(ws, timer, watch, recovery, extra)
            except ConnectionClosed as closed:
                if closed.rcvd is not None and closed.rcvd.code == REJECTED_KEY_CLOSE:
                    return Ended.key_rejected(f"close {REJECTED_KEY_CLOSE}")
                return Ended.unavailable(
                    "Reparo closed the connection before ending the Recovery"
                )


async def _connect(
    settings: ReparoSettings, extra: Mapping[str, object]
) -> ClientConnection | Ended:
    """Open the recovery WebSocket in up to `connect_attempts` attempts.

    A rejected robot key is never retried.
    """
    headers = {ROBOT_KEY_HEADER: settings.robot_key.get_secret_value()}
    attempts = settings.connect_attempts
    for attempt in range(1, attempts + 1):
        recovery_log.debug(
            "connect attempt %d of %d", attempt, attempts, extra=dict(extra)
        )
        try:
            return await connect(
                settings.websocket_url, additional_headers=headers, logger=_SILENT
            )
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status == REJECTED_KEY_STATUS:
                return Ended.key_rejected(f"HTTP {REJECTED_KEY_STATUS}")
            why = f"HTTP {status}"
        except (OSError, InvalidHandshake, TimeoutError) as exc:
            why = type(exc).__name__
        if attempt == attempts:
            return Ended.unavailable(f"could not connect to Reparo ({why})")
        backoff = CONNECT_BACKOFF[min(attempt, len(CONNECT_BACKOFF)) - 1]
        recovery_log.warning(
            "could not connect to Reparo (%s); retrying in %g s (attempt %d of %d)",
            why,
            backoff,
            attempt,
            attempts,
            extra=dict(extra),
        )
        await asyncio.sleep(backoff)
    raise AssertionError("connect_attempts is at least 1")  # pragma: no cover


class _Timer:
    """The silence timeout and the Recovery deadline, both started when the payload is sent."""

    def __init__(self, silence: timedelta, deadline: timedelta) -> None:
        self._silence: timedelta = silence
        self._deadline: timedelta = deadline
        self._ends_at: float = time.monotonic() + deadline.total_seconds()

        self._deadline_first: bool = False
        """Whether the last wait was cut short by the deadline rather than the silence timeout."""

    def wait(self) -> float:
        """How long to wait for the next message before one of the timers runs out."""
        left = self._ends_at - time.monotonic()
        self._deadline_first = left <= self._silence.total_seconds()
        return min(self._silence.total_seconds(), left)

    def expired(self) -> tuple[str, str]:
        """The abort code and description for the timer that ran out on the last wait."""
        # Decided by the wait's bound, not the clock: a wait can wake a little early.
        if self._deadline_first:
            return (
                DEADLINE_EXCEEDED,
                f"the Recovery deadline of {_seconds(self._deadline)} passed",
            )
        return SILENCE_TIMEOUT, f"Reparo was silent for {_seconds(self._silence)}"


def _seconds(span: timedelta) -> str:
    return f"{span.total_seconds():g} s"


class _EnvironmentWatch:
    """Notices the active page or the browser context going away while a Recovery runs.

    It watches the page that is active when the Recovery starts; with none open, the
    Environment is already gone.
    """

    def __init__(self, context: BrowserContext) -> None:
        self._context: BrowserContext = context
        open_pages = [page for page in context.pages if not page.is_closed()]
        self._page: Page | None = open_pages[-1] if open_pages else None
        self.lost: asyncio.Event = asyncio.Event()
        self.cause: str = ""
        """What went away, as a clause for the reason (`the page closed`)."""
        if self._page is None:
            self._lose("no page was open")

    def _lose(self, cause: str) -> None:
        if not self.lost.is_set():
            self.cause = cause
            self.lost.set()

    def _page_closed(self, _page: Page) -> None:
        self._lose("the page closed")

    def _page_crashed(self, _page: Page) -> None:
        self._lose("the page crashed")

    def _context_closed(self, _context: BrowserContext) -> None:
        self._lose("the browser context closed")

    def __enter__(self) -> Self:
        if self._page is not None:
            self._page.on("close", self._page_closed)
            self._page.on("crash", self._page_crashed)
        self._context.on("close", self._context_closed)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._page is not None:
            self._page.remove_listener("close", self._page_closed)
            self._page.remove_listener("crash", self._page_crashed)
        self._context.remove_listener("close", self._context_closed)


def _environment_failure(
    watch: _EnvironmentWatch, recovery: str, extra: Mapping[str, object]
) -> Ended:
    reason = f"{FailureKind.ENVIRONMENT.value}: {watch.cause} during the Recovery of {recovery}"
    recovery_log.error("%s; ending the Recovery", reason, extra=dict(extra))
    return Ended.environment(reason)


async def _next_frame(
    ws: ClientConnection, timer: _Timer, watch: _EnvironmentWatch
) -> str | bytes | None:
    """Reparo's next frame, or None when a timer ran out or the Environment went away first."""
    receiving = asyncio.ensure_future(ws.recv())
    losing = asyncio.ensure_future(watch.lost.wait())
    try:
        _ = await asyncio.wait(
            {receiving, losing},
            timeout=timer.wait(),
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        _ = losing.cancel()
        _ = receiving.cancel()  # cancelling recv() loses no data
    if watch.lost.is_set():
        if receiving.done() and not receiving.cancelled():
            _ = receiving.exception()  # dropped: the Environment failure wins
        return None
    return receiving.result() if receiving.done() else None


async def _serve(
    ws: ClientConnection,
    timer: _Timer,
    watch: _EnvironmentWatch,
    recovery: str,
    extra: Mapping[str, object],
) -> Done | Ended:
    while True:
        frame = await _next_frame(ws, timer, watch)
        if watch.lost.is_set():
            ended = _environment_failure(watch, recovery, extra)
            await _abort(ws, ENVIRONMENT_FAILURE, ended.reason, extra)
            return ended
        if frame is None:
            code, description = timer.expired()
            recovery_log.warning(
                "%s; ending the Recovery", description, extra=dict(extra)
            )
            await _abort(ws, code, description, extra)
            return Ended.unavailable(description, "timeout")
        message = _decode(frame)
        kind = message.get("type") if message is not None else None
        match kind:
            case "ping":
                continue
            case "done":
                return _done(message or {})
            case "error":
                return _error(message or {}, extra)
            case _:
                shown = kind if isinstance(kind, str) else "<unparseable>"
                recovery_log.warning(
                    "Reparo sent an unsupported message type %s",
                    shown,
                    extra=dict(extra),
                )
                await ws.send(
                    json.dumps(
                        {"success": False, "error": f"unsupported message type {shown}"}
                    )
                )


async def _abort(
    ws: ClientConnection, code: str, description: str, extra: Mapping[str, object]
) -> None:
    """Tell Reparo why the bridge ended the Recovery itself."""
    abort = {"type": "abort", "error_code": code, "error_description": description}
    try:
        await ws.send(json.dumps(abort))
    except ConnectionClosed:
        return
    recovery_log.warning("abort sent: %s", code, extra=dict(extra))


def _decode(frame: str | bytes) -> dict[str, object] | None:
    if not isinstance(frame, str):
        return None
    try:
        message = cast(object, json.loads(frame))
    except json.JSONDecodeError:
        return None
    return cast("dict[str, object]", message) if isinstance(message, dict) else None


def _done(message: dict[str, object]) -> Done | Ended:
    recovery_id = message.get("id")
    try:
        content = ContinuationContent.model_validate(message.get("content"))
    except ValidationError:
        content = None
    if content is None or not isinstance(recovery_id, str) or not recovery_id:
        return Ended.unavailable(
            "Reparo sent a done without a Continuation and a Recovery id"
        )
    return Done(content, recovery_id)


def _error(message: dict[str, object], extra: Mapping[str, object]) -> Ended:
    code = message.get("code")
    if code == INVALID_RECOVERY_PAYLOAD:
        recovery_log.error(
            "Reparo rejected the payload: %s", message.get("content"), extra=dict(extra)
        )
        return Ended.unavailable(
            f"Reparo rejected the Recovery payload ({INVALID_RECOVERY_PAYLOAD})",
            "payload_rejected",
        )
    return Ended.unavailable(f"Reparo answered with error {code}")
