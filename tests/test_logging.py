import logging

import pytest
from playwright.async_api import BrowserContext

import reparo_playwright_bridge
from reparo_playwright_bridge import EventContext, Process, ReparoSettings, run

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
