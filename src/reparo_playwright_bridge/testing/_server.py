"""FakeReparo: a real localhost server speaking Reparo's recovery contract."""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self, cast

from aiohttp import WSMessage, WSMsgType, web
from playwright.async_api import BrowserContext, Page
from pydantic import ValidationError

from reparo_playwright_bridge._context import latest_open_page
from reparo_playwright_bridge._log import testing_log
from reparo_playwright_bridge.testing._payload_schema import RecoveryPayload
from reparo_playwright_bridge.testing._steps import (
    TERMINAL_STEPS,
    Act,
    Close,
    Continue,
    Fail,
    Locate,
    Observe,
    SendError,
    SendRaw,
    Silence,
    Step,
)

ROBOT_KEY_HEADER = "X-ROBOT-KEY"
PING_INTERVAL = 10.0
INVALID_RECOVERY_PAYLOAD = "INVALID_RECOVERY_PAYLOAD"
SCREENSHOT_BUDGET = 15.0
"""Seconds the robot has to answer a screenshot request."""
ACTION_BUDGET = 60.0
"""Seconds the robot has to answer an action, so a robot that never replies can't hang a test."""
_CLOSING = (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR)

type JsonObject = dict[str, Any]  # pyright: ignore[reportExplicitAny]  # decoded JSON holds any value


class FakeReparoError(AssertionError):
    """The script broke, or the robot broke the contract; raised when the `async with` ends."""


@dataclass(frozen=True)
class Report:
    """One `POST report_result` the robot sent."""

    recovery_id: str
    body: object


class FakeReparo:
    """Plays one hand-written Recovery script per payload, and records what the robot sent.

    `async with FakeReparo(script) as reparo:` serves the recovery WebSocket and
    `POST report_result` on localhost. Point `ReparoSettings(base_url=reparo.url,
    robot_key=reparo.robot_key)` at it. `context` is needed only to resolve `Locate` targets
    and to check screenshot replies against the viewport size.

    It checks every reply to its own screenshot requests (`Observe`, `Act`): one binary JPEG,
    within 15 s, at the viewport size. A violation, or a broken script (a `Locate` that matches
    nothing), is recorded in `problems` and raises `FakeReparoError` when the `async with` ends.
    """

    def __init__(
        self,
        script: Sequence[Step],
        context: BrowserContext | None = None,
        *,
        ping_interval: float = PING_INTERVAL,
    ) -> None:
        self._script: tuple[Step, ...] = tuple(script)
        early_ends = [s for s in self._script[:-1] if isinstance(s, TERMINAL_STEPS)]
        if early_ends:
            raise ValueError(
                f"FakeReparo script has steps after the terminal step {early_ends[0]!r}"
            )
        self._context: BrowserContext | None = context  # resolves `Locate` targets
        self.ping_interval: float = ping_interval
        self.robot_key: str = secrets.token_urlsafe(16)
        self.payloads: list[JsonObject] = []
        self.replies: list[object] = []
        self.aborts: list[JsonObject] = []
        self.reports: list[Report] = []
        self.script_exhausted: bool = False
        self.problems: list[str] = []
        """Contract violations and script errors; any of them fails the `async with` loudly."""
        self._incoming: asyncio.Queue[object] = asyncio.Queue()
        """The robot's replies, for the steps that wait on one."""
        self._recovery_ids: set[str] = set()
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    @property
    def url(self) -> str:
        """The base URL to give `ReparoSettings`."""
        if self._port is None:
            raise RuntimeError("FakeReparo is not running; use `async with`")
        return f"http://127.0.0.1:{self._port}"

    async def __aenter__(self) -> Self:
        app = web.Application()
        _ = app.router.add_get("/recovery/robot_exception/ws", self._recovery_ws)
        _ = app.router.add_post(
            "/recovery/report_result/{recovery_id}", self._report_result
        )
        runner = web.AppRunner(app, shutdown_timeout=1.0)
        await runner.setup()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self._port = sock.getsockname()[1]
        await web.SockSite(runner, sock).start()
        self._runner = runner
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self.problems and exc is None:
            raise FakeReparoError("; ".join(self.problems))

    async def _report_result(self, request: web.Request) -> web.Response:
        recovery_id = request.match_info["recovery_id"]
        try:
            _ = uuid.UUID(recovery_id)
        except ValueError:
            return web.json_response(
                {"detail": "Invalid recovery ID format. Must be a UUID."}, status=400
            )
        key = request.headers.get(ROBOT_KEY_HEADER)
        if not key:
            return web.json_response({"detail": "No robot key provided"}, status=401)
        if key != self.robot_key:
            return web.json_response({"detail": "Invalid robot key"}, status=403)
        try:
            body = cast(object, await request.json())
        except json.JSONDecodeError:
            return web.json_response({"detail": "Body is not JSON"}, status=400)
        if recovery_id not in self._recovery_ids:
            return web.json_response({"detail": "Recovery ID not found"}, status=404)
        self.reports.append(Report(recovery_id=recovery_id, body=body))
        return web.Response(status=204)

    async def _recovery_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(max_msg_size=0)
        _ = await ws.prepare(request)
        if request.headers.get(ROBOT_KEY_HEADER) != self.robot_key:
            testing_log.warning("FakeReparo rejected a robot key (close 1008)")
            _ = await ws.close(code=1008, message=b"Invalid robot key")
            return ws
        pinger = asyncio.create_task(self._keep_alive(ws))
        try:
            await self._serve(ws)
        finally:
            _ = pinger.cancel()
        return ws

    async def _keep_alive(self, ws: web.WebSocketResponse) -> None:
        """Ping every `ping_interval` seconds, as Reparo does, until the socket closes."""
        while not ws.closed:
            await asyncio.sleep(self.ping_interval)
            try:
                await ws.send_json({"type": "ping"})
            except ConnectionError:
                return

    async def _serve(self, ws: web.WebSocketResponse) -> None:
        """Take one payload, then play the script while recording what the robot sends."""
        first = await ws.receive()
        if first.type in _CLOSING:
            return
        decoded = _decode_json_text(first)
        if decoded is None:
            testing_log.warning(
                "FakeReparo rejected the payload (close 1003): not JSON text"
            )
            await self._reject_payload(ws, "The payload must be JSON text")
            return
        if isinstance(decoded, dict):
            self.payloads.append(cast(JsonObject, decoded))
        try:
            _ = RecoveryPayload.model_validate(decoded)
        except ValidationError as exc:
            testing_log.warning(
                "FakeReparo rejected the payload (close 1003): %s",
                _error_locations(exc),
            )
            await self._reject_payload(ws, str(exc))
            return
        payload = cast(JsonObject, decoded)
        reader = asyncio.create_task(self._record_robot_frames(ws))
        player = asyncio.create_task(self._play(ws, payload))
        try:
            _ = await asyncio.wait(
                {reader, player}, return_when=asyncio.FIRST_COMPLETED
            )
            if player.done() and (exc := player.exception()) is not None:
                # A broken script stays loud: it fails the `async with`, and ends the Recovery.
                self.problems.append(f"the script broke: {exc}")
                testing_log.error("FakeReparo's script broke: %s", exc)
                _ = await ws.close()
            elif reader.done():  # the robot closed first: nothing left to play to
                _ = player.cancel()
            _ = await asyncio.gather(reader, player, return_exceptions=True)
        finally:
            _ = reader.cancel()
            _ = player.cancel()

    async def _reject_payload(self, ws: web.WebSocketResponse, content: str) -> None:
        await ws.send_json(
            {"type": "error", "code": INVALID_RECOVERY_PAYLOAD, "content": content}
        )
        _ = await ws.close(code=1003, message=b"Invalid recovery payload")

    async def _record_robot_frames(self, ws: web.WebSocketResponse) -> None:
        """Record every frame the robot sends after its payload, until the socket closes."""
        async for frame in ws:
            if frame.type is WSMsgType.BINARY:
                self._reply(cast(bytes, frame.data))
                continue
            if frame.type is not WSMsgType.TEXT:
                continue
            text = cast(str, frame.data)
            try:
                message = cast(object, json.loads(text))
            except json.JSONDecodeError:
                self._reply(text)
                continue
            if isinstance(message, dict):
                obj = cast(JsonObject, message)
                if obj.get("type") == "abort":
                    self.aborts.append(obj)
                    continue
                message = obj
            self._reply(message)

    def _reply(self, reply: object) -> None:
        self.replies.append(reply)
        self._incoming.put_nowait(reply)

    async def _play(self, ws: web.WebSocketResponse, payload: JsonObject) -> None:
        echoed = cast(object, payload.get("variables", {}))
        for step in self._script:
            match step:
                case Observe():
                    await self._screenshot(ws)
                case Act():
                    await self._act(ws, step)
                case Continue():
                    await self._send_done(
                        ws,
                        success=True,
                        process_finished=step.process_finished,
                        event_id=step.event_id,
                        loop_iteration=step.loop_iteration,
                        variables=echoed if step.variables is None else step.variables,
                    )
                    return
                case Fail():
                    testing_log.info("FakeReparo fails the Recovery: %s", step.reason)
                    await self._send_done(
                        ws,
                        success=False,
                        process_finished=False,
                        event_id=None,
                        loop_iteration=[],
                        variables=echoed,
                    )
                    return
                case SendError():
                    await ws.send_json(
                        {"type": "error", "code": step.code, "content": step.content}
                    )
                    code = 1003 if step.code == INVALID_RECOVERY_PAYLOAD else 1000
                    _ = await ws.close(code=code)
                    return
                case Close():
                    _ = await ws.close()
                    return
                case SendRaw(frame=bytes() as frame):
                    await ws.send_bytes(frame)
                case SendRaw(frame=str() as frame):
                    await ws.send_str(frame)
                case SendRaw(frame=frame):
                    await ws.send_json(frame)
                case Silence():
                    await asyncio.sleep(step.seconds)
        self.script_exhausted = True
        testing_log.warning(
            "FakeReparo's script ran out without a terminal step; closing the socket"
        )
        _ = await ws.close()

    async def _screenshot(self, ws: web.WebSocketResponse) -> None:
        """Ask for a screenshot and check the reply: one binary JPEG, in time, at the viewport size."""
        self._drain()
        await ws.send_json({"type": "screenshot", "content": ""})
        try:
            async with asyncio.timeout(SCREENSHOT_BUDGET):
                reply = await self._incoming.get()
        except TimeoutError:
            self._violation(f"no screenshot reply within {SCREENSHOT_BUDGET:g} s")
            return
        if not isinstance(reply, bytes):
            self._violation(f"a screenshot reply was not binary: {reply!r:.200}")
            return
        size = jpeg_size(reply)
        if size is None:
            self._violation("a screenshot reply was not a JPEG")
            return
        viewport = self._viewport()
        if viewport is not None and size != viewport:
            self._violation(
                f"a screenshot reply was {size[0]}x{size[1]}, "
                + f"not the viewport's {viewport[0]}x{viewport[1]}"
            )

    def _active_page(self) -> Page | None:
        return None if self._context is None else latest_open_page(self._context)

    def _viewport(self) -> tuple[int, int] | None:
        page = self._active_page()
        viewport = None if page is None else page.viewport_size
        return None if viewport is None else (viewport["width"], viewport["height"])

    async def _act(self, ws: web.WebSocketResponse, step: Act) -> None:
        """A screenshot request, then the action, its targets resolved just before sending."""
        await self._screenshot(ws)
        inputs: dict[str, object] = {}
        if step.target is not None:
            inputs["start_box"] = await self._resolve(step.target)
        for name, value in step.inputs.items():
            inputs[name] = (
                await self._resolve(value) if isinstance(value, Locate) else value
            )
        self._drain()
        await ws.send_json(
            {
                "type": "action",
                "content": {"action_type": step.action_type, "action_inputs": inputs},
            }
        )
        try:
            async with asyncio.timeout(ACTION_BUDGET):
                _ = await self._incoming.get()
        except TimeoutError:
            self._violation(
                f"no reply to a {step.action_type} action within {ACTION_BUDGET:g} s"
            )

    async def _resolve(self, target: object) -> list[float]:
        """A literal point as-is, or the centre of a `Locate`'s element on the active page."""
        if not isinstance(target, Locate):
            return [float(v) for v in cast("Sequence[float]", target)]
        page = self._active_page()
        if page is None:
            raise RuntimeError(
                f"{target!r} needs FakeReparo(..., context=...) with an open page"
            )
        locator = page.locator(target.selector)
        box = None if await locator.count() == 0 else await locator.first.bounding_box()
        if box is None:
            raise RuntimeError(f"{target!r} matched no visible element")
        return [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2]

    def _drain(self) -> None:
        """Forget replies no step waited on (to `SendRaw` requests), so the next one is ours."""
        while not self._incoming.empty():
            _ = self._incoming.get_nowait()

    def _violation(self, problem: str) -> None:
        testing_log.error("FakeReparo: the robot broke the contract: %s", problem)
        self.problems.append(problem)

    async def _send_done(
        self,
        ws: web.WebSocketResponse,
        *,
        success: bool,
        process_finished: bool,
        event_id: str | None,
        loop_iteration: list[int],
        variables: object,
    ) -> None:
        """Send `done` with a new Recovery id, then close."""
        recovery_id = str(uuid.uuid4())
        self._recovery_ids.add(recovery_id)
        content = {
            "success": success,
            "process_finished": process_finished,
            "continue_from_model_event_id": event_id,
            "loop_iteration": list(loop_iteration),
            "variables": variables,
        }
        await ws.send_json({"type": "done", "content": content, "id": recovery_id})
        _ = await ws.close()


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """The (width, height) in a JPEG's start-of-frame header, or None if it isn't a JPEG."""
    if data[:3] != b"\xff\xd8\xff":
        return None
    i = 2
    while i + 9 <= len(data) and data[i] == 0xFF:
        marker, length = data[i + 1], int.from_bytes(data[i + 2 : i + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5 : i + 7])
            width = int.from_bytes(data[i + 7 : i + 9])
            return width, height
        i += 2 + length
    return None


def _decode_json_text(frame: WSMessage) -> object | None:
    """The frame's JSON value, or None when it is not a JSON text frame."""
    if frame.type is not WSMsgType.TEXT:
        return None
    try:
        return cast(object, json.loads(cast(str, frame.data)))
    except json.JSONDecodeError:
        return None


def _error_locations(exc: ValidationError) -> str:
    """Where and how a payload breaks the schema, without the offending values."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['type']}"
        for error in exc.errors()
    )
