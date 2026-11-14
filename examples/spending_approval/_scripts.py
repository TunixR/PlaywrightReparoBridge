"""FakeReparo's Recovery scripts for the example's cases, built from the case rows.

Under the changed UI the run fails at `click_approve` in pass 0 (Ana's request), on the
request's detail page. The script plays what Reparo would do in the UI from there:

- `single`: open "Actions ▾", click Approve, and continue at `read_final_budget`;
- `full`: finish every remaining pass the same way (approve, or reject with the right reason),
  then continue at `read_final_budget`. Continuing at pass 1 would fail again at Cleo's Approve.

Variables are echoed back unchanged. The library ships no helper that knows about cases.
"""

from __future__ import annotations

from examples.spending_approval.cases import BUDGET_REASON, LIMIT_REASON, Case, Row
from reparo_playwright_bridge.testing import Act, Continue, Locate, Silence, Step

_PAUSE = Silence(1.0)
"""After every Recovery action: the page settles (a click may navigate) before the next target
is located, and a headed run can be followed by eye."""


def recovery_script(case: Case) -> list[Step]:
    """The script for `case`, starting on the detail page of its first request."""
    script: list[Step] = []
    for index, row in enumerate(case.by_priority()):
        if index > 0:
            script += _open(row)
        script += _decide(row)
    script.append(Continue("read_final_budget", []))
    return script


def _click(selector: str) -> list[Step]:
    return [Act("left_single", Locate(selector)), _PAUSE]


def _open(row: Row) -> list[Step]:
    """From a detail page, back to the list, then into `row`'s request."""
    return _click('a:text-is("Back to requests")') + _click(
        f'a[href="/requests/{row.request_id}"]'
    )


def _decide(row: Row) -> list[Step]:
    if row.expected_decision == "approve":
        return _click("#actions-button") + _click('[role=menuitem]:text-is("Approve")')
    reason = LIMIT_REASON if row.expected_decision == "reject_limit" else BUDGET_REASON
    return (
        _click("#reason-button")
        + _click(f'[role=option]:text-is("{reason}")')
        + _click('button:text-is("Reject")')
    )
