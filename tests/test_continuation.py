# pyright: reportAny=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
# (FakeReparo records payloads as decoded JSON, whose values are any JSON.)
"""Resuming after a Recovery: `run()` against FakeReparo's Continuations on real headless Chromium."""

import logging
import socket
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import pytest
from playwright.async_api import BrowserContext

from reparo_playwright_bridge import (
    CheckContext,
    EventContext,
    Process,
    ReparoSettings,
    RunResult,
    run,
)
from reparo_playwright_bridge.testing import (
    Continue,
    FakeReparo,
    Report,
    Step,
)
from tests.fixture_server import FixtureServer

PAGE = '<button disabled>Approve</button><p id="status">pending</p>'


def settings(reparo: FakeReparo) -> ReparoSettings:
    return ReparoSettings(base_url=reparo.url, robot_key=reparo.robot_key)


def approval_process(trail: list[str], *, failures: int = 1) -> Process:
    """open_page -> click_approve (a UI failure on its first `failures` attempts) -> read_status.

    Each Event that runs to its end appends its id to `trail`.
    """
    attempts: list[None] = []
    p = Process("approve requests")
    with p.activity("Open the requests"):

        @p.event("Open the page")
        async def open_page(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/page")
            trail.append("open_page")

    with p.activity("Approve the request"):

        @p.event("Click Approve")
        async def click_approve(ctx: EventContext) -> None:
            attempts.append(None)
            if len(attempts) <= failures:
                ctx.fail("The Approve button is disabled.", code="ApproveDisabled")
            _ = await ctx.page.locator("#status").inner_text()
            trail.append("click_approve")

        @p.event("Read the status")
        async def read_status(ctx: EventContext) -> None:
            ctx.vars["status"] = await ctx.page.locator("#status").inner_text()
            trail.append("read_status")

    return p


async def recover(
    context: BrowserContext, process: Process, script: list[Step], **kwargs: object
) -> tuple[RunResult, FakeReparo]:
    async with FakeReparo(script) as reparo:
        result = await run(
            process,
            context=context,
            reparo=settings(reparo),
            case_id=1,
            **kwargs,  # pyright: ignore[reportArgumentType]
        )
    return result, reparo


async def test_continuing_at_a_later_event_skips_ahead_and_completes(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)
    trail: list[str] = []

    result, reparo = await recover(
        context, approval_process(trail), [Continue("read_status", [])]
    )

    assert result.outcome == "completed"
    assert result.reason is None
    assert result.failure is None
    assert result.recovery_id is not None
    assert str(result) == f"completed after Recovery {result.recovery_id}"
    assert trail == ["open_page", "read_status"]
    assert reparo.reports == [Report(result.recovery_id, {"success": True})]


@pytest.mark.parametrize(
    ("event_id", "expected_trail"),
    [
        ("click_approve", ["open_page", "click_approve", "read_status"]),
        ("open_page", ["open_page", "open_page", "click_approve", "read_status"]),
    ],
)
async def test_continuing_at_the_errored_or_an_earlier_event_runs_from_there(
    context: BrowserContext,
    site: FixtureServer,
    event_id: str,
    expected_trail: list[str],
) -> None:
    _ = site.page("/page", PAGE)
    trail: list[str] = []

    result, _reparo = await recover(
        context, approval_process(trail), [Continue(event_id, [])]
    )

    assert result.outcome == "completed"
    assert trail == expected_trail


async def test_the_resumed_event_is_a_new_logged_event_with_the_next_number(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    result, reparo = await recover(
        context, approval_process([]), [Continue("click_approve", [])]
    )

    assert result.outcome == "completed"
    assert reparo.payloads[0]["errored_act"]["event_id"] == "2"
    logged = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("Logged event ")
    ]
    assert logged == [
        "Logged event 1: Open the page",
        "Logged event 3: Click Approve",
        "Logged event 4: Read the status",
    ]


def with_status_check(process: Process, expected: str) -> Process:
    @process.completion_check("the status is as expected")
    async def status_matches(ctx: CheckContext) -> tuple[bool, str | None]:
        status = ctx.vars.get("status")
        if status == expected:
            return True, None
        return False, f"expected {expected}, got {status}"

    return process


@pytest.mark.parametrize(
    ("expected", "outcome"), [("approved", "completed"), ("rejected", "checks_failed")]
)
async def test_process_finished_runs_no_more_events_and_checks_the_returned_variables(
    context: BrowserContext, site: FixtureServer, expected: str, outcome: str
) -> None:
    _ = site.page("/page", PAGE)
    trail: list[str] = []
    process = with_status_check(approval_process(trail), expected)

    result, reparo = await recover(
        context,
        process,
        [Continue(None, [], variables={"status": "approved"}, process_finished=True)],
    )

    assert result.outcome == outcome
    assert trail == ["open_page"]
    assert result.recovery_id is not None
    assert reparo.reports == [
        Report(result.recovery_id, {"success": outcome == "completed"})
    ]
    if outcome == "checks_failed":
        assert str(result) == (
            f"checks_failed after Recovery {result.recovery_id}: "
            + "1 completion check not met: status_matches: expected rejected, got approved"
        )


def structured_process(trail: list[str]) -> Process:
    """approval_process, then a loop `each_item` holding a gateway `decide`."""
    p = approval_process(trail)
    with p.for_each("each_item", over="items", as_="item"):
        with p.activity("Look at the item"):

            @p.event("Read the item")
            async def read_item(ctx: EventContext) -> None:
                _ = await ctx.page.locator("#status").inner_text()
                trail.append("read_item")

        with (
            p.if_("decide", lambda v: v["item"] > 0, label="item is positive"),
            p.activity("Accept the item"),
        ):

            @p.event("Click Accept")
            async def accept_item(ctx: EventContext) -> None:
                _ = await ctx.page.locator("#status").inner_text()
                trail.append("accept_item")

    return p


@pytest.mark.parametrize(
    ("script", "in_reason"),
    [
        ([Continue("decide", [0])], "decide"),
        ([Continue("each_item", [])], "each_item"),
        ([Continue("1", [])], "'1'"),
        ([Continue("no_such_event", [])], "no_such_event"),
        ([Continue(None, [])], "names no Event"),
        ([Continue("read_status", [0])], "read_status"),
        ([Continue("read_item", [])], "read_item"),
        ([Continue("accept_item", [0, 0])], "accept_item"),
    ],
)
async def test_an_answer_naming_no_event_or_the_wrong_number_of_passes_is_rejected(
    context: BrowserContext, site: FixtureServer, script: list[Step], in_reason: str
) -> None:
    _ = site.page("/page", PAGE)
    trail: list[str] = []

    result, reparo = await recover(
        context, structured_process(trail), script, variables={"items": [1]}
    )

    assert result.outcome == "continuation_rejected"
    assert result.failure is not None
    assert result.failure.type == "continuation_rejected"
    assert result.failure.event_id == "click_approve"
    assert result.reason is not None
    assert in_reason in result.reason
    assert trail == ["open_page"]
    assert result.recovery_id is not None
    assert reparo.reports == [Report(result.recovery_id, {"success": False})]


def capturing_final_variables(
    process: Process, final: list[dict[str, object]]
) -> Process:
    @process.completion_check("the final Variables are captured")
    async def capture(ctx: CheckContext) -> tuple[bool, str | None]:
        final.append(dict(ctx.vars))
        return True, None

    return process


async def test_returned_variables_override_local_ones_key_by_key(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)
    final: list[dict[str, object]] = []

    result, _reparo = await recover(
        context,
        capturing_final_variables(approval_process([]), final),
        [Continue("read_status", [], variables={"b": 20, "c": 3})],
        variables={"a": 1, "b": 2},
    )

    assert result.outcome == "completed"
    assert final == [{"a": 1, "b": 20, "c": 3, "status": "pending"}]


async def test_on_continuation_decides_the_variables_the_run_resumes_with(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)
    final: list[dict[str, object]] = []
    seen: list[tuple[dict[str, Any], dict[str, Any]]] = []  # pyright: ignore[reportExplicitAny]
    process = capturing_final_variables(approval_process([]), final)

    @process.on_continuation
    def keep_my_b(
        returned: Mapping[str, Any],  # pyright: ignore[reportExplicitAny]
        local: Mapping[str, Any],  # pyright: ignore[reportExplicitAny]
    ) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
        seen.append((dict(returned), dict(local)))
        return {**local, **returned, "b": local["b"]}

    result, _reparo = await recover(
        context,
        process,
        [Continue("read_status", [], variables={"b": 20, "c": 3})],
        variables={"a": 1, "b": 2},
    )

    assert result.outcome == "completed"
    assert seen == [({"b": 20, "c": 3}, {"a": 1, "b": 2})]
    assert final == [{"a": 1, "b": 2, "c": 3, "status": "pending"}]


def test_a_process_takes_at_most_one_on_continuation_hook() -> None:
    process = approval_process([])

    @process.on_continuation
    def first(
        returned: Mapping[str, object], local: Mapping[str, object]
    ) -> dict[str, object]:
        return {**local, **returned}

    with pytest.raises(ValueError, match="on_continuation"):

        @process.on_continuation
        def second(
            returned: Mapping[str, object], local: Mapping[str, object]
        ) -> dict[str, object]:
            return {**local, **returned}


async def test_the_continuation_point_and_changed_variable_names_are_logged_at_info(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    result, _reparo = await recover(
        context,
        approval_process([]),
        [Continue("read_status", [], variables={"a": 1, "b": 9137, "c": 4242})],
        variables={"a": 1, "b": 2},
    )

    assert result.outcome == "completed"
    continuation = [
        r for r in caplog.records if r.getMessage().startswith("Continuation:")
    ]
    assert len(continuation) == 1
    assert continuation[0].levelno == logging.INFO
    assert continuation[0].getMessage() == (
        "Continuation: resuming at Event read_status; changed Variables: b, c"
    )
    everything = "\n".join(r.getMessage() for r in caplog.records)
    assert "9137" not in everything
    assert "4242" not in everything


async def test_a_second_ui_failure_is_not_handed_over_and_ends_continuation_failed(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")

    result, reparo = await recover(
        context, approval_process([], failures=2), [Continue("click_approve", [])]
    )

    assert len(reparo.payloads) == 1
    assert result.outcome == "continuation_failed"
    assert result.failure is not None
    assert result.failure.type == "continuation_failed"
    assert result.failure.event_id == "click_approve"
    assert result.failure.error_code == "ApproveDisabled"
    assert result.reason is not None
    assert result.reason.startswith("UI failure: ")
    assert "the run already had its one Recovery" in result.reason
    suppressed = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(suppressed) == 1
    message = suppressed[0].getMessage()
    assert "ApproveDisabled" in message
    assert "The Approve button is disabled." in message
    assert "the run already had its one Recovery" in message


def failing_after(
    process: Process, body: Callable[[EventContext], Awaitable[None]]
) -> Process:
    with process.activity("Wrap up"):

        @process.event("Wrap up")
        async def wrap_up(ctx: EventContext) -> None:
            await body(ctx)

    return process


async def _divide_by_zero(ctx: EventContext) -> None:
    ctx.vars["ratio"] = 1 / 0


async def _unreachable_service(ctx: EventContext) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    _ = await ctx.page.goto(f"http://127.0.0.1:{port}/")


def with_crashing_check(process: Process) -> Process:
    @process.completion_check("the check crashes")
    async def crashes(_ctx: CheckContext) -> tuple[bool, str | None]:
        raise RuntimeError("boom")

    return process


def with_crashing_hook(process: Process) -> Process:
    @process.on_continuation
    def crashes(
        _returned: Mapping[str, object], local: Mapping[str, object]
    ) -> dict[str, object]:
        return {"missing": local["missing"]}

    return process


def with_hook_returning_unnamed_variables(process: Process) -> Process:
    @process.on_continuation
    def unnamed(
        returned: Mapping[str, object], _local: Mapping[str, object]
    ) -> Mapping[str, object]:
        return {**returned, 1: "one"}  # pyright: ignore[reportReturnType]

    return process


@pytest.mark.parametrize(
    ("build", "variables", "kind", "event_id"),
    [
        (
            lambda: failing_after(approval_process([]), _divide_by_zero),
            {},
            "Process bug",
            "wrap_up",
        ),
        (
            lambda: failing_after(approval_process([]), _unreachable_service),
            {},
            "Environment failure",
            "wrap_up",
        ),
        (lambda: with_crashing_check(approval_process([])), {}, "Process bug", None),
        (lambda: with_crashing_hook(approval_process([])), {}, "Process bug", None),
        (
            lambda: with_hook_returning_unnamed_variables(approval_process([])),
            {},
            "Process bug",
            None,
        ),
        (
            lambda: structured_process([]),
            {},
            "Process bug",
            None,
        ),  # loop over a missing list
    ],
    ids=[
        "process bug",
        "environment failure",
        "crashing check",
        "crashing hook",
        "hook returning unnamed Variables",
        "loop binding",
    ],
)
async def test_any_failure_after_a_continuation_ends_continuation_failed(
    context: BrowserContext,
    site: FixtureServer,
    build: Callable[[], Process],
    variables: dict[str, object],
    kind: str,
    event_id: str | None,
) -> None:
    _ = site.page("/page", PAGE)

    result, reparo = await recover(
        context, build(), [Continue("read_status", [])], variables=variables
    )

    assert result.outcome == "continuation_failed"
    assert result.failure is not None
    assert result.failure.type == "continuation_failed"
    assert result.failure.event_id == event_id
    assert result.reason is not None
    assert result.reason.startswith(f"{kind}: ")
    assert result.recovery_id is not None
    assert str(result).startswith("continuation_failed")
    assert f", Recovery {result.recovery_id}: " in str(result)
    assert reparo.reports == [Report(result.recovery_id, {"success": False})]


ORDERS = [["a0", "a1"], ["b0", "b1", "b2"], ["c0", "c1"]]


def nested_loop_process(trail: list[str], *, fail_at: str = "b1") -> Process:
    """open_page, then `each_order` over `orders`, holding `each_line` over the order itself.

    `handle_line` is a UI failure the first time it sees `fail_at`, and appends each line it
    handles to `trail`.
    """
    failed: list[None] = []
    p = Process("handle orders")
    with p.activity("Open the orders"):

        @p.event("Open the page")
        async def open_page(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/page")

    with (
        p.for_each("each_order", over="orders", as_="order"),
        p.for_each("each_line", over="order", as_="line"),
        p.activity("Handle the line"),
    ):

        @p.event("Handle the line")
        async def handle_line(ctx: EventContext) -> None:
            line = ctx.vars["line"]
            if line == fail_at and not failed:
                failed.append(None)
                ctx.fail("The line is locked.", code="LineLocked")
            trail.append(line)

    return p


async def test_resuming_inside_nested_loops_runs_on_from_the_named_passes(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)
    trail: list[str] = []

    result, _reparo = await recover(
        context,
        nested_loop_process(trail),
        [Continue("handle_line", [0, 1])],
        variables={"orders": ORDERS},
    )

    assert result.outcome == "completed"
    # Up to the failure at b1; then from order 0 line 1, and on with order 1 from line 0.
    assert trail == ["a0", "a1", "b0", "a1", "b0", "b1", "b2", "c0", "c1"]


async def test_loop_item_variables_are_rebound_from_their_lists_never_taken_from_reparo(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.INFO, logger="reparo_playwright_bridge")
    trail: list[str] = []

    result, _reparo = await recover(
        context,
        nested_loop_process(trail),
        [Continue("handle_line", [1, 1], variables={"order": ["x"], "line": "x"})],
        variables={"orders": ORDERS},
    )

    assert result.outcome == "completed"
    assert trail == ["a0", "a1", "b0", "b1", "b2", "c0", "c1"]
    continuation = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("Continuation:")
    ]
    assert continuation == [
        "Continuation: resuming at Event handle_line #1#1; no Variables changed"
    ]


@pytest.mark.parametrize(
    ("passes", "returned", "in_reason"),
    [
        ([3, 0], None, "pass 3 of loop each_order"),
        ([0, 2], None, "pass 2 of loop each_line"),
        ([1, 0], {"orders": [["a0"]]}, "pass 1 of loop each_order"),
        ([0, -1], None, "pass -1 of loop each_line"),
    ],
    ids=["outer", "inner", "merged list", "negative"],
)
async def test_a_pass_beyond_its_list_is_rejected(
    context: BrowserContext,
    site: FixtureServer,
    passes: list[int],
    returned: dict[str, object] | None,
    in_reason: str,
) -> None:
    _ = site.page("/page", PAGE)
    trail: list[str] = []

    result, reparo = await recover(
        context,
        nested_loop_process(trail),
        [Continue("handle_line", passes, variables=returned)],
        variables={"orders": ORDERS},
    )

    assert result.outcome == "continuation_rejected"
    assert result.reason is not None
    assert in_reason in result.reason
    assert trail == ["a0", "a1", "b0"]
    assert result.recovery_id is not None
    assert reparo.reports == [Report(result.recovery_id, {"success": False})]


def routing_process(trail: list[str]) -> Process:
    """open_page, then gateway `route`: `review_large` if the amount is over 100, else `approve_small`.

    `review_large` is a UI failure the first time it runs; each Event appends its id to `trail`.
    """
    failed: list[None] = []
    p = Process("route requests")
    with p.activity("Open the requests"):

        @p.event("Open the page")
        async def open_page(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/page")

    with (
        p.if_("route", lambda v: v["amount"] > 100, label="amount is large"),
        p.activity("Review the request"),
    ):

        @p.event("Review the request")
        async def review_large(ctx: EventContext) -> None:
            if not failed:
                failed.append(None)
                ctx.fail("The Review button is missing.", code="ReviewMissing")
            trail.append("review_large")

    with p.else_(), p.activity("Approve the request"):

        @p.event("Approve the request")
        async def approve_small(_ctx: EventContext) -> None:
            trail.append("approve_small")

    return p


@pytest.mark.parametrize(
    ("returned", "warnings"),
    [
        (
            None,
            [
                "Continuation enters arm 'otherwise' of gateway route, "
                + "but its conditions pick arm 'amount is large'"
            ],
        ),
        ({"amount": 5}, []),
    ],
    ids=["conditions disagree", "conditions agree"],
)
async def test_resuming_inside_an_xor_arm_enters_it_and_warns_if_conditions_disagree(
    context: BrowserContext,
    site: FixtureServer,
    caplog: pytest.LogCaptureFixture,
    returned: dict[str, object] | None,
    warnings: list[str],
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")
    trail: list[str] = []

    result, _reparo = await recover(
        context,
        routing_process(trail),
        [Continue("approve_small", [], variables=returned)],
        variables={"amount": 500},
    )

    assert result.outcome == "completed"
    assert trail == ["approve_small"]
    assert [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ] == warnings


async def test_a_loop_list_reparo_broke_fails_the_continuation_as_a_process_bug(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", PAGE)

    result, _reparo = await recover(
        context,
        nested_loop_process([]),
        [Continue("handle_line", [0, 0], variables={"orders": "not a list"})],
        variables={"orders": ORDERS},
    )

    assert result.outcome == "continuation_failed"
    assert result.reason is not None
    assert result.reason.startswith("Process bug: loop each_order")


BIG_ORDERS = [["a0", "a1"], ["b0", "b1", "b2"], ["c0", "c1", "c2"]]


def loop_xor_loop_process(trail: list[str]) -> Process:
    """`each_order` over `orders`: `note_order`, then gateway `size`.

    A big order (over 2 lines) runs `flag_order`, then `each_line` over the order itself, holding
    `handle_line`, a UI failure the first time it sees b1. Otherwise, `pass_order` runs.
    Each Event appends what it handled to `trail`.
    """
    failed: list[None] = []
    p = Process("handle orders by size")
    with p.activity("Open the orders"):

        @p.event("Open the page")
        async def open_page(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/page")

    with p.for_each("each_order", over="orders", as_="order"):
        with p.activity("Note the order"):

            @p.event("Note the order")
            async def note_order(ctx: EventContext) -> None:
                trail.append(f"note {ctx.vars['order'][0][0]}")

        with p.if_("size", lambda v: len(v["order"]) > 2, label="big order"):
            with p.activity("Flag the order"):

                @p.event("Flag the order")
                async def flag_order(ctx: EventContext) -> None:
                    trail.append(f"flag {ctx.vars['order'][0][0]}")

            with (
                p.for_each("each_line", over="order", as_="line"),
                p.activity("Handle the line"),
            ):

                @p.event("Handle the line")
                async def handle_line(ctx: EventContext) -> None:
                    line = ctx.vars["line"]
                    if line == "b1" and not failed:
                        failed.append(None)
                        ctx.fail("The line is locked.", code="LineLocked")
                    trail.append(line)

        with p.else_(), p.activity("Pass the order"):

            @p.event("Pass the order")
            async def pass_order(ctx: EventContext) -> None:
                trail.append(f"pass {ctx.vars['order'][0][0]}")

    return p


@pytest.mark.parametrize(
    ("passes", "resumed", "warnings"),
    [
        ([1, 1], ["b1", "b2"], []),
        (
            [0, 1],
            ["a1", "note b", "flag b", "b0", "b1", "b2"],
            [
                "Continuation enters arm 'big order' of gateway size, "
                + "but its conditions pick arm 'otherwise'"
            ],
        ),
    ],
    ids=["conditions agree", "conditions disagree"],
)
async def test_resuming_in_a_loop_inside_an_xor_arm_inside_a_loop(
    context: BrowserContext,
    site: FixtureServer,
    caplog: pytest.LogCaptureFixture,
    passes: list[int],
    resumed: list[str],
    warnings: list[str],
) -> None:
    _ = site.page("/page", PAGE)
    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")
    trail: list[str] = []

    result, _reparo = await recover(
        context,
        loop_xor_loop_process(trail),
        [Continue("handle_line", passes)],
        variables={"orders": BIG_ORDERS},
    )

    assert result.outcome == "completed"
    # Up to the failure at b1; from the named passes, skipping the Events before the resumed one;
    # then the next order runs in full, its gateway decided by its conditions.
    assert trail == [
        *["note a", "pass a", "note b", "flag b", "b0"],
        *resumed,
        *["note c", "flag c", "c0", "c1", "c2"],
    ]
    assert [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ] == warnings
