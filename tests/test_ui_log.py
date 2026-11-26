# pyright: reportAny=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportExplicitAny=false
# (FakeReparo records payloads as decoded JSON, whose values are any JSON.)
"""The complete UI log: what `reparo.payloads[0]` carries for each Logged event and the Errored activity."""

import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import pytest
from playwright.async_api import BrowserContext, Page, expect

from reparo_playwright_bridge import EventContext, Process, ReparoSettings, run
from reparo_playwright_bridge.testing import Fail, FakeReparo
from tests.fixture_server import FixtureServer

Body = Callable[[EventContext], Awaitable[None]]


async def _fail(ctx: EventContext) -> None:
    ctx.fail("Stop here.")


async def recorded(
    context: BrowserContext,
    *bodies: Body,
    secret: bool = False,
    case_id: str | int = "case-1",
) -> dict[str, Any]:
    """Run one Event per body (the last one should fail) and return the one payload Reparo got."""
    p = Process("ui log")
    with p.activity("Do the work"):
        for index, body in enumerate(bodies):

            async def fn(ctx: EventContext, body: Body = body) -> None:
                await body(ctx)

            fn.__name__ = f"step_{index}"
            _ = p.event(f"Step {index}", secret=secret)(fn)
    async with FakeReparo([Fail("no")]) as reparo:
        _ = await run(
            p,
            context=context,
            reparo=ReparoSettings(base_url=reparo.url, robot_key=reparo.robot_key),
            case_id=case_id,
        )
    assert len(reparo.payloads) == 1, "the Process should end with one UI failure"
    return reparo.payloads[0]


FORM = """
<title>Spending</title>
<form aria-label="Request form">
  <label for="amount">Amount</label><input id="amount" value="10">
  <button type="button" onclick="this.textContent='Approved'">Approve</button>
</form>
"""


async def test_a_click_logs_one_click_entry_with_its_target_group_states_and_application(
    context: BrowserContext, site: FixtureServer
) -> None:
    url = site.page("/form", FORM)

    async def open_form(ctx: EventContext) -> None:
        _ = await ctx.page.goto(url)

    async def approve(ctx: EventContext) -> None:
        await ctx.page.get_by_role("button", name="Approve").click()

    payload = await recorded(context, open_form, approve, _fail, case_id=7)

    click = payload["ui_log"][1]
    timestamp = click.pop("timestamp")
    assert datetime.fromisoformat(timestamp).utcoffset() is not None
    assert click == {
        "case_id": "7",
        "activity_id": "do_the_work",
        "activity_name": "Do the work",
        "event_id": "2",
        "event_name": "Step 1",
        "model_event_id": "step_1",
        "action_type": "click",
        "application": "Spending",
        "input": None,
        "ui_element_target": "Approve",
        "ui_group": "Request form",
        "previous_state": "Approve",
        "current_state": "Approved",
    }


async def run_local(context: BrowserContext, reparo: ReparoSettings, body: Body) -> str:
    """Run one Event that should end the run local, and return the reason."""
    p = Process("ui log")
    with p.activity("Do the work"):

        @p.event("Do it")
        async def do_it(ctx: EventContext) -> None:
            await body(ctx)

    result = await run(p, context=context, reparo=reparo, case_id=1)
    assert result.outcome == "local"
    assert result.reason is not None
    return result.reason


# ------------------------------------------------------------------------------- one action per Event


async def test_a_second_action_is_a_process_bug_raised_before_it_runs(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    url = site.page("/form", FORM)

    async def two_actions(ctx: EventContext) -> None:
        _ = await ctx.page.goto(url)
        await ctx.page.get_by_role("button", name="Approve").click()

    reason = await run_local(context, reparo, two_actions)

    assert reason.startswith("Process bug: ")
    assert "second action" in reason
    assert await context.pages[0].get_by_role("button").inner_text() == "Approve"


async def test_a_second_action_the_event_swallows_is_still_a_process_bug(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    url = site.page("/form", FORM)

    async def swallow(ctx: EventContext) -> None:
        _ = await ctx.page.goto(url)
        with contextlib.suppress(Exception):
            await ctx.page.get_by_role("button").click()

    reason = await run_local(context, reparo, swallow)

    assert "second action" in reason


async def test_an_event_with_no_logged_call_is_a_process_bug(
    context: BrowserContext, reparo: ReparoSettings, site: FixtureServer
) -> None:
    url = site.page("/form", FORM)
    page = await context.new_page()
    _ = await page.goto(url)

    async def only_checks(ctx: EventContext) -> None:
        _ = await ctx.page.get_by_role("button").is_visible()
        _ = await ctx.page.get_by_role("button").count()
        ctx.vars["seen"] = True

    reason = await run_local(context, reparo, only_checks)

    assert reason.startswith("Process bug: ")
    assert "no logged call" in reason


# --------------------------------------------------------------------------------------- read Events

LIST = """
<title>Requests</title>
<ul><li aria-label="Request 1"><span class="amount">  120
  EUR </span></li></ul>
<p id="total">Total: 120</p>
"""


async def test_an_event_with_no_action_logs_one_read_entry_from_its_last_read(
    context: BrowserContext, site: FixtureServer
) -> None:
    url = site.page("/list", LIST)

    async def open_list(ctx: EventContext) -> None:
        _ = await ctx.page.goto(url)

    async def read_two(ctx: EventContext) -> None:
        _ = await ctx.page.locator("#total").inner_text()
        ctx.vars["amount"] = await ctx.page.locator(".amount").text_content()

    payload = await recorded(context, open_list, read_two, _fail)

    assert len(payload["ui_log"]) == 2
    read = payload["ui_log"][1]
    assert read["action_type"] == "read"
    assert read["input"] is None
    assert read["ui_element_target"] == "120 EUR"
    assert read["ui_group"] == "Request 1"
    assert read["previous_state"] == read["current_state"] == "  120\n  EUR "


async def test_a_read_after_the_action_does_not_replace_the_action(
    context: BrowserContext, site: FixtureServer
) -> None:
    url = site.page("/list", LIST)

    async def navigate_then_read(ctx: EventContext) -> None:
        _ = await ctx.page.goto(url)
        _ = await ctx.page.locator("#total").inner_text()

    payload = await recorded(context, navigate_then_read, _fail)

    assert [e["action_type"] for e in payload["ui_log"]] == ["navigate"]


# ------------------------------------------------------------------------------- calls and action types

CONTROLS = """
<title>Controls</title>
<input id="name" aria-label="Name" value="">
<select id="size" aria-label="Size"><option value="s">Small</option><option value="l">Large</option></select>
<input id="agree" type="checkbox" aria-label="Agree">
<input id="opt" type="checkbox" aria-label="Optional" checked>
<input id="file" type="file" aria-label="Attachment" multiple>
<button id="b">Go</button>
<div id="src" draggable="true">Drag me</div><div id="dst">Drop here</div>
<div id="box" aria-label="Scroll box" style="height:50px;overflow:auto">
  <div style="height:400px"></div><p id="far">Far away</p>
</div>
<div style="height:3000px"></div>
"""

Call = Callable[[Page], Awaitable[object]]
FILES = [
    {"name": "a.txt", "mimeType": "text/plain", "buffer": b"a"},
    {"name": "b.txt", "mimeType": "text/plain", "buffer": b"b"},
]


async def _click_first_of_all(page: Page) -> None:
    await (await page.locator("#b").all())[0].click()


CALLS: list[tuple[str, Call, str, str | None]] = [
    ("click", lambda p: p.locator("#b").click(), "click", None),
    ("dblclick", lambda p: p.locator("#b").dblclick(), "double_click", None),
    ("fill", lambda p: p.locator("#name").fill("Ana"), "type", "Ana"),
    ("type", lambda p: p.locator("#name").type("Cy"), "type", "Cy"),
    (
        "press_sequentially",
        lambda p: p.locator("#name").press_sequentially("Bo"),
        "type",
        "Bo",
    ),
    ("press", lambda p: p.locator("#name").press("Enter"), "press", "Enter"),
    (
        "select_option",
        lambda p: p.locator("#size").select_option("l"),
        "select",
        "Large",
    ),
    ("check", lambda p: p.locator("#agree").check(), "check", "true"),
    ("uncheck", lambda p: p.locator("#opt").uncheck(), "uncheck", "false"),
    (
        "set_checked on",
        lambda p: p.locator("#agree").set_checked(True),
        "check",
        "true",
    ),
    (
        "set_checked off",
        lambda p: p.locator("#opt").set_checked(False),
        "uncheck",
        "false",
    ),
    ("hover", lambda p: p.locator("#b").hover(), "hover", None),
    ("drag_to", lambda p: p.locator("#src").drag_to(p.locator("#dst")), "drag", None),
    (
        "set_input_files",
        lambda p: p.locator("#file").set_input_files(FILES),  # pyright: ignore[reportArgumentType]
        "upload",
        "a.txt, b.txt",
    ),
    ("mouse.wheel", lambda p: p.mouse.wheel(0, 200), "scroll", "0,200"),
    ("goto", lambda p: p.goto("/controls"), "navigate", None),
    ("page.click", lambda p: p.click("#b"), "click", None),
    ("page.fill", lambda p: p.fill("#name", "Di"), "type", "Di"),
    ("page.drag_and_drop", lambda p: p.drag_and_drop("#src", "#dst"), "drag", None),
    ("get_by_label", lambda p: p.get_by_label("Name").fill("Ed"), "type", "Ed"),
    (
        "chained locator",
        lambda p: p.locator("body").locator("#b").first.click(),
        "click",
        None,
    ),
    ("inner_text", lambda p: p.locator("#b").inner_text(), "read", None),
    ("text_content", lambda p: p.locator("#b").text_content(), "read", None),
    ("input_value", lambda p: p.locator("#name").input_value(), "read", None),
    ("get_attribute", lambda p: p.locator("#b").get_attribute("id"), "read", None),
    ("all_inner_texts", lambda p: p.locator("#b").all_inner_texts(), "read", None),
    ("all_text_contents", lambda p: p.locator("#b").all_text_contents(), "read", None),
    ("page.inner_text", lambda p: p.inner_text("#b"), "read", None),
    ("a locator from all()", _click_first_of_all, "click", None),
]


@pytest.mark.parametrize(
    ("call", "action_type", "input"),
    [pytest.param(*rest, id=name) for name, *rest in CALLS],
)
async def test_every_logged_call_is_recorded_with_its_action_type_and_input(
    context: BrowserContext,
    site: FixtureServer,
    call: Call,
    action_type: str,
    input: str | None,
) -> None:
    url = site.page("/controls", CONTROLS)

    async def open_controls(ctx: EventContext) -> None:
        _ = await ctx.page.goto(url)

    async def do(ctx: EventContext) -> None:
        _ = await call(ctx.page)

    payload = await recorded(context, open_controls, do, _fail)

    entry = payload["ui_log"][1]
    assert (entry["action_type"], entry["input"]) == (action_type, input)


async def test_calls_the_spec_never_logs_are_not_recorded(
    context: BrowserContext, site: FixtureServer
) -> None:
    url = site.page("/controls", CONTROLS)

    async def open_controls(ctx: EventContext) -> None:
        _ = await ctx.page.goto(url)

    async def checks_then_click(ctx: EventContext) -> None:
        button = ctx.page.locator("#b")
        _ = await button.is_visible()
        _ = await button.is_enabled()
        _ = await button.count()
        await button.wait_for()
        await expect(button).to_have_text("Go")
        _ = await ctx.page.screenshot()
        await button.click()  # Playwright scrolls it into view by itself

    payload = await recorded(context, open_controls, checks_then_click, _fail)

    assert [e["action_type"] for e in payload["ui_log"]] == ["navigate", "click"]


async def test_a_scroll_gives_its_scrollable_ancestor_and_the_offsets_before_and_after(
    context: BrowserContext, site: FixtureServer
) -> None:
    url = site.page("/controls", CONTROLS)

    async def open_controls(ctx: EventContext) -> None:
        _ = await ctx.page.goto(url)

    async def scroll(ctx: EventContext) -> None:
        await ctx.page.locator("#far").scroll_into_view_if_needed()

    async def wheel(ctx: EventContext) -> None:
        await ctx.page.mouse.wheel(0, 100)

    payload = await recorded(context, open_controls, scroll, wheel, _fail)

    box, page = payload["ui_log"][1], payload["ui_log"][2]
    assert box["action_type"] == page["action_type"] == "scroll"
    assert box["ui_element_target"] == "Scroll box"
    assert box["previous_state"] == "0,0"
    dy = int(box["current_state"].split(",")[1])
    assert dy > 0
    assert box["input"] == f"0,{dy}"
    assert page["ui_element_target"] is None
    assert page["previous_state"] == "0,0"


async def test_goto_gives_the_url_as_target_and_the_urls_before_and_after_as_states(
    context: BrowserContext, site: FixtureServer
) -> None:
    first = site.page("/first", "<title>First</title>")
    second = site.page("/second", "<title>Second</title>")

    async def open_first(ctx: EventContext) -> None:
        _ = await ctx.page.goto(first)

    async def open_second(ctx: EventContext) -> None:
        _ = await ctx.page.goto("/second")

    payload = await recorded(context, open_first, open_second, _fail)

    entry = payload["ui_log"][1]
    assert entry["action_type"] == "navigate"
    assert entry["application"] == "First"
    assert entry["ui_element_target"] == second
    assert (entry["previous_state"], entry["current_state"]) == (first, second)


async def test_the_application_falls_back_to_the_url_host_and_the_group_to_root(
    context: BrowserContext, site: FixtureServer
) -> None:
    url = site.page("/bare", "<button>Go</button>")

    async def open_bare(ctx: EventContext) -> None:
        _ = await ctx.page.goto(url)

    async def click(ctx: EventContext) -> None:
        await ctx.page.get_by_role("button").click()

    payload = await recorded(context, open_bare, click, _fail)

    entry = payload["ui_log"][1]
    assert entry["application"] == "127.0.0.1"
    assert entry["ui_group"] == "root"
    assert payload["errored_act"]["application"] == "127.0.0.1"


# ------------------------------------------------------------------------------------ names and numbers


async def test_logged_events_number_up_through_the_run_with_one_pass_suffix_per_loop(
    context: BrowserContext, site: FixtureServer
) -> None:
    url = site.page("/list", LIST)
    p = Process("nested")
    with p.activity("Open"):

        @p.event("Open the list")
        async def open_list(ctx: EventContext) -> None:
            _ = await ctx.page.goto(url)

    with (
        p.for_each("rows", over="rows", as_="row"),
        p.for_each("cols", over="cols", as_="col"),
        p.activity("Read a cell"),
    ):

        @p.event("Read the cell")
        async def read_cell(ctx: EventContext) -> None:
            if (ctx.vars["row"], ctx.vars["col"]) == ("b", 1):
                ctx.fail("The cell is missing.")
            _ = await ctx.page.locator("#total").inner_text()

    async with FakeReparo([Fail("no")]) as reparo:
        _ = await run(
            p,
            context=context,
            reparo=ReparoSettings(base_url=reparo.url, robot_key=reparo.robot_key),
            case_id="c",
            variables={"rows": ["a", "b"], "cols": [0, 1]},
        )

    payload = reparo.payloads[0]
    assert [(e["event_id"], e["event_name"]) for e in payload["ui_log"]] == [
        ("1", "Open the list"),
        ("2", "Read the cell #0#0"),
        ("3", "Read the cell #0#1"),
        ("4", "Read the cell #1#0"),
    ]
    assert {e["activity_id"] for e in payload["ui_log"][1:]} == {"read_a_cell"}
    errored = payload["errored_act"]
    assert (errored["event_id"], errored["event_name"]) == ("5", "Read the cell #1#1")


# -------------------------------------------------------------------------------------- state placeholders

STATES = """
<title>States</title>
<button id="gone" onclick="this.remove()">Remove me</button>
<button id="twin" onclick="this.after(this.cloneNode(true))">Twin</button>
<div id="slot"></div>
<script>
  setTimeout(() => {
    document.querySelector('#slot').innerHTML = '<button id="late">Late</button>';
  }, 1200);
</script>
<p id="long">LONG</p>
"""


async def states_of(
    context: BrowserContext, site: FixtureServer, body: Body
) -> dict[str, Any]:
    url = site.page("/states", STATES.replace("LONG", "x" * 300))

    async def open_states(ctx: EventContext) -> None:
        _ = await ctx.page.goto(url)

    payload = await recorded(context, open_states, body, _fail)
    return payload["ui_log"][1]


async def test_an_element_removed_by_the_action_gives_no_longer_on_screen(
    context: BrowserContext, site: FixtureServer
) -> None:
    async def remove(ctx: EventContext) -> None:
        await ctx.page.locator("#gone").click()

    entry = await states_of(context, site, remove)

    assert entry["previous_state"] == "Remove me"
    assert entry["current_state"] == "[No longer on screen]"
    assert entry["ui_element_target"] == "Remove me"


async def test_a_state_that_cannot_be_read_in_500_ms_gives_a_placeholder_and_the_event_goes_on(
    context: BrowserContext, site: FixtureServer
) -> None:
    async def click_late(ctx: EventContext) -> None:
        await ctx.page.locator("#late").click()

    entry = await states_of(context, site, click_late)

    assert entry["previous_state"] == "[Previous state unavailable]"
    assert entry["current_state"] == "Late"
    assert entry["ui_element_target"] == "Late"


async def test_a_current_state_that_cannot_be_read_gives_its_placeholder(
    context: BrowserContext, site: FixtureServer
) -> None:
    async def twin(ctx: EventContext) -> None:
        await ctx.page.locator("#twin").click()  # now two elements match: strict mode

    entry = await states_of(context, site, twin)

    assert entry["previous_state"] == "Twin"
    assert entry["current_state"] == "[Current state unavailable]"


async def test_states_are_cut_at_200_characters(
    context: BrowserContext, site: FixtureServer
) -> None:
    async def read_long(ctx: EventContext) -> None:
        _ = await ctx.page.locator("#long").inner_text()

    entry = await states_of(context, site, read_long)

    assert entry["previous_state"] == entry["current_state"] == "x" * 200


# ------------------------------------------------------------------------------------- the Errored activity


async def _click_missing(ctx: EventContext) -> None:
    await ctx.page.locator("#missing").click(timeout=300)


async def _fill_missing(ctx: EventContext) -> None:
    await ctx.page.locator("#missing").fill("Ana", timeout=300)


async def _read_missing(ctx: EventContext) -> None:
    _ = await ctx.page.locator("#missing").inner_text(timeout=300)


async def _click_then_expect(ctx: EventContext) -> None:
    await ctx.page.get_by_role("button").click()
    await expect(ctx.page.get_by_role("button")).to_have_text("Nope", timeout=300)


@pytest.mark.parametrize(
    ("body", "action_type", "input"),
    [
        (_fail, "check", None),
        (_click_missing, "click", None),
        (_fill_missing, "type", "Ana"),
        (_read_missing, "read", None),
        (_click_then_expect, "click", None),
    ],
)
async def test_the_errored_activity_takes_the_next_number_and_the_attempted_call(
    context: BrowserContext,
    site: FixtureServer,
    body: Body,
    action_type: str,
    input: str | None,
) -> None:
    url = site.page("/form", FORM)

    async def open_form(ctx: EventContext) -> None:
        _ = await ctx.page.goto(url)

    payload = await recorded(context, open_form, body)

    assert [e["event_name"] for e in payload["ui_log"]] == ["Step 0"]
    errored = payload["errored_act"]
    assert errored["event_id"] == "2"
    assert errored["event_name"] == "Step 1"
    assert (errored["action_type"], errored["input"]) == (action_type, input)
    assert errored["application"] == "Spending"


# ----------------------------------------------------------------------------------------------- secrets


async def test_a_secret_events_input_and_states_are_masked_in_the_ui_log_and_errored_act(
    context: BrowserContext, site: FixtureServer
) -> None:
    url = site.page("/form", FORM)

    async def open_form(ctx: EventContext) -> None:
        _ = await ctx.page.goto(url)

    async def type_password(ctx: EventContext) -> None:
        await ctx.page.locator("#amount").fill("hunter2")

    payload = await recorded(
        context, open_form, type_password, _fill_missing, secret=True
    )

    entry = payload["ui_log"][1]
    assert (entry["input"], entry["previous_state"], entry["current_state"]) == (
        "***",
        "***",
        "***",
    )
    assert entry["ui_element_target"] == "Amount"
    assert payload["errored_act"]["input"] == "***"
    assert "hunter2" not in str(payload)


# ----------------------------------------------------------------------------------------------- logging


async def test_each_logged_event_is_logged_at_debug_with_typed_text_as_its_length(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    url = site.page("/form", FORM)

    async def open_form(ctx: EventContext) -> None:
        _ = await ctx.page.goto(url)

    async def type_amount(ctx: EventContext) -> None:
        await ctx.page.locator("#amount").fill("tops3cret")

    caplog.set_level(logging.DEBUG, logger="reparo_playwright_bridge")
    _ = await recorded(context, open_form, type_amount, _fail)

    logged = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and r.getMessage().startswith("Logged event ")
    ]
    assert len(logged) == 2
    message = logged[1].getMessage()
    assert message.startswith("Logged event 2: Step 1 (type on 'Amount'")
    assert "9 characters" in message
    assert all("tops3cret" not in r.getMessage() for r in caplog.records)
    assert logged[1].__dict__["event_id"] == "step_1"


async def test_a_checkbox_state_is_whether_it_is_checked(
    context: BrowserContext, site: FixtureServer
) -> None:
    url = site.page("/controls", CONTROLS)

    async def open_controls(ctx: EventContext) -> None:
        _ = await ctx.page.goto(url)

    async def agree(ctx: EventContext) -> None:
        await ctx.page.get_by_label("Agree").check()

    payload = await recorded(context, open_controls, agree, _fail)

    entry = payload["ui_log"][1]
    assert (entry["previous_state"], entry["current_state"]) == ("false", "true")
