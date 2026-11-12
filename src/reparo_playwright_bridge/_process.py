"""The Process builder."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from types import MappingProxyType

from reparo_playwright_bridge._definition import (
    Activity,
    EventFn,
    EventNode,
    ProcessDefinition,
)
from reparo_playwright_bridge._model_export import ModelFormat, render


def _slug(name: str) -> str:
    return re.sub(r"[\W_]+", "_", name.lower()).strip("_")


class Process:
    """A Process: Events grouped into Activities, built with `with` blocks and decorators.

    `variables` declares the input Variables every run must receive, as `{name: type}`.
    Mistakes in the structure are Process bugs, raised here at definition time.
    """

    def __init__(
        self, name: str, *, variables: Mapping[str, type] | None = None
    ) -> None:
        inputs = dict(variables or {})
        for var, expected in inputs.items():
            if not isinstance(expected, type):  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard
                raise TypeError(
                    f"input Variable {var!r} must be declared with a type, got {expected!r}"
                )
        self._definition: ProcessDefinition = ProcessDefinition(
            name, MappingProxyType(inputs)
        )
        self._ids: set[str] = set()
        self._activities: dict[str, str] = {}  # Activity id -> name
        self._activity: Activity | None = None

    @property
    def name(self) -> str:
        return self._definition.name

    @property
    def input_variables(self) -> Mapping[str, type]:
        """The declared input Variables and their types."""
        return self._definition.inputs

    @contextmanager
    def activity(
        self, name: str, *, recoverable: bool = True, recovery_task: str | None = None
    ) -> Generator[None]:
        """Group the Events declared inside this block into one Activity."""
        if self._activity is not None:
            raise ValueError(
                f"Activity {name!r} is nested in Activity {self._activity.name!r}; Activities don't nest"
            )
        activity_id = _slug(name)
        if not activity_id:
            raise ValueError(
                f"Activity name {name!r} needs at least one letter or digit"
            )
        if activity_id in self._activities:
            existing = self._activities[activity_id]
            what = (
                "is a duplicate"
                if existing == name
                else f"has the same id as Activity {existing!r}"
            )
            raise ValueError(f"Activity {name!r} {what}; Activity names must be unique")
        self._activities[activity_id] = name
        self._activity = Activity(activity_id, name, recoverable, recovery_task)
        try:
            yield
        finally:
            self._activity = None

    def event(
        self, name: str, *, recoverable: bool = True, secret: bool = False
    ) -> Callable[[EventFn], EventFn]:
        """Declare the decorated `async def <id>(ctx)` as the next Event, with a grounded name.

        The Event id is the function name.
        """
        if not isinstance(name, str):  # pyright: ignore[reportUnnecessaryIsInstance]  # catches a bare @p.event
            raise TypeError(  # pyright: ignore[reportUnreachable]
                "@p.event needs a grounded name, for example @p.event('Click Approve')"
            )
        if not name.strip():
            raise ValueError("@p.event needs a non-empty grounded name")

        def register(fn: EventFn) -> EventFn:
            event_id = fn.__name__
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(f"Event {event_id} must be an async function")
            if not event_id.isidentifier():
                raise ValueError(f"Event id {event_id!r} must be a Python identifier")
            if self._activity is None:
                raise ValueError(
                    f"Event {event_id} is outside any Activity; declare it inside `with p.activity(...)`"
                )
            if event_id in self._ids:
                raise ValueError(
                    f"Event id {event_id} is already used in Process {self.name!r}; ids must be unique"
                )
            self._ids.add(event_id)
            self._definition.root.nodes.append(
                EventNode(
                    event_id,
                    name,
                    self._activity,
                    fn,
                    recoverable=recoverable,
                    secret=secret,
                )
            )
            return fn

        return register

    def to_model(self, model_format: ModelFormat = "bpmn") -> str:
        """The Process model Reparo reads, as BPMN 2.0 or the compact outline."""
        return render(self._definition, model_format)


def definition_of(process: Process) -> ProcessDefinition:
    return process._definition  # pyright: ignore[reportPrivateUsage]
