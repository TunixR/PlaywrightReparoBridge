# pyright: reportAny=false
# (FakeReparo records payloads as decoded JSON, whose values are any JSON.)
"""Evaluating a UI failure against a chosen GUI agent: Reparo's eval endpoint via FakeReparo."""

import uuid

from playwright.async_api import BrowserContext

from reparo_playwright_bridge import EventContext, Process, ReparoSettings, run
from reparo_playwright_bridge.testing import Continue, FakeReparo, SendError, SendRaw
from tests.fixture_server import FixtureServer

PAGE = '<button disabled>Approve</button><p id="status">pending</p>'


def eval_settings(reparo: FakeReparo, agent_id: str | None = None) -> ReparoSettings:
    return ReparoSettings(
        base_url=reparo.url,
        robot_key=reparo.robot_key,
        eval_agent_id=agent_id or reparo.agent_id,
    )


def approval_process() -> Process:
    """open_page -> click_approve (fails on a disabled button) -> read_status."""
    p = Process("approve requests")
    with p.activity("Approve the request", recovery_task="Approve it by hand"):

        @p.event("Open the page")
        async def open_page(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/page")

        @p.event("Click Approve")
        async def click_approve(ctx: EventContext) -> None:
            await ctx.page.get_by_role("button", name="Approve").click(timeout=300)

        @p.event("Read the status")
        async def read_status(ctx: EventContext) -> None:
            ctx.vars["status"] = await ctx.page.locator("#status").text_content()

    return p


async def test_an_eval_sends_the_payload_with_its_recovery_task_to_the_chosen_agent(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    async with FakeReparo([Continue("read_status", [])], context) as reparo:
        result = await run(
            approval_process(),
            context=context,
            reparo=eval_settings(reparo),
            case_id=1,
        )

    assert result.outcome == "completed"
    [payload] = reparo.payloads
    assert payload["recovery_task"] == "Approve it by hand"


async def test_an_eval_result_continues_the_run_and_reports_its_exception_id(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)
    exception_id = str(uuid.uuid4())
    eval_result: dict[str, object] = {
        "type": "eval_result",
        "content": {
            "success": True,
            "process_finished": False,
            "continue_from_model_event_id": "read_status",
            "loop_iteration": [],
            "variables": {},
        },
        "exception_id": exception_id,
        "request_id": "r-1",
    }

    async with FakeReparo([SendRaw(eval_result)]) as reparo:
        result = await run(
            approval_process(),
            context=context,
            reparo=eval_settings(reparo),
            case_id=1,
        )

    assert result.outcome == "completed"
    assert result.recovery_id == exception_id


async def test_an_unknown_agent_id_ends_the_run_local(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    async with FakeReparo([Continue("read_status", [])]) as reparo:
        result = await run(
            approval_process(),
            context=context,
            reparo=eval_settings(reparo, agent_id=str(uuid.uuid4())),
            case_id=1,
        )

    assert result.outcome == "local"
    assert result.reason is not None
    assert "robot key or agent id" in result.reason
    assert reparo.payloads == []


async def test_an_eval_validation_error_gives_payload_rejected(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    async with FakeReparo(
        [SendError("VALIDATION_ERROR", "recovery_task must be a non-empty string")]
    ) as reparo:
        result = await run(
            approval_process(),
            context=context,
            reparo=eval_settings(reparo),
            case_id=1,
        )

    assert result.outcome == "recovery_unavailable"
    assert result.failure is not None
    assert result.failure.type == "payload_rejected"
