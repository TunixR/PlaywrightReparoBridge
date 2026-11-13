"""The protocol loop: one Recovery over Reparo's recovery WebSocket."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Self, cast

from pydantic import BaseModel, ConfigDict, ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from reparo_playwright_bridge._log import recovery_log
from reparo_playwright_bridge._payload import Payload
from reparo_playwright_bridge._result import FailureType, Outcome
from reparo_playwright_bridge._settings import ReparoSettings

ROBOT_KEY_HEADER = "X-ROBOT-KEY"
INVALID_RECOVERY_PAYLOAD = "INVALID_RECOVERY_PAYLOAD"
REJECTED_KEY_CLOSE = 1008
REJECTED_KEY_STATUS = 403
"""Reparo closes with 1008 before accepting the socket, which reaches the robot as HTTP 403."""

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

    outcome: Outcome
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


async def recover(
    settings: ReparoSettings, payload: Payload, extra: Mapping[str, object]
) -> Done | Ended:
    """Connect, send `payload`, and serve Reparo until it ends the Recovery."""
    headers = {ROBOT_KEY_HEADER: settings.robot_key.get_secret_value()}
    try:
        ws = await connect(
            settings.websocket_url, additional_headers=headers, logger=_SILENT
        )
    except InvalidStatus as exc:
        if exc.response.status_code == REJECTED_KEY_STATUS:
            return Ended.key_rejected(f"HTTP {REJECTED_KEY_STATUS}")
        return Ended.unavailable(
            f"could not connect to Reparo (HTTP {exc.response.status_code})"
        )
    except (OSError, InvalidHandshake, TimeoutError) as exc:
        return Ended.unavailable(f"could not connect to Reparo ({type(exc).__name__})")
    async with ws:
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
            return await _serve(ws, extra)
        except ConnectionClosed as closed:
            if closed.rcvd is not None and closed.rcvd.code == REJECTED_KEY_CLOSE:
                return Ended.key_rejected(f"close {REJECTED_KEY_CLOSE}")
            return Ended.unavailable(
                "Reparo closed the connection before ending the Recovery"
            )


async def _serve(ws: ClientConnection, extra: Mapping[str, object]) -> Done | Ended:
    while True:
        frame = await ws.recv()
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
