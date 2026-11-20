"""FakeReparo as a standalone server, driven by a raw `websockets` client (no bridge code)."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from typing import Any, cast

import aiohttp
import pytest
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from reparo_playwright_bridge.testing import (
    Close,
    Continue,
    Fail,
    FakeReparo,
    Report,
    SendError,
    SendRaw,
    Silence,
    Step,
)
from reparo_playwright_bridge.testing._payload_schema import (
    RecoveryPayload,
    RobotCapabilities,
)

type JsonObject = dict[str, Any]  # pyright: ignore[reportExplicitAny]  # decoded frames hold any JSON

WS_PATH = "/recovery/robot_exception/ws"
CONTINUE = Continue("click_approve", [0])


def valid_payload(**overrides: object) -> JsonObject:
    payload: JsonObject = {
        "task_name": "spending_approval",
        "platform": "playwright",
        "os": "Windows",
        "variables": {"approval_limit": 500, "pending": ["1", "2"]},
        "ui_log": [
            {
                "case_id": "case-1",
                "activity_id": "open-the-spending-requests",
                "activity_name": "Open the spending requests",
                "event_id": "1",
                "event_name": "Open the requests page",
                "action_type": "navigate",
                "application": "Spending",
                "input": None,
                "ui_element_target": "http://app/requests",
                "ui_group": "root",
                "timestamp": "2026-11-12T10:00:00+00:00",
                "previous_state": "about:blank",
                "current_state": "http://app/requests",
            }
        ],
        "errored_act": {
            "case_id": "case-1",
            "activity_id": "approve-the-request",
            "activity_name": "Approve the request",
            "event_id": "2",
            "event_name": "Click Approve #0",
            "action_type": "click",
            "application": "Spending",
            "input": None,
            "error_code": "ElementNotFound",
            "error_description": "The Approve button was not found.",
        },
        "model": 'process spending_approval\n  click_approve "Click Approve"\n',
    }
    payload.update(overrides)
    return payload


def loads(frame: str | bytes) -> JsonObject:
    return cast(JsonObject, json.loads(frame))


def open_socket(reparo: FakeReparo, key: str | None = None) -> connect:
    ws_url = "ws" + reparo.url.removeprefix("http") + WS_PATH
    return connect(
        ws_url,
        additional_headers={"X-ROBOT-KEY": reparo.robot_key if key is None else key},
    )


async def recv(ws: ClientConnection) -> str | bytes:
    """The next frame, failing fast instead of hanging."""
    async with asyncio.timeout(5):
        return await ws.recv()


def is_ping(frame: str | bytes) -> bool:
    return isinstance(frame, str) and loads(frame).get("type") == "ping"


async def next_json(ws: ClientConnection) -> JsonObject:
    """The next non-ping JSON frame."""
    while True:
        frame = await recv(ws)
        if not is_ping(frame):
            return loads(frame)


async def frames_until_closed(ws: ClientConnection) -> list[str | bytes]:
    """Every non-ping frame until the fake closes the socket."""
    frames: list[str | bytes] = []
    with pytest.raises(ConnectionClosed):
        while True:
            frame = await recv(ws)
            if not is_ping(frame):
                frames.append(frame)
    return frames


async def play(script: list[Step]) -> tuple[FakeReparo, list[str | bytes], int | None]:
    """Send a valid payload; return the fake, the frames it sent and its close code."""
    async with FakeReparo(script) as reparo, open_socket(reparo) as ws:
        await ws.send(json.dumps(valid_payload()))
        frames = await frames_until_closed(ws)
    return reparo, frames, ws.close_code


# --- Serving and the robot key ---


async def test_continue_sends_done_with_an_id_and_echoes_variables() -> None:
    reparo, frames, close_code = await play([CONTINUE])

    [done] = [loads(frame) for frame in frames]
    assert done["type"] == "done"
    assert isinstance(done["id"], str)
    assert done["id"]
    assert done["content"] == {
        "success": True,
        "process_finished": False,
        "continue_from_model_event_id": "click_approve",
        "loop_iteration": [0],
        "variables": {"approval_limit": 500, "pending": ["1", "2"]},
    }
    assert "continue_from_step" not in done["content"]
    assert close_code == 1000
    assert reparo.payloads == [valid_payload()]
    assert not reparo.script_exhausted


async def test_continue_sends_the_variables_it_is_given_and_process_finished() -> None:
    _, frames, _ = await play(
        [Continue(None, [], variables={"remaining_budget": 50}, process_finished=True)]
    )

    assert loads(frames[0])["content"] == {
        "success": True,
        "process_finished": True,
        "continue_from_model_event_id": None,
        "loop_iteration": [],
        "variables": {"remaining_budget": 50},
    }


async def test_a_wrong_robot_key_is_closed_with_1008() -> None:
    async with (
        FakeReparo([CONTINUE]) as reparo,
        open_socket(reparo, key="not-the-key") as ws,
    ):
        with pytest.raises(ConnectionClosed) as closed:
            _ = await recv(ws)

    assert closed.value.rcvd is not None
    assert closed.value.rcvd.code == 1008
    assert reparo.payloads == []


def test_the_fake_pings_every_10_seconds_by_default() -> None:
    assert FakeReparo([]).ping_interval == 10.0


async def test_the_fake_pings_while_the_script_plays() -> None:
    async with (
        FakeReparo([Silence(0.35), Close()], ping_interval=0.1) as reparo,
        open_socket(reparo) as ws,
    ):
        await ws.send(json.dumps(valid_payload()))
        frames: list[str | bytes] = []
        with pytest.raises(ConnectionClosed):
            while True:
                frames.append(await recv(ws))

    assert len([frame for frame in frames if is_ping(frame)]) >= 3


# --- Payload validation ---


async def payload_answer(payload: object) -> tuple[JsonObject, int | None]:
    """Send one payload to a fake whose script would continue; return its answer and close code."""
    async with FakeReparo([CONTINUE]) as reparo, open_socket(reparo) as ws:
        await ws.send(
            payload if isinstance(payload, str | bytes) else json.dumps(payload)
        )
        [answer] = [loads(frame) for frame in await frames_until_closed(ws)]
    return answer, ws.close_code


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(valid_payload(surprise=1), id="unknown top-level field"),
        pytest.param(valid_payload(model="  "), id="blank model"),
        pytest.param(
            {k: v for k, v in valid_payload().items() if k != "errored_act"},  # pyright: ignore[reportAny]  # payload values are any JSON
            id="missing errored_act",
        ),
        pytest.param(
            valid_payload(
                ui_log=[{**valid_payload()["ui_log"][0], "timestamp": "yesterday"}]
            ),
            id="timestamp not ISO-8601",
        ),
        pytest.param(
            valid_payload(
                capabilities={"modalities": ["vision"], "ocr": False, "x": 1}
            ),
            id="unknown capabilities field",
        ),
        pytest.param(
            valid_payload(capabilities={"modalities": []}), id="no modalities"
        ),
        pytest.param(
            valid_payload(capabilities={"modalities": ["dom", "dom"]}),
            id="duplicate modalities",
        ),
        pytest.param(["not", "an", "object"], id="JSON but not an object"),
        pytest.param("not json", id="text that is not JSON"),
        pytest.param(b"\x00", id="binary frame"),
    ],
)
async def test_an_invalid_payload_gets_reparos_invalid_payload_error_and_close_1003(
    payload: object,
) -> None:
    answer, close_code = await payload_answer(payload)

    assert answer["type"] == "error"
    assert answer["code"] == "INVALID_RECOVERY_PAYLOAD"
    assert isinstance(answer["content"], str)
    assert answer["content"]
    assert close_code == 1003


async def test_a_rejected_payload_is_logged_without_its_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")
    payload = valid_payload(variables="secret-variable-value")

    answer, _ = await payload_answer(payload)

    assert (
        "secret-variable-value" in answer["content"]
    )  # Reparo's own answer carries it
    [record] = [r for r in caplog.records if "rejected the payload" in r.getMessage()]
    assert "variables" in record.getMessage()
    assert "secret-variable-value" not in caplog.text


async def test_the_optional_payload_fields_are_accepted() -> None:
    entry = {**valid_payload()["ui_log"][0], "model_event_id": "open_requests"}
    errored = {**valid_payload()["errored_act"], "model_event_id": "click_approve"}
    payload = valid_payload(
        ui_log=[entry],
        errored_act=errored,
        recovery_task="Approve the request through the Actions menu.",
        capabilities={"modalities": ["vision"], "ocr": False},
    )

    answer, _ = await payload_answer(payload)

    assert answer["type"] == "done"


def test_absent_capabilities_default_to_vision_without_ocr() -> None:
    payload = RecoveryPayload.model_validate(valid_payload())

    assert payload.capabilities == RobotCapabilities(modalities=["vision"], ocr=False)


# --- Script steps ---


async def test_fail_sends_done_with_success_false() -> None:
    reparo, frames, close_code = await play([Fail("the Approve button is gone")])

    [done] = [loads(frame) for frame in frames]
    assert done["type"] == "done"
    assert isinstance(done["id"], str)
    assert done["content"]["success"] is False
    assert done["content"]["continue_from_model_event_id"] is None
    assert close_code == 1000
    assert not reparo.script_exhausted


async def test_send_error_sends_an_error_frame_and_closes() -> None:
    _, frames, close_code = await play([SendError("NO_AGENT_GRAPH")])

    [error] = [loads(frame) for frame in frames]
    assert error["type"] == "error"
    assert error["code"] == "NO_AGENT_GRAPH"
    assert close_code == 1000


async def test_send_error_invalid_recovery_payload_closes_with_1003_like_reparo() -> (
    None
):
    _, frames, close_code = await play([SendError("INVALID_RECOVERY_PAYLOAD")])

    assert loads(frames[0])["code"] == "INVALID_RECOVERY_PAYLOAD"
    assert close_code == 1003


async def test_close_closes_the_socket_without_a_done() -> None:
    reparo, frames, close_code = await play([Close()])

    assert frames == []
    assert close_code == 1000
    assert not reparo.script_exhausted


def test_steps_after_a_terminal_step_are_rejected() -> None:
    with pytest.raises(ValueError, match="after the terminal step"):
        _ = FakeReparo([Close(), Silence(1)])


ACTION = {
    "type": "action",
    "content": {"action_type": "left_single", "start_box": [10, 20]},
}


async def test_send_raw_sends_frames_as_is_and_the_script_plays_blindly() -> None:
    script: list[Step] = [
        SendRaw(ACTION),
        SendRaw("not even json"),
        SendRaw(b"\x00\x01"),
        Silence(0.3),
    ]
    async with FakeReparo(script) as reparo, open_socket(reparo) as ws:
        await ws.send(json.dumps(valid_payload()))
        assert await next_json(ws) == ACTION
        assert await recv(ws) == "not even json"
        assert await recv(ws) == b"\x00\x01"
        failed = {"success": False, "action_type": "left_single", "error": "x"}
        await ws.send(json.dumps(failed))
        await ws.send(b"\xff\xd8jpeg")
        abort = {"type": "abort", "error_code": "Timeout", "error_description": "x"}
        await ws.send(json.dumps(abort))
        assert await frames_until_closed(ws) == []  # runs out after its Silence

    assert reparo.replies == [failed, b"\xff\xd8jpeg"]
    assert reparo.aborts == [abort]
    assert reparo.script_exhausted


async def test_silence_sends_nothing_for_its_seconds() -> None:
    async with (
        FakeReparo([Silence(0.5), Close()]) as reparo,
        open_socket(reparo) as ws,
    ):
        await ws.send(json.dumps(valid_payload()))
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await frames_until_closed(ws) == []
        assert loop.time() - started >= 0.45


async def test_a_script_that_runs_out_without_a_terminal_step_closes_and_is_marked() -> (
    None
):
    reparo, frames, _ = await play([])

    assert frames == []
    assert reparo.script_exhausted


# --- report_result ---


async def recovery_id_from(reparo: FakeReparo) -> str:
    async with open_socket(reparo) as ws:
        await ws.send(json.dumps(valid_payload()))
        recovery_id = cast(str, (await next_json(ws))["id"])
    return recovery_id


async def report(
    reparo: FakeReparo, recovery_id: str, key: str | None, body: object
) -> int:
    headers = {} if key is None else {"X-ROBOT-KEY": key}
    async with (
        aiohttp.ClientSession() as http,
        http.post(
            f"{reparo.url}/recovery/report_result/{recovery_id}",
            json=body,
            headers=headers,
        ) as response,
    ):
        return response.status


async def test_report_result_is_recorded_for_a_recovery_id_the_fake_gave() -> None:
    async with FakeReparo([CONTINUE]) as reparo:
        recovery_id = await recovery_id_from(reparo)
        status = await report(reparo, recovery_id, reparo.robot_key, {"success": True})

    assert status == 204
    assert reparo.reports == [Report(recovery_id=recovery_id, body={"success": True})]


@pytest.mark.parametrize(
    ("key", "known_id", "status"),
    [
        pytest.param(None, True, 401, id="no robot key"),
        pytest.param("not-the-key", True, 403, id="wrong robot key"),
        pytest.param("right", False, 404, id="unknown Recovery id"),
        pytest.param("right", "not-a-uuid", 400, id="Recovery id not a UUID"),
    ],
)
async def test_report_result_is_refused_like_reparo(
    key: str | None, known_id: bool | str, status: int
) -> None:
    async with FakeReparo([CONTINUE]) as reparo:
        recovery_id = await recovery_id_from(reparo)
        sent_key = reparo.robot_key if key == "right" else key
        sent_id = (
            known_id
            if isinstance(known_id, str)
            else recovery_id
            if known_id
            else "00000000-0000-0000-0000-000000000000"
        )
        got = await report(reparo, sent_id, sent_key, {"success": False})

    assert got == status
    assert reparo.reports == []


# --- Packaging ---


def test_importing_the_testing_module_without_aiohttp_names_the_extra() -> None:
    hide_aiohttp_then_import = (
        "import sys\n"
        "sys.modules['aiohttp'] = None\n"
        "try:\n"
        "    import reparo_playwright_bridge.testing\n"
        "except ImportError as exc:\n"
        "    print(exc)\n"
        "else:\n"
        "    print('imported')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", hide_aiohttp_then_import],
        capture_output=True,
        text=True,
        check=True,
    )

    assert "[fake]" in done.stdout
