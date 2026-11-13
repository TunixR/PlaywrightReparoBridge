"""RunResult and Failure: how a run ended."""

from __future__ import annotations

from typing import Literal, Self, override

from pydantic import BaseModel, model_validator

Outcome = Literal[
    "completed",
    "checks_failed",
    "local",
    "recovery_failed",
    "recovery_unavailable",
    "continuation_rejected",
    "continuation_failed",
]

FailureType = Literal[
    "local",
    "recovery_failed",
    "recovery_unavailable",
    "continuation_rejected",
    "continuation_failed",
    "payload_rejected",
    "timeout",
]

_REACHED_END: frozenset[Outcome] = frozenset({"completed", "checks_failed"})
_SUB_CAUSES: dict[Outcome, frozenset[FailureType]] = {
    "recovery_unavailable": frozenset({"payload_rejected", "timeout"}),
}


class Failure(BaseModel):
    """Where and why a failed run failed.

    `type` is a real sub-cause only where one exists (`payload_rejected` or `timeout` under
    `recovery_unavailable`); otherwise it equals the outcome. `event_id` and `loop_iteration`
    are None outside an Event.
    """

    type: FailureType
    event_id: str | None = None
    loop_iteration: list[int] | None = None
    error_code: str | None = None
    error_description: str | None = None


class RunResult(BaseModel):
    """How a run of a Process ended. `str(result)` is one summary line."""

    outcome: Outcome
    reason: str | None = None
    failure: Failure | None = None
    recovery_id: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.outcome == "completed":
            if self.reason is not None:
                raise ValueError("a completed run has no reason")
        elif not self.reason:
            raise ValueError(f"a {self.outcome} run needs a reason")
        if self.outcome in _REACHED_END:
            if self.failure is not None:
                raise ValueError(f"a {self.outcome} run has no failure")
        elif self.failure is None:
            raise ValueError(f"a {self.outcome} run needs a failure")
        elif (
            self.failure.type != self.outcome
            and self.failure.type not in _SUB_CAUSES.get(self.outcome, frozenset())
        ):
            raise ValueError(
                f"failure type {self.failure.type} does not fit outcome {self.outcome}"
            )
        return self

    @override
    def __str__(self) -> str:
        line = self.outcome
        failure = self.failure
        if failure is not None:
            if failure.type != self.outcome:
                line += f"/{failure.type}"
            if failure.event_id is not None:
                line += f" at {failure.event_id}"
                passes = "".join(f"#{i}" for i in failure.loop_iteration or [])
                if passes:
                    line += f" {passes}"
        if self.recovery_id is not None:
            joiner = " after" if self.outcome in _REACHED_END else ","
            line += f"{joiner} Recovery {self.recovery_id}"
        if self.reason is not None:
            line += f": {self.reason}"
        return line
