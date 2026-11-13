# pyright: reportAny=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
# (FakeReparo records payloads as decoded JSON, whose values are any JSON.)
"""A UI failure reaches Reparo: `run()` against FakeReparo on real headless Chromium."""

import logging
import platform
import re
import socket
import time
import uuid
from collections.abc import Awaitable, Callable

import pytest
from aiohttp import web
from playwright.async_api import BrowserContext, expect

from reparo_playwright_bridge import (
    EventContext,
    Process,
    ReparoSettings,
    RunResult,
    run,
)
from reparo_playwright_bridge.testing import (
    Close,
    Decline,
    FakeReparo,
    Report,
    SendError,
    SendRaw,
    Step,
)
from tests.fixture_server import FixtureServer

PAGE = '<button disabled>Approve</button><p id="status">pending</p>'


def settings(reparo: FakeReparo, **overrides: object) -> ReparoSettings:
    return ReparoSettings(
        base_url=reparo.url,
        robot_key=reparo.robot_key,
        **overrides,  # pyright: ignore[reportArgumentType]
    )


def approval_process(body: Callable[[EventContext], Awaitable[None]]) -> Process:
    """open_page -> click_approve (runs `body`) -> after."""
    p = Process("approve requests")
    with p.activity("Open the requests"):

        @p.event("Open the page")
        async def open_page(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/page")
            ctx.vars["opened"] = True

    with p.activity("Approve the request"):

        @p.event("Click Approve")
        async def click_approve(ctx: EventContext) -> None:
            await body(ctx)

        @p.event("Click Done")
        async def after(_ctx: EventContext) -> None:
            pass

    return p


async def _click_disabled(ctx: EventContext) -> None:
    await ctx.page.get_by_role("button", name="Approve").click(timeout=300)


async def test_a_ui_failure_sends_one_payload_and_a_decline_ends_the_run_declined(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    async with FakeReparo([Decline("no idea")]) as reparo:
        result = await run(
            approval_process(_click_disabled),
            context=context,
            reparo=settings(reparo),
            case_id=7,
        )

    assert result.outcome == "recovery_declined"
    assert result.failure is not None
    assert result.failure.type == "recovery_declined"
    assert result.failure.event_id == "click_approve"
    assert len(reparo.payloads) == 1
    payload = reparo.payloads[0]
    assert payload["task_name"] == "approve requests"
    assert payload["platform"] == "playwright"
    assert payload["errored_act"]["event_name"] == "Click Approve"
    assert [e["event_name"] for e in payload["ui_log"]] == ["Open the page"]


# --------------------------------------------------------------------------- what starts a Recovery


async def _click_missing(ctx: EventContext) -> None:
    await ctx.page.get_by_role("button", name="Reject").click(timeout=300)


async def _click_missing_whose_selector_names_an_environment_failure(
    ctx: EventContext,
) -> None:
    # The call log repeats the selector; only the error itself decides the class.
    await ctx.page.get_by_text("Target closed: Protocol error, crashed").click(
        timeout=300
    )


async def _click_ambiguous(ctx: EventContext) -> None:
    await ctx.page.locator("button, p").click(timeout=300)


async def _failed_expect(ctx: EventContext) -> None:
    await expect(ctx.page.locator("#status")).to_have_text("approved", timeout=300)


async def _declared_failure(ctx: EventContext) -> None:
    ctx.fail("The request is still pending.", code="RequestStillPending")


@pytest.mark.parametrize(
    "body",
    [
        _click_disabled,
        _click_missing,
        _click_missing_whose_selector_names_an_environment_failure,
        _click_ambiguous,
        _failed_expect,
        _declared_failure,
    ],
)
async def test_a_ui_failure_starts_a_recovery_with_a_short_code_and_one_sentence(
    context: BrowserContext,
    site: FixtureServer,
    body: Callable[[EventContext], Awaitable[None]],
) -> None:
    _ = site.page("/page", PAGE)

    async with FakeReparo([Decline("no")]) as reparo:
        result = await run(
            approval_process(body), context=context, reparo=settings(reparo), case_id=1
        )

    assert result.outcome == "recovery_declined"
    errored = reparo.payloads[0]["errored_act"]
    code, description = errored["error_code"], errored["error_description"]
    assert re.fullmatch(r"[A-Za-z]{3,40}", code)
    assert description
    assert len(description) <= 200
    assert "\n" not in description
    assert "Call log" not in description
    assert "Traceback" not in description
    assert not re.match(r"\w+\.\w+:", description), (
        "a sentence, not a Playwright API line"
    )
    assert description[0].isupper()
    assert description.endswith(".")
    assert result.failure is not None
    assert (result.failure.error_code, result.failure.error_description) == (
        code,
        description,
    )


async def test_ctx_fail_sends_its_own_code_and_description(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    async with FakeReparo([Decline("no")]) as reparo:
        _ = await run(
            approval_process(_declared_failure),
            context=context,
            reparo=settings(reparo),
            case_id=1,
        )

    errored = reparo.payloads[0]["errored_act"]
    assert errored["error_code"] == "RequestStillPending"
    assert errored["error_description"] == "The request is still pending."


async def _divide_by_zero(ctx: EventContext) -> None:
    ctx.vars["ratio"] = 1 / 0


async def _unreachable_service(ctx: EventContext) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    _ = await ctx.page.goto(f"http://127.0.0.1:{port}/")


@pytest.mark.parametrize(
    ("body", "kind"),
    [(_divide_by_zero, "Process bug"), (_unreachable_service, "Environment failure")],
)
async def test_process_bugs_and_environment_failures_never_reach_reparo(
    context: BrowserContext,
    site: FixtureServer,
    body: Callable[[EventContext], Awaitable[None]],
    kind: str,
) -> None:
    _ = site.page("/page", PAGE)

    async with FakeReparo([Decline("no")]) as reparo:
        result = await run(
            approval_process(body), context=context, reparo=settings(reparo), case_id=1
        )

    assert result.outcome == "local"
    assert result.reason is not None
    assert result.reason.startswith(f"{kind}: ")
    assert reparo.payloads == []


# --------------------------------------------------------------------------- opting out


def opted_out_process(*, activity: bool, event: bool) -> Process:
    p = Process("opted out")
    with p.activity("Approve the request", recoverable=activity):

        @p.event("Open the page")
        async def open_page(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/page")

        @p.event("Click Approve", recoverable=event)
        async def click_approve(ctx: EventContext) -> None:
            await _click_disabled(ctx)

    return p


@pytest.mark.parametrize(
    ("activity", "event", "why"),
    [
        (True, False, "Event click_approve is not recoverable"),
        (False, True, "Activity 'Approve the request' is not recoverable"),
        (False, False, "Activity 'Approve the request' is not recoverable"),
    ],
)
async def test_an_opted_out_ui_failure_ends_local_and_logs_what_would_have_been_sent(
    context: BrowserContext,
    site: FixtureServer,
    caplog: pytest.LogCaptureFixture,
    activity: bool,
    event: bool,
    why: str,
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    async with FakeReparo([Decline("no")]) as reparo:
        result = await run(
            opted_out_process(activity=activity, event=event),
            context=context,
            reparo=settings(reparo),
            case_id=1,
        )

    assert reparo.payloads == []
    assert result.outcome == "local"
    assert result.failure is not None
    assert result.failure.type == "local"
    assert result.failure.error_code == "UiElementTimeout"
    assert result.reason is not None
    assert why in result.reason
    suppressed = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(suppressed) == 1
    message = suppressed[0].getMessage()
    assert "UiElementTimeout" in message
    assert result.failure.error_description is not None
    assert result.failure.error_description in message
    assert why in message


# --------------------------------------------------------------------------- the payload


def snapshot_process() -> Process:
    p = Process("snapshot", variables={"approval_limit": int})
    with p.activity("Approve the request", recovery_task="Approve it by hand"):

        @p.event("Open the page")
        async def open_page(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/page")
            ctx.vars["opened"] = True

        @p.event("Click Approve")
        async def click_approve(ctx: EventContext) -> None:
            ctx.vars["approval_limit"] = 0
            ctx.vars["half_written"] = "yes"
            await _click_disabled(ctx)

    return p


async def test_the_payload_carries_the_failing_events_start_snapshot_and_the_model(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)
    p = snapshot_process()

    async with FakeReparo([Decline("no")]) as reparo:
        _ = await run(
            p,
            context=context,
            reparo=settings(reparo, model_format="compact"),
            case_id="case-9",
            variables={"approval_limit": 500},
        )

    payload = reparo.payloads[0]
    assert payload["variables"] == {"approval_limit": 500, "opened": True}
    assert payload["model"] == p.to_model("compact")
    assert payload["os"] == platform.system()
    assert payload["errored_act"]["case_id"] == "case-9"
    assert payload["errored_act"]["activity_id"] == "approve_the_request"
    assert payload["errored_act"]["event_id"] == "2"
    assert [e["event_id"] for e in payload["ui_log"]] == ["1"]


async def test_the_model_defaults_to_bpmn(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)
    p = snapshot_process()

    async with FakeReparo([Decline("no")]) as reparo:
        _ = await run(
            p,
            context=context,
            reparo=settings(reparo),
            case_id=1,
            variables={"approval_limit": 500},
        )

    assert reparo.payloads[0]["model"] == p.to_model("bpmn")


async def test_entries_name_their_model_event_and_the_recovery_task_is_sent(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    async with FakeReparo([Decline("no")]) as reparo:
        _ = await run(
            snapshot_process(),
            context=context,
            reparo=settings(reparo),
            case_id=1,
            variables={"approval_limit": 500},
        )

    payload = reparo.payloads[0]
    assert payload["errored_act"]["model_event_id"] == "click_approve"
    assert [e["model_event_id"] for e in payload["ui_log"]] == ["open_page"]
    assert payload["recovery_task"] == "Approve it by hand"


async def test_no_recovery_task_is_sent_when_the_activity_declares_none(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    async with FakeReparo([Decline("no")]) as reparo:
        _ = await run(
            approval_process(_click_disabled),
            context=context,
            reparo=settings(reparo),
            case_id=1,
        )

    assert "recovery_task" not in reparo.payloads[0]


# --------------------------------------------------------------------------- Reparo's answer


async def recover_with(
    context: BrowserContext, script: list[Step], **overrides: object
) -> tuple[RunResult, FakeReparo]:
    async with FakeReparo(script) as reparo:
        result = await run(
            approval_process(_click_disabled),
            context=context,
            reparo=settings(reparo, **overrides),
            case_id=1,
        )
    return result, reparo


async def test_a_reparo_error_gives_recovery_unavailable(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    result, _reparo = await recover_with(context, [SendError("AGENT_CRASHED")])

    assert result.outcome == "recovery_unavailable"
    assert result.failure is not None
    assert result.failure.type == "recovery_unavailable"
    assert result.failure.event_id == "click_approve"
    assert result.recovery_id is None


async def test_a_rejected_payload_gives_payload_rejected_logged_with_reparos_text(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    result, _reparo = await recover_with(
        context,
        [SendError("INVALID_RECOVERY_PAYLOAD", content="ui_log.0.timestamp: bad")],
    )

    assert result.outcome == "recovery_unavailable"
    assert result.failure is not None
    assert result.failure.type == "payload_rejected"
    errors = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and r.name.endswith(".recovery")
    ]
    assert len(errors) == 1
    assert "ui_log.0.timestamp: bad" in errors[0].getMessage()


async def test_reparo_pings_are_ignored(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    result, reparo = await recover_with(
        context, [SendRaw({"type": "ping"}), SendRaw({"type": "ping"}), Decline("no")]
    )

    assert result.outcome == "recovery_declined"
    assert reparo.replies == []


async def test_a_rejected_robot_key_ends_the_run_local_at_once(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    async with FakeReparo([Decline("no")]) as reparo:
        started = time.monotonic()
        result = await run(
            approval_process(_click_disabled),
            context=context,
            reparo=ReparoSettings(base_url=reparo.url, robot_key="not-the-key"),
            case_id=1,
        )
        elapsed = time.monotonic() - started

    assert result.outcome == "local"
    assert result.failure is not None
    assert result.failure.type == "local"
    assert result.reason is not None
    assert "robot key" in result.reason
    assert reparo.payloads == []
    assert elapsed < 5


async def test_a_robot_key_refused_at_the_handshake_ends_the_run_local(
    context: BrowserContext, site: FixtureServer
) -> None:
    """Real Reparo closes with 1008 before accepting, which reaches the robot as HTTP 403."""
    _ = site.page("/page", PAGE)

    async def refuse(_request: web.Request) -> web.Response:
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
                approval_process(_click_disabled),
                context=context,
                reparo=ReparoSettings(
                    base_url=f"http://127.0.0.1:{port}", robot_key="not-the-key"
                ),
                case_id=1,
            )
        finally:
            await runner.cleanup()

    assert result.outcome == "local"
    assert result.reason is not None
    assert "robot key" in result.reason


# --------------------------------------------------------------------------- report_result


async def test_a_done_with_an_id_sets_recovery_id_and_reports_once(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    result, reparo = await recover_with(context, [Decline("no")])

    assert result.recovery_id is not None
    assert reparo.reports == [Report(result.recovery_id, {"success": False})]
    assert str(result).startswith(
        f"recovery_declined at click_approve, Recovery {result.recovery_id}"
    )


async def test_a_failed_report_logs_a_warning_and_keeps_the_outcome(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")
    unknown_id = str(uuid.uuid4())  # FakeReparo answers 404 to a report for it
    done = {
        "type": "done",
        "id": unknown_id,
        "content": {
            "success": False,
            "process_finished": False,
            "continue_from_model_event_id": None,
            "loop_iteration": [],
            "variables": {},
        },
    }

    result, reparo = await recover_with(context, [SendRaw(done), Close()])

    assert result.outcome == "recovery_declined"
    assert result.recovery_id == unknown_id
    assert reparo.reports == []
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name.endswith(".report")
    ]
    assert len(warnings) == 1
    assert unknown_id in warnings[0].getMessage()


async def test_no_report_without_a_recovery_id(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    _result, reparo = await recover_with(context, [SendError("AGENT_CRASHED")])

    assert reparo.reports == []


# --------------------------------------------------------------------------- logging


async def test_recovery_milestones_are_logged_at_info_without_secrets(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG)

    async with FakeReparo([Decline("no")]) as reparo:
        _ = await run(
            snapshot_process(),
            context=context,
            reparo=settings(reparo),
            case_id=1,
            variables={"approval_limit": 9137},
        )

    info = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.INFO and r.name == "reparo_playwright_bridge.recovery"
    ]
    assert len(info) == 3
    assert info[0].startswith("Recovery started: Event click_approve")
    assert "UiElementTimeout" in info[0]
    model_kb = len(snapshot_process().to_model("bpmn").encode()) / 1000
    assert (
        info[1]
        == f"payload sent: 1 logged events, model {model_kb:.1f} kB BPMN, 2 variables"
    )
    assert info[2].startswith("done received")
    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert reparo.robot_key not in everything
    assert "9137" not in everything
