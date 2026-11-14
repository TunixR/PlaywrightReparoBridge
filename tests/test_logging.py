import base64
import logging
from typing import Literal

import pytest
from playwright.async_api import BrowserContext

import reparo_playwright_bridge
from reparo_playwright_bridge import (
    CheckContext,
    EventContext,
    Process,
    ReparoSettings,
    run,
)
from reparo_playwright_bridge.testing import (
    Act,
    Continue,
    Fail,
    FakeReparo,
    Locate,
    Step,
)
from tests.fixture_server import FixtureServer

ROBOT_KEY_HEADER = "X-ROBOT-KEY"

PACKAGE = "reparo_playwright_bridge"


def test_the_logger_tree_has_only_a_null_handler() -> None:
    assert reparo_playwright_bridge.__name__ == PACKAGE
    root = logging.getLogger(PACKAGE)

    assert [type(h) for h in root.handlers] == [logging.NullHandler]
    assert root.level == logging.NOTSET
    for child in ("run", "recovery", "checks", "report", "testing"):
        logger = logging.getLogger(f"{PACKAGE}.{child}")
        assert logger.handlers == []
        assert logger.level == logging.NOTSET
        assert logger.propagate


def one_event(body_raises: bool) -> Process:
    p = Process("logged")
    with p.activity("Do it"):

        @p.event("Do the thing")
        async def do_it(ctx: EventContext) -> None:
            if body_raises:
                raise RuntimeError("boom")
            _ = await ctx.page.locator("body").inner_text()

    return p


@pytest.mark.parametrize(
    ("body_raises", "level"), [(False, logging.INFO), (True, logging.ERROR)]
)
async def test_a_run_logs_its_start_and_exactly_one_outcome_line(
    context: BrowserContext,
    reparo: ReparoSettings,
    caplog: pytest.LogCaptureFixture,
    body_raises: bool,
    level: int,
) -> None:
    caplog.set_level(logging.DEBUG, logger=PACKAGE)

    result = await run(
        one_event(body_raises), context=context, reparo=reparo, case_id="case-7"
    )

    run_records = [r for r in caplog.records if r.name == f"{PACKAGE}.run"]
    start = run_records[0]
    assert start.levelno == logging.INFO
    assert "logged" in start.getMessage()
    assert "case-7" in start.getMessage()
    outcome_lines = [r for r in run_records if r.getMessage() == str(result)]
    assert len(outcome_lines) == 1
    assert outcome_lines[0] is run_records[-1]
    assert outcome_lines[0].levelno == level


async def test_event_log_lines_carry_structured_extras(
    context: BrowserContext, reparo: ReparoSettings, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=PACKAGE)

    _ = await run(one_event(False), context=context, reparo=reparo, case_id=9)

    event_lines = [r for r in caplog.records if getattr(r, "event_id", None) == "do_it"]
    assert event_lines
    for record in event_lines:
        assert getattr(record, "case_id", None) == 9
        assert getattr(record, "activity_id", None) == "do_it"
        assert getattr(record, "loop_iteration", None) == []


# --- Never logged: a Recovering run at DEBUG leaks no secret, typed text, value or screenshot.

FORM = """
<input id="note">
<button id="menu" onclick="document.getElementById('approve').hidden = false">More</button>
<button id="approve" hidden
  onclick="document.getElementById('status').textContent = 'approved'">Approve</button>
<p id="status">pending</p>
"""

# Event bodies use these by name, so a traceback's source lines never show the values.
INPUT_VALUE = "input-value-R4"
WRITTEN_VALUE = "written-value-W8"
CONTINUED_VALUE = "continued-value-C2"
FILLED_TEXT = "filled-text-F6"
TYPED_TEXT = "typed-text-T3"
RECOVERY_TYPED_TEXT = "recovery-typed-X5"


def form_process(*, fail_on: Literal["click", "fill"]) -> Process:
    """Fill and type into a note, then click Approve, which the page hides behind a menu.

    `fail_on` picks the failing call: `click` (a hidden button) or `fill` (a missing field).
    """
    p = Process("never logged", variables={"customer": str})
    with p.activity("Fill the form"):

        @p.event("Open the form")
        async def open_form(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/form")

        @p.event("Fill the note")
        async def fill_note(ctx: EventContext) -> None:
            ctx.vars["note"] = WRITTEN_VALUE
            selector = "#missing" if fail_on == "fill" else "#note"
            await ctx.page.locator(selector).fill(FILLED_TEXT, timeout=300)

        @p.event("Type in the note")
        async def type_note(ctx: EventContext) -> None:
            await ctx.page.locator("#note").press_sequentially(TYPED_TEXT)

        @p.event("Click Approve")
        async def click_approve(ctx: EventContext) -> None:
            await ctx.page.locator("#approve").click(timeout=300)

        @p.event("Read the status")
        async def read_status(ctx: EventContext) -> None:
            ctx.vars["status"] = await ctx.page.locator("#status").text_content()

    @p.completion_check("The request is approved")
    async def approved(ctx: CheckContext) -> tuple[bool, str | None]:
        status = ctx.vars.get("status")
        return (True, None) if status == "approved" else (False, f"status {status}")

    return p


RECOVERED: list[Step] = [
    Act("left_single", Locate("#note")),
    Act("type", content=RECOVERY_TYPED_TEXT),
    Act("left_single", Locate("#menu")),
    Act("left_single", Locate("#approve")),
    Continue("read_status", [], variables={"customer": CONTINUED_VALUE}),
]
FAILED: list[Step] = [
    Act("type", content=RECOVERY_TYPED_TEXT),
    Act("left_single", [5000, 5000]),  # off the page: a failed action
    Fail("no idea"),
]


def rendered(record: logging.LogRecord) -> str:
    """Everything a handler could ship: message, traceback, stack and every extra field."""
    return logging.Formatter("%(message)s").format(record) + repr(vars(record))


@pytest.mark.parametrize(
    ("fail_on", "script", "outcome"),
    [("click", RECOVERED, "completed"), ("fill", FAILED, "recovery_failed")],
    ids=["recovered", "failed"],
)
async def test_a_recovering_run_at_debug_logs_no_secret_text_value_or_screenshot(
    context: BrowserContext,
    site: FixtureServer,
    caplog: pytest.LogCaptureFixture,
    fail_on: Literal["click", "fill"],
    script: list[Step],
    outcome: str,
) -> None:
    _ = site.page("/form", FORM)
    caplog.set_level(logging.DEBUG)

    async with FakeReparo(script, context=context) as reparo:
        result = await run(
            form_process(fail_on=fail_on),
            context=context,
            reparo=ReparoSettings(base_url=reparo.url, robot_key=reparo.robot_key),
            case_id=3,
            variables={"customer": INPUT_VALUE},
        )

    assert result.outcome == outcome, result.reason
    assert result.recovery_id is not None
    screenshots = [r for r in reparo.replies if isinstance(r, bytes)]
    assert screenshots
    secrets = {
        "robot key": reparo.robot_key,
        "auth header name": ROBOT_KEY_HEADER.lower(),  # the bridge has no reason to name it
        "input Variable value": INPUT_VALUE,
        "written Variable value": WRITTEN_VALUE,
        "continued Variable value": CONTINUED_VALUE,
        "filled text": FILLED_TEXT,
        "typed text": TYPED_TEXT,
        "Recovery typed text": RECOVERY_TYPED_TEXT,
        "JPEG bytes": "\xff\xd8\xff",
        "JPEG as base64": "/9j/",
    }
    for i, shot in enumerate(screenshots):
        middle = len(shot) // 2
        secrets[f"screenshot {i} bytes"] = repr(shot[middle : middle + 16])[2:-1]
        b64 = base64.b64encode(shot).decode()
        secrets[f"screenshot {i} base64"] = b64[len(b64) // 2 :][:24]
    logs = [(r.name, rendered(r).lower()) for r in caplog.records]
    assert any(name == f"{PACKAGE}.recovery" for name, _ in logs)
    leaks = [
        f"{what} in {name}"
        for what, needle in secrets.items()
        for name, text in logs
        if needle.lower() in text
    ]
    assert leaks == []


async def test_recovery_lines_carry_the_failing_events_extras_and_every_line_the_case(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    _ = site.page("/form", FORM)
    caplog.set_level(logging.DEBUG, logger=PACKAGE)

    async with FakeReparo(RECOVERED, context=context) as reparo:
        result = await run(
            form_process(fail_on="click"),
            context=context,
            reparo=ReparoSettings(base_url=reparo.url, robot_key=reparo.robot_key),
            case_id="case-11",
            variables={"customer": INPUT_VALUE},
        )

    assert result.outcome == "completed", result.reason
    bridge = [
        r
        for r in caplog.records
        if r.name.startswith(PACKAGE) and r.name != f"{PACKAGE}.testing"
    ]
    assert {r.name for r in bridge} >= {
        f"{PACKAGE}.{child}" for child in ("run", "recovery", "checks", "report")
    }
    assert [
        r.getMessage() for r in bridge if getattr(r, "case_id", None) != "case-11"
    ] == []
    recovery = [r for r in bridge if r.name == f"{PACKAGE}.recovery"]
    # Recovery started, connect attempt, payload sent, Observations and actions, done received.
    assert len(recovery) >= 5
    for record in recovery:
        assert getattr(record, "event_id", None) == "click_approve", record.getMessage()
        assert getattr(record, "loop_iteration", None) == []
        assert getattr(record, "activity_id", None) == "fill_the_form"
