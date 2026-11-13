"""The UI log and the Recovery payload Reparo receives."""

from __future__ import annotations

import platform
from dataclasses import dataclass

from reparo_playwright_bridge._context import Variables
from reparo_playwright_bridge._definition import EventNode, ProcessDefinition
from reparo_playwright_bridge._model_export import render
from reparo_playwright_bridge._recording import LoggedCall
from reparo_playwright_bridge._settings import ReparoSettings

type JsonObject = dict[str, object]

PLATFORM = "playwright"
NO_CALL_ATTEMPTED = "check"
"""The Errored activity's `action_type` when the Event attempted no logged call."""
MASK = "***"
"""A secret Event's `input` and states, wherever Reparo could see them."""


def pass_suffix(passes: tuple[int, ...]) -> str:
    """` #<pass>` per enclosing loop, outermost first (` #1#2`), or nothing outside loops."""
    return " " + "".join(f"#{i}" for i in passes) if passes else ""


def event_name(event: EventNode, passes: tuple[int, ...]) -> str:
    """The grounded name plus its pass suffix: `Click Approve #1#2`."""
    return event.name + pass_suffix(passes)


@dataclass(frozen=True)
class LoggedEvent:
    """One entry of the UI log: an Event that ended, and the one logged call it is recorded as."""

    number: int
    event: EventNode
    passes: tuple[int, ...]
    application: str | None
    call: LoggedCall


@dataclass(frozen=True)
class ErroredActivity:
    """The failing Event, with the call it attempted (`None` when it attempted none)."""

    number: int
    event: EventNode
    passes: tuple[int, ...]
    application: str | None
    attempted: tuple[str, str | None] | None
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


def _masked(event: EventNode, value: str | None) -> str | None:
    """`value`, or the mask for a secret Event; a missing value stays missing."""
    return MASK if event.secret and value is not None else value


def _common(
    case_id: str | int,
    number: int,
    event: EventNode,
    passes: tuple[int, ...],
) -> JsonObject:
    return {
        "case_id": str(case_id),
        "activity_id": event.activity.id,
        "activity_name": event.activity.name,
        "event_id": str(number),
        "event_name": event_name(event, passes),
        "model_event_id": event.id,
    }


def build_payload(
    definition: ProcessDefinition,
    settings: ReparoSettings,
    case_id: str | int,
    ui_log: list[LoggedEvent],
    errored: ErroredActivity,
    variables: Variables,
    capabilities: JsonObject,
) -> Payload:
    """The Recovery payload, in Reparo's v1 recovery contract."""
    entries: list[JsonObject] = [
        {
            **_common(case_id, logged.number, logged.event, logged.passes),
            "action_type": logged.call.action_type,
            "application": logged.application,
            "input": _masked(logged.event, logged.call.input),
            "ui_element_target": logged.call.target,
            "ui_group": logged.call.group,
            "timestamp": logged.call.timestamp,
            "previous_state": _masked(logged.event, logged.call.previous_state),
            "current_state": _masked(logged.event, logged.call.current_state),
        }
        for logged in ui_log
    ]
    action_type, input = errored.attempted or (NO_CALL_ATTEMPTED, None)
    errored_act: JsonObject = {
        **_common(case_id, errored.number, errored.event, errored.passes),
        "action_type": action_type,
        "application": errored.application,
        "input": _masked(errored.event, input),
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
        "capabilities": capabilities,
    }
    if errored.event.activity.recovery_task is not None:
        body["recovery_task"] = errored.event.activity.recovery_task
    return Payload(body, len(entries), len(variables), len(model.encode()))
