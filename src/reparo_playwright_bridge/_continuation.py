"""The Continuation resolver: turns Reparo's answer into the point the engine resumes from."""

from __future__ import annotations

import copy
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import cast

from reparo_playwright_bridge._context import Variables
from reparo_playwright_bridge._definition import (
    EventNode,
    LoopNode,
    Node,
    ProcessDefinition,
    SequenceNode,
    XorNode,
)
from reparo_playwright_bridge._protocol import ContinuationContent

_ANSWER = "Reparo's Continuation"
"""How a rejection reason names the answer it rejects."""


@dataclass(frozen=True)
class Resume:
    """Where the engine resumes, and with which Variables."""

    event: EventNode | None
    """None when Reparo finished the Process: no more Events run."""
    variables: Variables
    changed: list[str]
    """The names of the Variables that differ from the failing Event's start snapshot, sorted."""


@dataclass(frozen=True)
class Rejected:
    """An answer the bridge can't resume from."""

    reason: str


@dataclass(frozen=True)
class HookCrashed:
    """The Process's `on_continuation` hook raised, or returned something other than Variables."""

    exc: Exception


@dataclass(frozen=True)
class _Placed:
    """A node of the Process, with the blocks around it."""

    node: Node
    loop_depth: int
    nested: bool
    """Inside a loop or an XOR arm."""


def _place(
    sequence: SequenceNode, depth: int = 0, nested: bool = False
) -> Iterator[tuple[str, _Placed]]:
    for node in sequence.nodes:
        yield node.id, _Placed(node, depth, nested)
        match node:
            case EventNode():
                pass
            case LoopNode():
                yield from _place(node.body, depth + 1, True)
            case XorNode():
                for arm in node.arms:
                    yield from _place(arm.body, depth, True)


def resolve(
    definition: ProcessDefinition,
    answer: ContinuationContent,
    snapshot: Variables,
) -> Resume | Rejected | HookCrashed:
    """Validate a successful answer against the Process and decide the Variables it resumes with.

    `snapshot` is the failing Event's start snapshot.
    """
    event: EventNode | None = None
    if not answer.process_finished:
        located = _locate(definition, answer)
        if isinstance(located, Rejected):
            return located
        event = located
    try:
        variables = _decide(definition, answer.variables, snapshot)
    except Exception as exc:  # noqa: BLE001  # the hook's own failure, reported by the engine
        return HookCrashed(exc)
    return Resume(event, variables, _changed(variables, snapshot))


def _locate(
    definition: ProcessDefinition, answer: ContinuationContent
) -> EventNode | Rejected:
    event_id = answer.continue_from_model_event_id
    if event_id is None:
        return Rejected(f"{_ANSWER} names no Event and does not finish the Process")
    placed = dict(_place(definition.root)).get(event_id)
    if placed is None:
        return Rejected(f"{_ANSWER} names {event_id!r}, which is not an Event id")
    match placed.node:
        case LoopNode():
            return Rejected(f"{_ANSWER} names loop {event_id}, not an Event")
        case XorNode():
            return Rejected(f"{_ANSWER} names gateway {event_id}, not an Event")
        case EventNode() as event:
            pass
    passes = answer.loop_iteration
    if len(passes) != placed.loop_depth:
        return Rejected(
            f"{_ANSWER} gives {len(passes)} loop passes for Event {event_id}, "
            + f"which is inside {placed.loop_depth} loops"
        )
    if placed.nested:
        return Rejected(
            f"{_ANSWER} names Event {event_id}, inside a loop or a branch; "
            + "resuming there is not supported yet"
        )
    return event


def _decide(
    definition: ProcessDefinition, returned: Variables, snapshot: Variables
) -> Variables:
    """Returned keys override the snapshot key by key; the `on_continuation` hook, if any, then decides."""
    hook = definition.on_continuation
    if hook is None:
        return {**copy.deepcopy(snapshot), **copy.deepcopy(returned)}
    decided = cast("object", hook(copy.deepcopy(returned), copy.deepcopy(snapshot)))
    if not isinstance(decided, Mapping):
        raise TypeError(
            f"on_continuation returned a {type(decided).__name__}, not a mapping of Variables"
        )
    variables = dict(cast("Mapping[object, object]", decided))
    if not all(isinstance(name, str) for name in variables):
        raise TypeError(
            "on_continuation returned Variables whose names are not all str"
        )
    return cast("Variables", variables)


def _changed(variables: Variables, snapshot: Variables) -> list[str]:
    return sorted(
        name
        for name in variables.keys() | snapshot.keys()
        if name not in variables
        or name not in snapshot
        or variables[name] != snapshot[name]
    )
