"""The Process definition: what the builder produces, and what the engine and the export read."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from reparo_playwright_bridge._context import EventContext

EventFn = Callable[[EventContext], Awaitable[None]]


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
    nodes: list[EventNode] = field(default_factory=list)


@dataclass
class ProcessDefinition:
    name: str
    inputs: Mapping[str, type]
    root: SequenceNode = field(default_factory=SequenceNode)
