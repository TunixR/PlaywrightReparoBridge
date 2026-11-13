"""The Process definition: what the builder produces, and what the engine and the export read."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from reparo_playwright_bridge._context import CheckContext, EventContext

EventFn = Callable[[EventContext], Awaitable[None]]
CheckFn = Callable[[CheckContext], Awaitable[object]]
"""A Completion check: met is `(True, None)`, unmet is `(False, reason)`; anything else crashes it."""


@dataclass(frozen=True)
class Activity:
    id: str
    """A stable slug of the name."""
    name: str
    recoverable: bool
    recovery_task: str | None


@dataclass(frozen=True)
class EventNode:
    id: str
    """The Event id: the function name."""
    name: str
    """The grounded name."""
    activity: Activity
    fn: EventFn
    recoverable: bool
    secret: bool


@dataclass
class SequenceNode:
    nodes: list[Node] = field(default_factory=list)


@dataclass
class LoopNode:
    """A `for_each`: its body runs once per item of `vars[over]`, with `vars[as_]` bound to it."""

    id: str
    over: str
    as_: str
    body: SequenceNode = field(default_factory=SequenceNode)


Condition = Callable[[Mapping[str, Any]], object]  # pyright: ignore[reportExplicitAny]  # Variables hold any value
"""A pure function of the Variables; its truth picks an XOR arm."""


@dataclass
class XorArm:
    label: str
    condition: Condition | None
    """None for the `else_` arm."""
    body: SequenceNode = field(default_factory=SequenceNode)


@dataclass
class XorNode:
    """An n-way exclusive gateway: the first arm whose condition holds runs."""

    id: str
    arms: list[XorArm] = field(default_factory=list)


Node = EventNode | LoopNode | XorNode

ContinuationHook = Callable[
    [Mapping[str, Any], Mapping[str, Any]],  # pyright: ignore[reportExplicitAny]
    Mapping[str, Any],  # pyright: ignore[reportExplicitAny]
]
"""`(returned, local) -> Variables`: decides the Variables a Continuation resumes with."""


@dataclass(frozen=True)
class CompletionCheck:
    id: str
    """The function name."""
    name: str
    """The grounded name."""
    fn: CheckFn


@dataclass
class ProcessDefinition:
    name: str
    inputs: Mapping[str, type]
    root: SequenceNode = field(default_factory=SequenceNode)
    checks: list[CompletionCheck] = field(default_factory=list)
    """The Completion checks, in declaration order."""
    on_continuation: ContinuationHook | None = None


ELSE_LABEL = "otherwise"
"""The arm label of an `else_`."""

JOIN_SUFFIX = "_join"
"""The BPMN id of an XOR's join gateway is the gateway id plus this."""
NEXT_SUFFIX = "_next"
"""The BPMN id of a loop's tail split is the loop id plus this."""
