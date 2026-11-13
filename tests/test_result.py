import pytest
from pydantic import ValidationError

from reparo_playwright_bridge import Failure, RunResult


@pytest.mark.parametrize(
    ("result", "line"),
    [
        (RunResult(outcome="completed"), "completed"),
        (
            RunResult(outcome="completed", recovery_id="r-1"),
            "completed after Recovery r-1",
        ),
        (
            RunResult(
                outcome="checks_failed",
                reason="1 completion check not met: budget_matches: expected 50, got 250",
            ),
            "checks_failed: 1 completion check not met: budget_matches: expected 50, got 250",
        ),
        (
            RunResult(
                outcome="checks_failed",
                reason="1 completion check not met: x: no",
                recovery_id="r-1",
            ),
            "checks_failed after Recovery r-1: 1 completion check not met: x: no",
        ),
        (
            RunResult(
                outcome="local",
                reason="Process bug: ZeroDivisionError: division by zero",
                failure=Failure(
                    type="local", event_id="open_requests", loop_iteration=[]
                ),
            ),
            "local at open_requests: Process bug: ZeroDivisionError: division by zero",
        ),
        (
            RunResult(
                outcome="local",
                reason="Missing or mistyped input Variables: approval_limit (missing, expected int)",
                failure=Failure(type="local"),
            ),
            "local: Missing or mistyped input Variables: approval_limit (missing, expected int)",
        ),
        (
            RunResult(
                outcome="recovery_failed",
                reason="Reparo could not recover the failure: no Approve button",
                failure=Failure(
                    type="recovery_failed",
                    event_id="click_approve",
                    loop_iteration=[0],
                    error_code="ElementNotFound",
                    error_description="The Approve button was not found",
                ),
                recovery_id="r-2",
            ),
            "recovery_failed at click_approve #0, Recovery r-2: Reparo could not recover the failure: no Approve button",
        ),
        (
            RunResult(
                outcome="recovery_unavailable",
                reason="no message from Reparo for 2 min",
                failure=Failure(
                    type="timeout", event_id="click_approve", loop_iteration=[1, 2]
                ),
            ),
            "recovery_unavailable/timeout at click_approve #1#2: no message from Reparo for 2 min",
        ),
        (
            RunResult(
                outcome="recovery_unavailable",
                reason="Reparo rejected the payload",
                failure=Failure(
                    type="payload_rejected", event_id="click_approve", loop_iteration=[]
                ),
            ),
            "recovery_unavailable/payload_rejected at click_approve: Reparo rejected the payload",
        ),
        (
            RunResult(
                outcome="continuation_rejected",
                reason="unknown Event id: decide",
                failure=Failure(
                    type="continuation_rejected",
                    event_id="click_approve",
                    loop_iteration=[0],
                ),
                recovery_id="r-3",
            ),
            "continuation_rejected at click_approve #0, Recovery r-3: unknown Event id: decide",
        ),
        (
            RunResult(
                outcome="continuation_failed",
                reason="Process bug: KeyError: 'amount'",
                failure=Failure(
                    type="continuation_failed",
                    event_id="read_request",
                    loop_iteration=[3],
                ),
                recovery_id="r-4",
            ),
            "continuation_failed at read_request #3, Recovery r-4: Process bug: KeyError: 'amount'",
        ),
    ],
)
def test_str_is_one_summary_line(result: RunResult, line: str) -> None:
    assert str(result) == line


def test_str_never_truncates_the_reason() -> None:
    reason = "Process bug: " + "x" * 1000
    result = RunResult(outcome="local", reason=reason, failure=Failure(type="local"))

    assert str(result) == f"local: {reason}"


def test_repr_stays_pydantics_default() -> None:
    assert repr(RunResult(outcome="completed")).startswith(
        "RunResult(outcome='completed'"
    )


def test_round_trips_through_json() -> None:
    result = RunResult(
        outcome="local",
        reason="Process bug: boom",
        failure=Failure(type="local", event_id="e", loop_iteration=[]),
    )

    assert RunResult.model_validate_json(result.model_dump_json()) == result
    assert result.model_dump(mode="json")["failure"]["event_id"] == "e"


@pytest.mark.parametrize(
    "fields",
    [
        {"outcome": "completed", "reason": "why"},
        {"outcome": "completed", "failure": Failure(type="local")},
        {"outcome": "checks_failed"},
        {"outcome": "local", "failure": Failure(type="local")},
        {"outcome": "local", "reason": "boom"},
        {"outcome": "local", "reason": "boom", "failure": Failure(type="timeout")},
        {
            "outcome": "recovery_failed",
            "reason": "no",
            "failure": Failure(type="local"),
        },
    ],
)
def test_inconsistent_results_are_rejected(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _ = RunResult.model_validate(fields)
