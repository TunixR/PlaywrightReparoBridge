"""The Process builder."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from types import MappingProxyType

from reparo_playwright_bridge._definition import (
    ELSE_LABEL,
    JOIN_SUFFIX,
    NEXT_SUFFIX,
    Activity,
    CheckFn,
    CompletionCheck,
    Condition,
    ContinuationHook,
    EventFn,
    EventNode,
    LoopNode,
    ProcessDefinition,
    SequenceNode,
    XorArm,
    XorNode,
)
from reparo_playwright_bridge._model_export import ModelFormat, render


def _slug(name: str) -> str:
    return re.sub(r"[\W_]+", "_", name.lower()).strip("_")


def _check_grounded_name(decorator: str, name: object, example: str) -> None:
    if not isinstance(name, str):  # catches a bare decorator, which passes the function
        raise TypeError(
            f"@p.{decorator} needs a grounded name, for example @p.{decorator}({example!r})"
        )
    if not name.strip():
        raise ValueError(f"@p.{decorator} needs a non-empty grounded name")


def _check_async(kind: str, fn: Callable[..., object]) -> str:
    """The id of a decorated Event or Completion check: its function name, once it is async."""
    if not inspect.iscoroutinefunction(fn):
        raise TypeError(f"{kind} {fn.__name__} must be an async function")
    return fn.__name__


def _check_arm(keyword: str, cond: object, label: object) -> None:
    if not callable(cond):
        raise TypeError(
            f"p.{keyword}() needs a condition: a function of the Variables, got {cond!r}"
        )
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"p.{keyword}() needs a non-empty arm label")


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
        self._ids: dict[str, str] = {}  # id -> what uses it
        self._activities: dict[str, str] = {}  # Activity id -> name
        self._activity: Activity | None = None
        self._sequences: list[SequenceNode] = [self._definition.root]
        """The open blocks, innermost last: new nodes go into the last one. It acts as a stack of tree nodes, where the first is the root, and the last is the current block being built.
        The final value of sequences after going over the definition is a single root node."""
        self._open_xor: XorNode | None = None
        """The XOR gateway whose arm closed last: an `elif_` / `else_` right after it adds to it."""

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
        self._open_xor = None
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
        _check_grounded_name("event", name, "Click Approve")

        def register(fn: EventFn) -> EventFn:
            event_id = _check_async("Event", fn)
            if self._activity is None:
                raise ValueError(
                    f"Event {event_id} is outside any Activity; declare it inside `with p.activity(...)`"
                )
            self._claim_id("Event", event_id)
            self._sequences[-1].nodes.append(
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

    def completion_check(self, name: str) -> Callable[[CheckFn], CheckFn]:
        """Declare the decorated `async def <id>(ctx)` as a Completion check, with a grounded name.

        Checks run in declaration order once the Process reaches its end. Each returns
        `(True, None)` when met or `(False, reason)` when not. Declare them at the builder's top
        level, outside every block.
        """
        _check_grounded_name(
            "completion_check", name, "the remaining budget is as expected"
        )

        def register(fn: CheckFn) -> CheckFn:
            check_id = _check_async("Completion check", fn)
            if self._activity is not None or len(self._sequences) > 1:
                raise ValueError(
                    f"Completion check {check_id} is inside a block; "
                    + "declare Completion checks at the builder's top level"
                )
            self._claim_id("Completion check", check_id)
            self._definition.checks.append(CompletionCheck(check_id, name, fn))
            return fn

        return register

    def on_continuation(self, fn: ContinuationHook) -> ContinuationHook:
        """Register the decorated function as the hook that decides a Continuation's Variables.

        It is called as `fn(returned, local)`: `returned` holds the Variables Reparo returned,
        `local` the failing Event's start snapshot. What it returns is the Variables the run
        resumes with. Without a hook, returned keys override local ones key by key.
        A Process takes at most one hook.
        """
        if self._definition.on_continuation is not None:
            raise ValueError(
                f"p.on_continuation is already registered; a Process takes at most one hook, got {getattr(fn, '__name__', fn)!r} too"
            )
        self._definition.on_continuation = fn
        return fn

    @contextmanager
    def for_each(self, id: str, *, over: str, as_: str) -> Generator[None]:
        """Run the block once per item of the list Variable `over`, binding the item to `as_`.

        Pass *i* binds `vars[as_] = vars[over][i]`. Loops may nest.
        """
        for what, value in (("over", over), ("as_", as_)):
            if not isinstance(value, str) or not value:  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard
                raise ValueError(f"Loop {id!r} needs a Variable name for {what}")
        if over == as_:
            raise ValueError(
                f"Loop {id!r} binds its item to the list it walks ({over!r})"
            )
        self._claim_id("Loop", id, generates=NEXT_SUFFIX)
        loop = LoopNode(id, over, as_)
        self._sequences[-1].nodes.append(loop)
        with self._block(loop.body):
            yield

    @contextmanager
    def if_(self, id: str, cond: Condition, *, label: str) -> Generator[None]:
        """Open an XOR gateway: its first arm, which runs if `cond(vars)` holds.

        Follow it with any number of `elif_` and an optional `else_`; the first true arm wins.
        `cond` is a pure function of the Variables, which it gets read-only.
        """
        _check_arm("if_", cond, label)
        self._claim_id("Gateway", id, generates=JOIN_SUFFIX)
        xor = XorNode(id)
        self._sequences[-1].nodes.append(xor)
        with self._arm(xor, label, cond):
            yield

    @contextmanager
    def elif_(self, cond: Condition, *, label: str) -> Generator[None]:
        """Add an arm to the XOR gateway just above, which runs if no earlier arm did and `cond` holds."""
        _check_arm("elif_", cond, label)
        with self._arm(self._continued_xor("elif_"), label, cond):
            yield

    @contextmanager
    def else_(self) -> Generator[None]:
        """Close the XOR gateway just above with an arm that runs when no other arm did."""
        with self._arm(self._continued_xor("else_"), ELSE_LABEL, None):
            yield

    def _continued_xor(self, keyword: str) -> XorNode:
        """The XOR gateway an `elif_` / `else_` adds to: the one declared right before it."""
        sequence = self._sequences[-1]
        xor = self._open_xor
        if xor is None or not sequence.nodes or sequence.nodes[-1] is not xor:
            raise ValueError(
                f"p.{keyword}() must come right after a p.if_() or p.elif_() block"
            )
        if xor.arms[-1].condition is None:
            raise ValueError(
                f"p.{keyword}() comes after the else_() of gateway {xor.id}; else_() is the last arm"
            )
        return xor

    @contextmanager
    def _arm(
        self, xor: XorNode, label: str, condition: Condition | None
    ) -> Generator[None]:
        arm = XorArm(label, condition)
        xor.arms.append(arm)
        try:
            with self._block(arm.body):
                yield
        finally:
            self._open_xor = xor

    @contextmanager
    def _block(self, body: SequenceNode) -> Generator[None]:
        self._sequences.append(body)
        self._open_xor = None
        try:
            yield
        finally:
            _ = self._sequences.pop()

    def _claim_id(self, kind: str, node_id: str, *, generates: str = "") -> None:
        """Event, gateway and loop ids share one namespace across the Process.

        A gateway or loop also reserves the id its closing BPMN gateway gets (the id plus `generates`).
        """
        if not isinstance(node_id, str) or not node_id.isidentifier():  # pyright: ignore[reportUnnecessaryIsInstance]  # runtime guard
            raise ValueError(f"{kind} id {node_id!r} must be a Python identifier")
        claims = {node_id: f"{kind} {node_id}"}
        if generates:
            claims[node_id + generates] = (
                f"the BPMN gateway {node_id}{generates} of {kind} {node_id}"
            )
        for claimed, owner in claims.items():
            if claimed in self._ids:
                used_by = self._ids[claimed]
                raise ValueError(
                    f"{owner} needs the id {claimed}, already used by {used_by}; ids must be unique"
                )
        self._ids.update(claims)

    def to_model(self, model_format: ModelFormat = "bpmn") -> str:
        """The Process model Reparo reads, as BPMN 2.0 or the compact outline."""
        return render(self._definition, model_format)


def definition_of(process: Process) -> ProcessDefinition:
    return process._definition  # pyright: ignore[reportPrivateUsage]
