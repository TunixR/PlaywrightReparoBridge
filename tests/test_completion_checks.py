"""Completion checks, through the main seam: `run()` on real headless Chromium."""

import logging
from collections.abc import Awaitable, Callable

import pytest
from playwright.async_api import BrowserContext

from reparo_playwright_bridge import (
    CheckContext,
    EventContext,
    Process,
    ReparoSettings,
    run,
)
from tests.fixture_server import FixtureServer

CHECKS_LOG = "reparo_playwright_bridge.checks"

BUDGET = '<p id="budget">250</p>'

Check = Callable[[CheckContext], Awaitable[object]]


def budget_process(*checks: tuple[str, Check]) -> Process:
    """One Event that opens the budget page and reads it into `budget`, then the given checks."""
    p = Process("budget")
    with p.activity("Read the budget"):

        @p.event("Read the remaining budget")
        async def read_budget(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/budget")
            ctx.vars["budget"] = int(await ctx.page.locator("#budget").inner_text())

    for name, check in checks:
        _ = p.completion_check(name)(check)
    return p


async def budget_matches(ctx: CheckContext) -> tuple[bool, str | None]:
    if ctx.vars["budget"] == 250:
        return True, None
    return False, f"expected 250, got {ctx.vars['budget']}"


async def budget_is_50(ctx: CheckContext) -> tuple[bool, str | None]:
    if ctx.vars["budget"] == 50:
        return True, None
    return False, f"expected 50, got {ctx.vars['budget']}"


async def test_a_process_with_no_checks_completes_on_reaching_its_end(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    _ = site.page("/budget", BUDGET)

    result = await run(budget_process(), context=context, reparo=reparo, case_id=1)

    assert result.outcome == "completed"


async def test_a_met_check_completes_the_run_and_is_logged_at_info(
    context: BrowserContext,
    reparo: ReparoSettings,
    site: FixtureServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _ = site.page("/budget", BUDGET)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    result = await run(
        budget_process(("the budget is 250", budget_matches)),
        context=context,
        reparo=reparo,
        case_id=1,
    )

    assert result.outcome == "completed"
    assert result.reason is None
    met = [r for r in caplog.records if r.name == CHECKS_LOG]
    assert len(met) == 1
    assert met[0].levelno == logging.INFO
    assert "budget_matches" in met[0].getMessage()


async def test_an_unmet_check_gives_checks_failed_with_its_reason(
    context: BrowserContext,
    reparo: ReparoSettings,
    site: FixtureServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _ = site.page("/budget", BUDGET)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    result = await run(
        budget_process(("the budget is 50", budget_is_50)),
        context=context,
        reparo=reparo,
        case_id=1,
    )

    assert result.outcome == "checks_failed"
    assert result.failure is None
    assert (
        result.reason
        == "1 completion check not met: budget_is_50: expected 50, got 250"
    )
    unmet = [r for r in caplog.records if r.name == CHECKS_LOG]
    assert len(unmet) == 1
    assert unmet[0].levelno == logging.WARNING
    assert "budget_is_50" in unmet[0].getMessage()
    assert "expected 50, got 250" in unmet[0].getMessage()


async def test_every_check_runs_in_declaration_order_even_after_one_is_unmet(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    _ = site.page("/budget", BUDGET)
    ran: list[str] = []

    async def first(_ctx: CheckContext) -> tuple[bool, str | None]:
        ran.append("first")
        return False, "first reason"

    async def second(_ctx: CheckContext) -> tuple[bool, str | None]:
        ran.append("second")
        return True, None

    async def third(_ctx: CheckContext) -> tuple[bool, str | None]:
        ran.append("third")
        return False, "third reason"

    result = await run(
        budget_process(("one", first), ("two", second), ("three", third)),
        context=context,
        reparo=reparo,
        case_id=1,
    )

    assert ran == ["first", "second", "third"]
    assert result.outcome == "checks_failed"
    assert result.reason == (
        "2 completion checks not met: first: first reason; third: third reason"
    )


async def test_a_check_may_navigate_and_the_next_check_sees_that_page(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    _ = site.page("/budget", BUDGET)
    _ = site.page("/history", '<p id="last">approved 200</p>')

    async def opens_history(ctx: CheckContext) -> tuple[bool, str | None]:
        _ = await ctx.page.goto("/history")
        return True, None

    async def history_shows_approval(ctx: CheckContext) -> tuple[bool, str | None]:
        text = await ctx.page.locator("#last").inner_text()
        return (True, None) if text == "approved 200" else (False, text)

    result = await run(
        budget_process(
            ("the history opens", opens_history),
            ("the history shows the approval", history_shows_approval),
        ),
        context=context,
        reparo=reparo,
        case_id=1,
    )

    assert result.outcome == "completed"


async def _writes_vars(ctx: CheckContext) -> tuple[bool, str | None]:
    ctx.vars["budget"] = 50  # pyright: ignore[reportIndexIssue]
    return True, None


async def _raises(_ctx: CheckContext) -> tuple[bool, str | None]:
    raise ZeroDivisionError("division by zero")


async def _returns_bare_true(_ctx: CheckContext) -> bool:
    return True


async def _returns_unmet_without_reason(_ctx: CheckContext) -> tuple[bool, str | None]:
    return False, None


async def _returns_met_with_reason(_ctx: CheckContext) -> tuple[bool, str | None]:
    return True, "all good"


async def _returns_truthy_int(_ctx: CheckContext) -> tuple[int, None]:
    return 1, None


async def _ui_error(ctx: CheckContext) -> tuple[bool, str | None]:
    _ = await ctx.page.locator("#missing").inner_text(timeout=200)
    return True, None


@pytest.mark.parametrize(
    ("check", "expected_in_reason"),
    [
        (_writes_vars, "TypeError"),
        (_raises, "ZeroDivisionError"),
        (_returns_bare_true, "a bool"),
        (_returns_unmet_without_reason, "(bool, NoneType)"),
        (_returns_met_with_reason, "(bool, str)"),
        (_returns_truthy_int, "(int, NoneType)"),
        (_ui_error, "Timed out after 200 ms"),
    ],
)
async def test_a_crashing_check_is_a_process_bug_that_stops_the_remaining_checks(
    context: BrowserContext,
    reparo: ReparoSettings,
    site: FixtureServer,
    caplog: pytest.LogCaptureFixture,
    check: Check,
    expected_in_reason: str,
) -> None:
    _ = site.page("/budget", BUDGET)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")
    ran_after: list[str] = []

    async def unmet_before(_ctx: CheckContext) -> tuple[bool, str | None]:
        return False, "not this one"

    async def after(_ctx: CheckContext) -> tuple[bool, str | None]:
        ran_after.append("after")
        return True, None

    result = await run(
        budget_process(
            ("an unmet check first", unmet_before),
            ("the crashing check", check),
            ("a check after it", after),
        ),
        context=context,
        reparo=reparo,
        case_id=1,
    )

    assert result.outcome == "local"
    assert result.failure is not None
    assert result.failure.type == "local"
    assert result.failure.event_id is None
    assert result.reason is not None
    assert result.reason.startswith("Process bug: ")
    assert check.__name__ in result.reason
    assert expected_in_reason in result.reason
    assert ran_after == []
    crashed = [
        r for r in caplog.records if r.name == CHECKS_LOG and r.levelno == logging.ERROR
    ]
    assert len(crashed) == 1
    assert check.__name__ in crashed[0].getMessage()


async def test_a_check_that_hits_an_environment_failure_keeps_that_class(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    _ = site.page("/budget", BUDGET)

    async def page_closed(ctx: CheckContext) -> tuple[bool, str | None]:
        await ctx.page.close()
        _ = await ctx.page.locator("#budget").inner_text()
        return True, None

    result = await run(
        budget_process(("the page is closed", page_closed)),
        context=context,
        reparo=reparo,
        case_id=1,
    )

    assert result.outcome == "local"
    assert result.failure is not None
    assert result.failure.type == "local"
    assert result.reason is not None
    assert result.reason.startswith("Environment failure: ")
    assert "page_closed" in result.reason


async def test_checks_do_not_run_when_the_process_fails_before_its_end(
    context: BrowserContext, reparo: ReparoSettings
) -> None:
    ran: list[str] = []

    async def never(_ctx: CheckContext) -> tuple[bool, str | None]:
        ran.append("never")
        return True, None

    p = Process("fails")
    with p.activity("Fail"):

        @p.event("Fail")
        async def fail_here(_ctx: EventContext) -> None:
            raise RuntimeError("boom")

    _ = p.completion_check("never runs")(never)

    result = await run(p, context=context, reparo=reparo, case_id=1)

    assert result.outcome == "local"
    assert ran == []
