"""The Continuation resolver: turns Reparo's answer into the point the engine resumes from."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from reparo_playwright_bridge._context import Variables
from reparo_playwright_bridge._definition import (
    EventNode,
    LoopNode,
    Node,
    ProcessDefinition,
    SequenceNode,
    XorArm,
    XorNode,
)
from reparo_playwright_bridge._protocol import ContinuationContent

_ANSWER = "Reparo's Continuation"
"""How a rejection reason names the answer it rejects."""


@dataclass(frozen=True)
class InLoop:
    """The resume point is inside `loop`, in pass `pass_`."""

    loop: LoopNode
    pass_: int


@dataclass(frozen=True)
class InArm:
    """The resume point is inside `arm` of `xor`."""

    xor: XorNode
    arm: XorArm


Enclosing = InLoop | InArm


@dataclass(frozen=True)
class Target:
    """An Event to resume at, and the blocks the engine enters to reach it, outermost first."""

    event: EventNode
    within: tuple[Enclosing, ...] = ()

    @property
    def head(self) -> Node:
        """The node of the enclosing sequence the engine starts at."""
        if not self.within:
            return self.event
        first = self.within[0]
        return first.loop if isinstance(first, InLoop) else first.xor

    @property
    def pass_(self) -> int:
        """The pass of the loop at the head."""
        first = self.within[0]
        assert isinstance(first, InLoop)
        return first.pass_

    @property
    def arm(self) -> XorArm:
        """The arm of the gateway at the head."""
        first = self.within[0]
        assert isinstance(first, InArm)
        return first.arm

    @property
    def inner(self) -> Target:
        """This target, seen from inside the block at its head."""
        return Target(self.event, self.within[1:])


@dataclass(frozen=True)
class Resume:
    """Where the engine resumes, and with which Variables."""

    target: Target | None
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


_Block = LoopNode | tuple[XorNode, XorArm]
"""A block around a node: a loop, or an arm of a gateway."""


def _find(
    sequence: SequenceNode, node_id: str, around: list[_Block]
) -> tuple[Node, tuple[_Block, ...]] | None:
    """The node with `node_id` and the blocks around it, outermost first, in one depth-first pass.

    `around` is the stack of blocks entered so far; the search stops at the first match.
    """
    for node in sequence.nodes:
        if node.id == node_id:
            return node, tuple(around)
        match node:
            case EventNode():
                continue
            case LoopNode():
                bodies = [(node, node.body)]
            case XorNode():
                bodies = [((node, arm), arm.body) for arm in node.arms]
        for block, body in bodies:
            around.append(block)
            found = _find(body, node_id, around)
            _ = around.pop()
            if found is not None:
                return found
    return None


def resolve(
    definition: ProcessDefinition,
    answer: ContinuationContent,
    snapshot: Variables,
) -> Resume | Rejected | HookCrashed:
    """Validate a successful answer against the Process and decide the Variables it resumes with.

    `snapshot` is the failing Event's start snapshot.
    """
    located: tuple[EventNode, tuple[_Block, ...]] | None = None
    if not answer.process_finished:
        found = _locate(definition, answer)
        if isinstance(found, Rejected):
            return found
        located = found
    try:
        variables = _decide(definition, answer.variables, snapshot)
    except Exception as exc:  # noqa: BLE001  # the hook's own failure, reported by the engine
        return HookCrashed(exc)
    target: Target | None = None
    if located is not None:
        event, around = located
        entered = _enter(event, around, answer.loop_iteration, variables)
        if isinstance(entered, Rejected):
            return entered
        target = entered
    return Resume(target, variables, _changed(variables, snapshot))


def _locate(
    definition: ProcessDefinition, answer: ContinuationContent
) -> tuple[EventNode, tuple[_Block, ...]] | Rejected:
    event_id = answer.continue_from_model_event_id
    if event_id is None:
        return Rejected(f"{_ANSWER} names no Event and does not finish the Process")
    found = _find(definition.root, event_id, [])
    if found is None:
        return Rejected(f"{_ANSWER} names {event_id!r}, which is not an Event id")
    node, around = found
    match node:
        case LoopNode():
            return Rejected(f"{_ANSWER} names loop {event_id}, not an Event")
        case XorNode():
            return Rejected(f"{_ANSWER} names gateway {event_id}, not an Event")
        case EventNode() as event:
            pass
    depth = sum(isinstance(block, LoopNode) for block in around)
    passes = answer.loop_iteration
    if len(passes) != depth:
        return Rejected(
            f"{_ANSWER} gives {len(passes)} loop passes for Event {event_id}, "
            + f"which is inside {depth} loops"
        )
    return event, around


def _enter(
    event: EventNode,
    around: tuple[_Block, ...],
    passes: list[int],
    variables: Variables,
) -> Target | Rejected:
    """The path to `event` in the named passes, rebinding each loop item Variable on the way.

    A loop list that is missing or not a list is left for the engine, whose binding fails on it
    as a Process bug; the loops inside it are then not checked either.
    """
    remaining = iter(passes)
    within: list[Enclosing] = []
    checking = True
    for block in around:
        if not isinstance(block, LoopNode):
            within.append(InArm(*block))
            continue
        pass_ = next(remaining)
        within.append(InLoop(block, pass_))
        items = variables.get(block.over)
        if not checking or not isinstance(items, list):
            checking = False
            continue
        items = cast("list[object]", items)
        if not 0 <= pass_ < len(items):
            return Rejected(
                f"{_ANSWER} names pass {pass_} of loop {block.id}, "
                + f"but {block.over} has {len(items)} items"
            )
        variables[block.as_] = items[pass_]
    return Target(event, tuple(within))


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
