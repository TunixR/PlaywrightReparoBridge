# pyright: reportAny=false
# (FakeReparo records payloads as decoded JSON, whose values are any JSON.)
"""The spending-approval example end to end: every case x UI against FakeReparo on real Chromium."""

from __future__ import annotations

import pytest
from playwright.async_api import Browser

from examples.spending_approval.cases import CaseName
from examples.spending_approval.example import run_case


@pytest.mark.parametrize("case", ["single", "full"])
async def test_under_the_original_ui_the_run_completes_without_a_recovery(
    browser: Browser, case: CaseName
) -> None:
    run = await run_case(browser, ui="original", case=case)

    assert run.result.outcome == "completed", run.result.reason
    assert run.reparo is not None
    assert run.reparo.payloads == []


# The requests of each case, by priority; Ana's (104, 300) is the first.
PENDING: dict[CaseName, list[int]] = {
    "single": [104],
    "full": [104, 102, 105, 101, 103],
}


@pytest.mark.parametrize("case", ["single", "full"])
async def test_under_the_changed_ui_reparo_recovers_click_approve_and_the_run_completes(
    browser: Browser, case: CaseName
) -> None:
    run = await run_case(browser, ui="changed", case=case)

    assert run.result.outcome == "completed", run.result.reason
    assert str(run.result).startswith("completed after Recovery ")
    assert run.reparo is not None
    payload = run.reparo.payloads[0]
    assert payload["errored_act"]["model_event_id"] == "click_approve"
    assert payload["errored_act"]["event_name"] == "Click Approve #0"
    assert [entry["event_name"] for entry in payload["ui_log"]] == [
        "Open the requests page",
        "Read the pending requests",
        "Open the request #0",
        "Read the request #0",
    ]
    assert "click_approve" in payload["model"]
    assert "read_final_budget" in payload["model"]
    assert payload["variables"] == {
        "approval_limit": 500,
        "pending": PENDING[case],
        "request_id": 104,
        "amount": 300,
        "remaining_budget": 1000,
    }
