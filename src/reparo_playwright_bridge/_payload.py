"""The UI log and the Recovery payload Reparo receives."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from datetime import UTC, datetime

from reparo_playwright_bridge._context import Variables
from reparo_playwright_bridge._definition import EventNode, ProcessDefinition
from reparo_playwright_bridge._model_export import render
from reparo_playwright_bridge._settings import ReparoSettings

type JsonObject = dict[str, object]

PLATFORM = "playwright"
NO_CALL_ATTEMPTED = "check"
"""The Errored activity's `action_type` when the Event attempted no logged call.

Until the recording page records each Event's call, every UI log entry carries it too.
"""


def pass_suffix(passes: tuple[int, ...]) -> str:
    """` #<pass>` per enclosing loop, outermost first (` #1#2`), or nothing outside loops."""
    return " " + "".join(f"#{i}" for i in passes) if passes else ""


def event_name(event: EventNode, passes: tuple[int, ...]) -> str:
    """The grounded name plus its pass suffix: `Click Approve #1#2`."""
    return event.name + pass_suffix(passes)


@dataclass(frozen=True)
class LoggedEvent:
    """One entry of the UI log.

    Only the fields Reparo's schema requires are filled for now; the recording page fills the
    rest from Playwright.
    """

    number: int
    event: EventNode
    passes: tuple[int, ...]
    timestamp: str

    @classmethod
    def now(cls, number: int, event: EventNode, passes: tuple[int, ...]) -> LoggedEvent:
        return cls(number, event, passes, datetime.now(UTC).isoformat())


@dataclass(frozen=True)
class ErroredActivity:
    number: int
    event: EventNode
    passes: tuple[int, ...]
    error_code: str
    error_description: str


@dataclass(frozen=True)
class Payload:
    """The payload's JSON body, plus the counts its log line shows instead of its content."""

    body: JsonObject
    logged_events: int
    variable_count: int
    model_size: int
    """The size of `model` in bytes."""


def _common(
    case_id: str | int,
    number: int,
    event: EventNode,
    passes: tuple[int, ...],
    future_contract: bool,
) -> JsonObject:
    fields: JsonObject = {
        "case_id": str(case_id),
        "activity_id": event.activity.id,
        "activity_name": event.activity.name,
        "event_id": str(number),
        "event_name": event_name(event, passes),
    }
    if future_contract:
        fields["model_event_id"] = event.id
    return fields


def build_payload(
    definition: ProcessDefinition,
    settings: ReparoSettings,
    case_id: str | int,
    ui_log: list[LoggedEvent],
    errored: ErroredActivity,
    variables: Variables,
) -> Payload:
    """The Recovery payload, with the future-contract fields only when `future_contract` is on."""
    future = settings.future_contract
    entries: list[JsonObject] = [
        {
            **_common(case_id, logged.number, logged.event, logged.passes, future),
            "action_type": NO_CALL_ATTEMPTED,
            "application": None,
            "input": None,
            "ui_element_target": None,
            "ui_group": None,
            "timestamp": logged.timestamp,
            "previous_state": None,
            "current_state": None,
        }
        for logged in ui_log
    ]
    errored_act: JsonObject = {
        **_common(case_id, errored.number, errored.event, errored.passes, future),
        "action_type": NO_CALL_ATTEMPTED,
        "application": None,
        "input": None,
        "error_code": errored.error_code,
        "error_description": errored.error_description,
    }
    model = render(definition, settings.model_format)
    body: JsonObject = {
        "task_name": definition.name,
        "platform": PLATFORM,
        "os": platform.system() or "unknown",
        "variables": variables,
        "ui_log": entries,
        "errored_act": errored_act,
        "model": model,
    }
    if future and errored.event.activity.recovery_task is not None:
        body["recovery_task"] = errored.event.activity.recovery_task
    return Payload(body, len(entries), len(variables), len(model.encode()))
