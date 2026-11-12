"""The main seam: `run()` on real headless Chromium against localhost fixture pages."""

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import pytest
from playwright.async_api import Browser, BrowserContext, expect

from reparo_playwright_bridge import EventContext, Process, ReparoSettings, run
from tests.fixture_server import FixtureServer

GREETER = """
<label>Name <input id="name"></label>
<button onclick="document.querySelector('#out').textContent = 'Hello ' + document.querySelector('#name').value">
  Greet
</button>
<p id="out"></p>
"""


async def test_a_sequence_runs_its_events_in_order_and_completes(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    _ = site.page("/greeter", GREETER)
    p = Process("greet")
    with p.activity("Greet someone"):

        @p.event("Open the greeter")
        async def open_greeter(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/greeter")

        @p.event("Type Ana into Name")
        async def type_name(ctx: EventContext) -> None:
            await ctx.page.get_by_label("Name").fill("Ana")

        @p.event("Click Greet")
        async def click_greet(ctx: EventContext) -> None:
            await ctx.page.get_by_role("button", name="Greet").click()

    result = await run(p, context=context, reparo=reparo, case_id=1)

    assert result.outcome == "completed"
    assert result.reason is None
    assert result.failure is None
    assert await context.pages[0].locator("#out").text_content() == "Hello Ana"


# --------------------------------------------------------------------------- Variables and case_id


def counter_process(variables: dict[str, type] | None = None) -> Process:
    """Two Events that count through ctx.vars and show the count on the page."""
    p = Process("count", variables=variables)
    with p.activity("Count"):

        @p.event("Open the counter")
        async def open_counter(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/counter")
            ctx.vars["count"] = ctx.vars["start"] + 1

        @p.event("Show the count")
        async def show_count(ctx: EventContext) -> None:
            ctx.vars["count"] += 1
            await ctx.page.locator("#out").fill(f"{ctx.vars['count']}")

    return p


COUNTER = '<input id="out">'


async def test_ctx_vars_start_from_the_given_variables_and_are_shared_across_events(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    _ = site.page("/counter", COUNTER)
    starting = {"start": 40}

    result = await run(
        counter_process({"start": int}),
        context=context,
        reparo=reparo,
        case_id="c",
        variables=starting,
    )

    assert result.outcome == "completed"
    assert await context.pages[0].locator("#out").input_value() == "42"
    assert starting == {"start": 40}, "the caller's mapping is never mutated"


@pytest.mark.parametrize(
    ("variables", "expected_in_reason"),
    [
        ({}, ["approval_limit", "int", "requester", "str"]),
        ({"approval_limit": 500}, ["requester", "str"]),
        (
            {"approval_limit": "five hundred", "requester": "Ana"},
            ["approval_limit", "int"],
        ),
    ],
)
async def test_missing_or_mistyped_input_variables_end_the_run_local_before_the_first_event(
    context: BrowserContext,
    reparo: ReparoSettings,
    variables: dict[str, object],
    expected_in_reason: list[str],
) -> None:
    p = Process("approve", variables={"approval_limit": int, "requester": str})
    ran: list[str] = []
    with p.activity("Approve"):

        @p.event("Open the requests page")
        async def open_requests(_ctx: EventContext) -> None:
            ran.append("open_requests")

    result = await run(
        p, context=context, reparo=reparo, case_id=1, variables=variables
    )

    assert result.outcome == "local"
    assert result.failure is not None
    assert result.failure.type == "local"
    assert result.failure.event_id is None
    assert result.reason is not None
    assert "input Variable" in result.reason
    for word in expected_in_reason:
        assert word in result.reason
    assert "five hundred" not in result.reason
    assert "Ana" not in result.reason
    assert ran == []
    assert context.pages == [], "no page is touched before the input check"


async def test_correct_input_variables_pass_the_check(
    context: BrowserContext, reparo: ReparoSettings
) -> None:
    p = Process("approve", variables={"approval_limit": int})

    result = await run(
        p,
        context=context,
        reparo=reparo,
        case_id=1,
        variables={"approval_limit": 500, "extra": "x"},
    )

    assert result.outcome == "completed"


async def test_a_callable_case_id_is_called_once_per_run(
    context: BrowserContext,
    reparo: ReparoSettings,
    site: FixtureServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _ = site.page("/counter", COUNTER)
    calls: list[int] = []

    def next_case() -> int:
        calls.append(1)
        return 100 + len(calls)

    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")
    process = counter_process()
    _ = await run(
        process,
        context=context,
        reparo=reparo,
        case_id=next_case,
        variables={"start": 0},
    )
    _ = await run(
        process,
        context=context,
        reparo=reparo,
        case_id=next_case,
        variables={"start": 0},
    )

    assert len(calls) == 2
    case_ids = {getattr(r, "case_id", None) for r in caplog.records}
    assert case_ids == {101, 102}


@pytest.mark.parametrize("case_id", [None, 1.5, True])
async def test_a_case_id_that_is_not_str_or_int_ends_the_run_local(
    context: BrowserContext, reparo: ReparoSettings, case_id: object
) -> None:
    result = await run(
        Process("p"),
        context=context,
        reparo=reparo,
        case_id=lambda: case_id,  # pyright: ignore[reportArgumentType]
    )

    assert result.outcome == "local"
    assert result.reason is not None
    assert "case_id" in result.reason


async def test_a_case_id_callable_that_raises_ends_the_run_local(
    context: BrowserContext, reparo: ReparoSettings
) -> None:
    def broken() -> str:
        raise RuntimeError("no case")

    result = await run(Process("p"), context=context, reparo=reparo, case_id=broken)

    assert result.outcome == "local"
    assert result.reason is not None
    assert "case_id" in result.reason


# --------------------------------------------------------------------------- the active page


def page_watcher(
    bodies: list[Callable[[EventContext], Awaitable[None]]], seen: list[str]
) -> Process:
    """One Event per body; each records the URL of its ctx.page, then runs its body."""
    p = Process("watch")
    with p.activity("Watch pages"):
        for n, body in enumerate(bodies):

            async def event(
                ctx: EventContext,
                body: Callable[[EventContext], Awaitable[None]] = body,
            ) -> None:
                seen.append(ctx.page.url)
                await body(ctx)

            event.__name__ = f"event_{n}"
            _ = p.event(f"Event {n}")(event)
    return p


async def _nothing(_ctx: EventContext) -> None:
    pass


async def test_the_bridge_opens_a_page_only_if_the_context_has_none(
    context: BrowserContext, reparo: ReparoSettings
) -> None:
    seen: list[str] = []

    result = await run(
        page_watcher([_nothing, _nothing], seen),
        context=context,
        reparo=reparo,
        case_id=1,
    )

    assert result.outcome == "completed"
    assert seen == ["about:blank", "about:blank"]
    assert len(context.pages) == 1
    assert not context.pages[0].is_closed(), "the bridge never closes anything"


async def test_ctx_page_is_the_most_recently_opened_page(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    first, second = await context.new_page(), await context.new_page()
    _ = await first.goto(site.page("/first", "first"))
    _ = await second.goto(site.page("/second", "second"))
    seen: list[str] = []

    result = await run(
        page_watcher([_nothing], seen), context=context, reparo=reparo, case_id=1
    )

    assert result.outcome == "completed"
    assert seen == [site.url + "/second"]
    assert context.pages == [first, second]


async def test_an_event_that_opens_a_tab_leaves_the_next_event_on_that_tab(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    _ = site.page("/list", '<a href="/detail" target="_blank">Detail</a>')
    _ = site.page("/detail", "detail")
    seen: list[str] = []

    async def open_list(ctx: EventContext) -> None:
        _ = await ctx.page.goto("/list")

    async def open_detail_in_a_tab(ctx: EventContext) -> None:
        async with ctx.page.expect_popup() as popup:
            await ctx.page.get_by_role("link", name="Detail").click()
        await (await popup.value).wait_for_load_state()

    bodies: list[Callable[[EventContext], Awaitable[None]]] = [
        open_list,
        open_detail_in_a_tab,
        _nothing,
    ]
    result = await run(
        page_watcher(bodies, seen), context=context, reparo=reparo, case_id=1
    )

    assert result.outcome == "completed"
    assert seen == ["about:blank", site.url + "/list", site.url + "/detail"]
    assert len(context.pages) == 2


async def test_an_event_that_closes_its_page_leaves_the_next_event_on_the_previous_page(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    first, second = await context.new_page(), await context.new_page()
    _ = await first.goto(site.page("/first", "first"))
    seen: list[str] = []

    async def close_it(ctx: EventContext) -> None:
        await ctx.page.close()

    result = await run(
        page_watcher([close_it, _nothing], seen),
        context=context,
        reparo=reparo,
        case_id=1,
    )

    assert result.outcome == "completed"
    assert seen == ["about:blank", site.url + "/first"]
    assert second.is_closed()
    assert context.pages == [first]


# --------------------------------------------------------------------------- failures end the run local


def failing_process(
    body: Callable[[EventContext], Awaitable[None]], ran_after: list[str]
) -> Process:
    """open_page -> fail_here (runs `body`) -> after (records that it ran)."""
    p = Process("fail")
    with p.activity("Open"):

        @p.event("Open the page")
        async def open_page(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/page")

    with p.activity("Do the thing"):

        @p.event("Do the thing")
        async def fail_here(ctx: EventContext) -> None:
            await body(ctx)

        @p.event("Click Done")
        async def after(_ctx: EventContext) -> None:
            ran_after.append("after")

    return p


PAGE = '<button disabled>Approve</button><p id="status">pending</p>'


async def _divide_by_zero(ctx: EventContext) -> None:
    ctx.vars["ratio"] = 1 / 0


async def _plain_assert(ctx: EventContext) -> None:
    assert ctx.vars.get("ready"), "not ready"


async def _missing_variable(ctx: EventContext) -> None:
    ctx.vars["amount"] = ctx.vars["requested_amount"]


@pytest.mark.parametrize(
    ("body", "error_name"),
    [
        (_divide_by_zero, "ZeroDivisionError"),
        (_plain_assert, "AssertionError"),
        (_missing_variable, "KeyError"),
    ],
)
async def test_a_process_bug_ends_the_run_local_and_logs_its_traceback(
    context: BrowserContext,
    reparo: ReparoSettings,
    site: FixtureServer,
    caplog: pytest.LogCaptureFixture,
    body: Callable[[EventContext], Awaitable[None]],
    error_name: str,
) -> None:
    _ = site.page("/page", PAGE)
    ran_after: list[str] = []
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    result = await run(
        failing_process(body, ran_after), context=context, reparo=reparo, case_id=1
    )

    assert result.outcome == "local"
    assert result.failure is not None
    assert result.failure.type == "local"
    assert result.failure.event_id == "fail_here"
    assert result.failure.loop_iteration == []
    assert result.reason is not None
    assert result.reason.startswith("Process bug: ")
    assert error_name in result.reason
    assert ran_after == []
    tracebacks = [
        r for r in caplog.records if r.levelno == logging.ERROR and r.exc_info
    ]
    assert len(tracebacks) == 1
    assert tracebacks[0].exc_info is not None
    assert type(tracebacks[0].exc_info[1]).__name__ == error_name
    assert getattr(tracebacks[0], "event_id", None) == "fail_here"


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])  # pyright: ignore[reportAny]
    return port


async def _unreachable_service(ctx: EventContext) -> None:
    _ = await ctx.page.goto(f"http://127.0.0.1:{_closed_port()}/")


async def _closed_page(ctx: EventContext) -> None:
    await ctx.page.close()
    await ctx.page.get_by_role("button").click()


async def _crashed_page(ctx: EventContext) -> None:
    cdp = await ctx.page.context.new_cdp_session(ctx.page)
    async with ctx.page.expect_event("crash"):
        never_answered = asyncio.ensure_future(cdp.send("Page.crash"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType, reportUnknownVariableType]
    try:
        await ctx.page.get_by_role("button").click(timeout=2000)
    finally:
        _ = never_answered.cancel()


@pytest.mark.parametrize("body", [_unreachable_service, _closed_page, _crashed_page])
async def test_an_environment_failure_ends_the_run_local(
    context: BrowserContext,
    reparo: ReparoSettings,
    site: FixtureServer,
    caplog: pytest.LogCaptureFixture,
    body: Callable[[EventContext], Awaitable[None]],
) -> None:
    _ = site.page("/page", PAGE)
    ran_after: list[str] = []
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    result = await run(
        failing_process(body, ran_after), context=context, reparo=reparo, case_id=1
    )

    assert result.outcome == "local"
    assert result.failure is not None
    assert result.failure.type == "local"
    assert result.failure.event_id == "fail_here"
    assert result.reason is not None
    assert result.reason.startswith("Environment failure: ")
    assert "\n" not in result.reason
    assert ran_after == []
    assert [r for r in caplog.records if r.levelno == logging.ERROR and r.exc_info]


async def _click_disabled(ctx: EventContext) -> None:
    await ctx.page.get_by_role("button", name="Approve").click(timeout=300)


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
    ctx.fail("The request is still pending", code="RequestStillPending")


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
async def test_a_ui_failure_ends_the_run_local_without_reparo(
    context: BrowserContext,
    reparo: ReparoSettings,
    site: FixtureServer,
    body: Callable[[EventContext], Awaitable[None]],
) -> None:
    _ = site.page("/page", PAGE)
    ran_after: list[str] = []

    result = await run(
        failing_process(body, ran_after), context=context, reparo=reparo, case_id=1
    )

    assert result.outcome == "local"
    assert result.failure is not None
    assert result.failure.type == "local"
    assert result.failure.event_id == "fail_here"
    assert result.failure.error_code
    assert result.failure.error_description
    assert "\n" not in result.failure.error_description
    assert "Call log" not in result.failure.error_description
    assert result.reason is not None
    assert result.reason.startswith("UI failure: ")
    assert ran_after == []


async def test_ctx_fail_carries_its_code_and_description(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    result = await run(
        failing_process(_declared_failure, []),
        context=context,
        reparo=reparo,
        case_id=1,
    )

    assert result.failure is not None
    assert result.failure.error_code == "RequestStillPending"
    assert result.failure.error_description == "The request is still pending"


async def test_ctx_fail_defaults_to_the_expectation_not_met_code(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    async def fail_plainly(ctx: EventContext) -> None:
        ctx.fail("The status is wrong")

    result = await run(
        failing_process(fail_plainly, []), context=context, reparo=reparo, case_id=1
    )

    assert result.failure is not None
    assert result.failure.error_code == "ExpectationNotMet"


async def test_a_failure_while_finding_the_active_page_is_still_a_result(
    browser: Browser, reparo: ReparoSettings
) -> None:
    closed = await browser.new_context()
    await closed.close()
    p = Process("p")
    with p.activity("A"):
        _ = p.event("Do nothing")(_nothing)

    result = await run(p, context=closed, reparo=reparo, case_id=1)

    assert result.outcome == "local"
    assert result.failure is not None
    assert result.failure.event_id == "_nothing"
    assert result.reason is not None
    assert result.reason.startswith("Environment failure: ")


# --------------------------------------------------------------------------- loops and branches

LOG = '<ol id="log"></ol>'


async def _note(ctx: EventContext, text: str) -> None:
    """Append `text` to the page's log, so the test reads what ran from the page."""
    script = "t => document.querySelector('#log').append(Object.assign(document.createElement('li'), {textContent: t}))"
    await ctx.page.evaluate(script, text)


async def _page_log(context: BrowserContext) -> list[str]:
    return await context.pages[0].locator("#log li").all_text_contents()


async def test_for_each_runs_its_block_once_per_item_binding_the_item(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    _ = site.page("/log", LOG)
    p = Process("loop", variables={"pending": list})
    with p.activity("Open the log"):

        @p.event("Open the log page")
        async def open_log(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/log")

    with (
        p.for_each("each_request", over="pending", as_="request_id"),
        p.activity("Note the request"),
    ):

        @p.event("Note the request id")
        async def note_request(ctx: EventContext) -> None:
            await _note(ctx, f"request {ctx.vars['request_id']}")

    result = await run(
        p,
        context=context,
        reparo=reparo,
        case_id=1,
        variables={"pending": ["r1", "r2", "r3"]},
    )

    assert result.outcome == "completed"
    assert await _page_log(context) == ["request r1", "request r2", "request r3"]


def grid_process(fail_at: tuple[str, int] | None = None) -> Process:
    """Notes every cell of `rows` × `cols` in two nested loops; optionally fails at one cell."""
    p = Process("grid", variables={"rows": list, "cols": list})
    with p.activity("Open the log"):

        @p.event("Open the log page")
        async def open_log(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/log")

    with (
        p.for_each("each_row", over="rows", as_="row"),
        p.for_each("each_col", over="cols", as_="col"),
        p.activity("Note the cell"),
    ):

        @p.event("Note the cell")
        async def note_cell(ctx: EventContext) -> None:
            if (ctx.vars["row"], ctx.vars["col"]) == fail_at:
                ctx.fail("the cell is missing")
            await _note(ctx, f"{ctx.vars['row']}{ctx.vars['col']}")

    return p


async def test_nested_loops_run_the_inner_block_per_item_of_each_outer_pass(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    _ = site.page("/log", LOG)

    result = await run(
        grid_process(),
        context=context,
        reparo=reparo,
        case_id=1,
        variables={"rows": ["a", "b"], "cols": [1, 2, 3]},
    )

    assert result.outcome == "completed"
    assert await _page_log(context) == ["a1", "a2", "a3", "b1", "b2", "b3"]


async def test_a_failure_in_a_nested_loop_names_the_pass_of_each_loop(
    context: BrowserContext,
    reparo: ReparoSettings,
    site: FixtureServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _ = site.page("/log", LOG)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    result = await run(
        grid_process(fail_at=("b", 2)),
        context=context,
        reparo=reparo,
        case_id=1,
        variables={"rows": ["a", "b"], "cols": [1, 2, 3]},
    )

    assert result.outcome == "local"
    assert result.failure is not None
    assert result.failure.event_id == "note_cell"
    assert result.failure.loop_iteration == [1, 1]
    assert await _page_log(context) == ["a1", "a2", "a3", "b1"]
    warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert warning.getMessage().startswith("Event note_cell #1#1 failed: UI failure")
    assert getattr(warning, "loop_iteration", None) == [1, 1]


async def test_each_activity_entered_is_logged_once_per_pass(
    context: BrowserContext,
    reparo: ReparoSettings,
    site: FixtureServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _ = site.page("/log", LOG)
    caplog.set_level(logging.INFO, logger="reparo_playwright_bridge")

    _ = await run(
        grid_process(),
        context=context,
        reparo=reparo,
        case_id=1,
        variables={"rows": ["a"], "cols": [1, 2]},
    )

    entered = [r.getMessage() for r in caplog.records if " entered" in r.getMessage()]
    assert entered == [
        "Activity Open the log entered",
        "Activity Note the cell entered #0#0",
        "Activity Note the cell entered #0#1",
    ]


def approval_process(conditions_ran: list[str] | None = None) -> Process:
    """Decides on `amount` with an n-way XOR, then always notes that it's done."""
    p = Process("approve", variables={"amount": int})
    with p.activity("Open the log"):

        @p.event("Open the log page")
        async def open_log(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/log")

    def small(v: Mapping[str, int]) -> bool:
        if conditions_ran is not None:
            conditions_ran.append("small")
        return v["amount"] < 100

    def medium(v: Mapping[str, int]) -> bool:
        if conditions_ran is not None:
            conditions_ran.append("medium")
        return v["amount"] < 1000

    def also_medium(v: Mapping[str, int]) -> bool:
        if conditions_ran is not None:
            conditions_ran.append("also_medium")
        return v["amount"] < 1000

    with p.if_("decide", small, label="small amount"), p.activity("Approve"):

        @p.event("Click Approve")
        async def approve(ctx: EventContext) -> None:
            await _note(ctx, "approved")

    with p.elif_(medium, label="medium amount"), p.activity("Escalate"):

        @p.event("Click Escalate")
        async def escalate(ctx: EventContext) -> None:
            await _note(ctx, "escalated")

    with p.elif_(also_medium, label="never reached"), p.activity("Shadowed"):

        @p.event("Click Shadowed")
        async def shadowed(ctx: EventContext) -> None:
            await _note(ctx, "shadowed")

    with p.else_(), p.activity("Reject"):

        @p.event("Click Reject")
        async def reject(ctx: EventContext) -> None:
            await _note(ctx, "rejected")

    with p.activity("Finish"):

        @p.event("Note done")
        async def done(ctx: EventContext) -> None:
            await _note(ctx, "done")

    return p


@pytest.mark.parametrize(
    ("amount", "expected_log", "expected_conditions"),
    [
        (50, ["approved", "done"], ["small"]),
        (500, ["escalated", "done"], ["small", "medium"]),
        (5000, ["rejected", "done"], ["small", "medium", "also_medium"]),
    ],
)
async def test_an_xor_runs_only_the_first_arm_whose_condition_holds(
    context: BrowserContext,
    reparo: ReparoSettings,
    site: FixtureServer,
    amount: int,
    expected_log: list[str],
    expected_conditions: list[str],
) -> None:
    _ = site.page("/log", LOG)
    conditions_ran: list[str] = []

    result = await run(
        approval_process(conditions_ran),
        context=context,
        reparo=reparo,
        case_id=1,
        variables={"amount": amount},
    )

    assert result.outcome == "completed"
    assert await _page_log(context) == expected_log
    assert conditions_ran == expected_conditions


async def test_an_xor_with_no_else_and_no_true_arm_runs_no_arm(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    _ = site.page("/log", LOG)
    p = Process("maybe", variables={"flag": bool})
    with p.activity("Open the log"):

        @p.event("Open the log page")
        async def open_log(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/log")

    with (
        p.if_("check", _flag_set, label="flag set"),
        p.activity("Note"),
    ):

        @p.event("Note the flag")
        async def note(ctx: EventContext) -> None:
            await _note(ctx, "flagged")

    result = await run(
        p, context=context, reparo=reparo, case_id=1, variables={"flag": False}
    )

    assert result.outcome == "completed"
    assert await _page_log(context) == []


def _mutates(v: Mapping[str, Any]) -> bool:  # pyright: ignore[reportExplicitAny]
    v["sneaky"] = 1  # pyright: ignore[reportIndexIssue]
    return True


def control_flow_bug_process(kind: str) -> tuple[Process, list[str]]:
    """open_log, then a gateway or loop that raises, then an Event that must not run."""
    ran: list[str] = []
    p = Process("buggy")
    with p.activity("Open the log"):

        @p.event("Open the log page")
        async def open_log(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/log")

    def body() -> None:
        with p.activity("Never"):

            @p.event("Never runs")
            async def never(_ctx: EventContext) -> None:
                ran.append("never")

    match kind:
        case "condition raises":
            with p.if_("decide", _missing_is_positive, label="missing is positive"):
                body()
        case "condition mutates":
            with p.if_("decide", _mutates, label="mutates"):
                body()
        case "missing list":
            with p.for_each("each_item", over="missing", as_="item"):
                body()
        case _:
            with p.for_each("each_item", over="not_a_list", as_="item"):
                body()
    return p, ran


@pytest.mark.parametrize(
    ("kind", "in_reason"),
    [
        ("condition raises", ["gateway decide", "KeyError"]),
        ("condition mutates", ["gateway decide", "TypeError"]),
        ("missing list", ["loop each_item", "missing", "KeyError"]),
        ("not a list", ["loop each_item", "not_a_list", "str"]),
    ],
)
async def test_anything_raised_by_a_condition_or_loop_binding_is_a_process_bug(
    context: BrowserContext,
    reparo: ReparoSettings,
    site: FixtureServer,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    in_reason: list[str],
) -> None:
    _ = site.page("/log", LOG)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")
    p, ran = control_flow_bug_process(kind)

    result = await run(
        p,
        context=context,
        reparo=reparo,
        case_id=1,
        variables={"not_a_list": "abc"},
    )

    assert result.outcome == "local"
    assert result.failure is not None
    assert result.failure.type == "local"
    assert result.failure.event_id is None
    assert result.reason is not None
    assert result.reason.startswith("Process bug")
    for word in in_reason:
        assert word in result.reason
    assert ran == []
    bug_lines = [r for r in caplog.records if r.levelno == logging.ERROR and r.exc_info]
    assert len(bug_lines) == 1
    assert "abc" not in caplog.text, "Variable values are never logged"


def _flag_set(v: Mapping[str, bool]) -> bool:
    return v["flag"]


def _missing_is_positive(v: Mapping[str, int]) -> bool:
    return v["missing"] > 0
