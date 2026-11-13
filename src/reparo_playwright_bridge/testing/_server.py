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
from playwright.async_api import BrowserContext
from pydantic import ValidationError

from reparo_playwright_bridge._log import testing_log
from reparo_playwright_bridge.testing._payload_schema import RecoveryPayload
from reparo_playwright_bridge.testing._steps import (
    TERMINAL_STEPS,
    Close,
    Continue,
    Fail,
    SendError,
    SendRaw,
    Silence,
    Step,
)

ROBOT_KEY_HEADER = "X-ROBOT-KEY"
PING_INTERVAL = 10.0
INVALID_RECOVERY_PAYLOAD = "INVALID_RECOVERY_PAYLOAD"
_CLOSING = (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR)

type JsonObject = dict[str, Any]  # pyright: ignore[reportExplicitAny]  # decoded JSON holds any value


@dataclass(frozen=True)
class Report:
    """One `POST report_result` the robot sent."""

    recovery_id: str
    body: object


class FakeReparo:
    """Plays one hand-written Recovery script per payload, and records what the robot sent.

    `async with FakeReparo(script) as reparo:` serves the recovery WebSocket and
    `POST report_result` on localhost. Point `ReparoSettings(base_url=reparo.url,
    robot_key=reparo.robot_key)` at it.
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
            if reader.done():  # the robot closed first: nothing left to play to
                _ = player.cancel()
            _ = await asyncio.gather(reader, player, return_exceptions=True)
            if not player.cancelled() and (exc := player.exception()) is not None:
                raise exc  # a broken script stays loud
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
                self.replies.append(cast(bytes, frame.data))
                continue
            if frame.type is not WSMsgType.TEXT:
                continue
            text = cast(str, frame.data)
            try:
                message = cast(object, json.loads(text))
            except json.JSONDecodeError:
                self.replies.append(text)
                continue
            if isinstance(message, dict):
                obj = cast(JsonObject, message)
                if obj.get("type") == "abort":
                    self.aborts.append(obj)
                    continue
                message = obj
            self.replies.append(message)

    async def _play(self, ws: web.WebSocketResponse, payload: JsonObject) -> None:
        echoed = cast(object, payload.get("variables", {}))
        for step in self._script:
            match step:
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
