"""`run()`: the engine that walks a Process against the author's BrowserContext."""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import cast

from playwright.async_api import BrowserContext, Page

from reparo_playwright_bridge._classify import FailureKind, classify
from reparo_playwright_bridge._context import EventContext, Variables
from reparo_playwright_bridge._definition import (
    Activity,
    EventNode,
    LoopNode,
    ProcessDefinition,
    SequenceNode,
    XorNode,
)
from reparo_playwright_bridge._log import run_log
from reparo_playwright_bridge._modality import Modality
from reparo_playwright_bridge._process import Process, definition_of
from reparo_playwright_bridge._result import Failure, RunResult
from reparo_playwright_bridge._settings import ReparoSettings

CaseId = str | int
Passes = tuple[int, ...]
"""The pass of each enclosing loop, outermost first."""


def _suffix(passes: Passes) -> str:
    """The pass suffix the UI log puts on a name: ` #1#2`, or nothing outside loops."""
    return " " + "".join(f"#{i}" for i in passes) if passes else ""


class _LocalFailure(Exception):
    """Ends the run `local` before any Event, with `reason`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason: str = reason


def _resolve_case_id(case_id: CaseId | Callable[[], CaseId]) -> CaseId:
    try:
        value: object = case_id() if callable(case_id) else case_id
    except Exception as exc:
        raise _LocalFailure(
            f"case_id callable raised {type(exc).__name__}: {exc}"
        ) from exc
    if isinstance(value, bool) or not isinstance(value, str | int):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise _LocalFailure(
            f"case_id must be a str or an int, got {type(value).__name__}"
        )
    return value


def _check_inputs(
    definition: ProcessDefinition, variables: Mapping[str, object]
) -> None:
    problems: list[str] = []
    for name, expected in definition.inputs.items():
        if name not in variables:
            problems.append(f"{name} (missing, expected {expected.__name__})")
        elif not isinstance(variables[name], expected):
            problems.append(
                f"{name} (expected {expected.__name__}, got {type(variables[name]).__name__})"
            )
    if problems:
        raise _LocalFailure(
            "Missing or mistyped input Variables: " + ", ".join(problems)
        )


async def _active_page(context: BrowserContext) -> Page:
    """The most recently opened page that is still open; a new page only if there is none."""
    pages = [page for page in context.pages if not page.is_closed()]
    return pages[-1] if pages else await context.new_page()


class _Run:
    """One run of a Process: its case, its Variables and its log context."""

    def __init__(
        self, definition: ProcessDefinition, context: BrowserContext, case_id: CaseId
    ) -> None:
        self.definition: ProcessDefinition = definition
        self.context: BrowserContext = context
        self.case_id: CaseId = case_id

        self._entered: tuple[Activity, Passes] | None = None
        """The Activity and pass of the last Event run, to log each Activity entered once per pass."""

    async def walk(self, variables: Variables) -> RunResult:
        failure = await self._sequence(self.definition.root, variables, ())
        return failure or RunResult(outcome="completed")

    async def _sequence(
        self, sequence: SequenceNode, variables: Variables, passes: Passes
    ) -> RunResult | None:
        for node in sequence.nodes:
            match node:
                case EventNode():
                    failure = await self._event(node, variables, passes)
                case LoopNode():
                    failure = await self._loop(node, variables, passes)
                case XorNode():
                    failure = await self._xor(node, variables, passes)
            if failure is not None:
                return failure
        return None

    async def _xor(
        self, xor: XorNode, variables: Variables, passes: Passes
    ) -> RunResult | None:
        # Conditions get the Variables read-only: they are pure functions of them.
        view = MappingProxyType(variables)
        for arm in xor.arms:
            if arm.condition is not None:
                try:
                    holds = bool(arm.condition(view))
                except Exception as exc:  # noqa: BLE001  # a Process bug, never re-raised
                    return self._control_flow_bug(
                        f"condition {arm.label!r} of gateway {xor.id}", exc, passes
                    )
                if not holds:
                    continue
            return await self._sequence(arm.body, variables, passes)
        return None

    async def _loop(
        self, loop: LoopNode, variables: Variables, passes: Passes
    ) -> RunResult | None:
        # The list is re-read on every pass: pass i always binds the current vars[over][i].
        index = 0
        while True:
            try:
                items = cast("object", variables[loop.over])
                if not isinstance(items, list):
                    raise TypeError(
                        f"Variable {loop.over} is a {type(items).__name__}, not a list"
                    )
                items = cast("list[object]", items)
                if index >= len(items):
                    return None
                variables[loop.as_] = items[index]
            except Exception as exc:  # noqa: BLE001  # a Process bug, never re-raised
                return self._control_flow_bug(
                    f"loop {loop.id} could not bind {loop.as_} from {loop.over}",
                    exc,
                    passes,
                )
            failure = await self._sequence(loop.body, variables, (*passes, index))
            if failure is not None:
                return failure
            index += 1

    def _control_flow_bug(
        self, where: str, exc: Exception, passes: Passes
    ) -> RunResult:
        """Anything raised in a condition or a loop binding is a Process bug."""
        summary = classify(exc).summary
        run_log.error(
            "Process bug in %s (%s)",
            where,
            summary,
            exc_info=exc,
            extra={"case_id": self.case_id, "loop_iteration": list(passes)},
        )
        return RunResult(
            outcome="local",
            reason=f"{FailureKind.PROCESS_BUG.value}: {where}: {summary}",
            failure=Failure(type="local"),
        )

    async def _event(
        self, event: EventNode, variables: Variables, passes: Passes
    ) -> RunResult | None:
        extra: dict[str, object] = {
            "case_id": self.case_id,
            "event_id": event.id,
            "loop_iteration": list(passes),
            "activity_id": event.activity.id,
        }
        if self._entered != (event.activity, passes):
            self._entered = (event.activity, passes)
            run_log.info(
                "Activity %s entered%s",
                event.activity.name,
                _suffix(passes),
                extra=extra,
            )
        name = event.id + _suffix(passes)
        run_log.debug("Event %s started", name, extra=extra)
        started = time.monotonic()
        try:
            page = await _active_page(self.context)
            await event.fn(EventContext(page, variables))
        except Exception as exc:  # noqa: BLE001  # every raise is classified, never re-raised
            return self._event_failed(event, name, passes, exc, extra)
        run_log.debug(
            "Event %s ended in %.0f ms",
            name,
            (time.monotonic() - started) * 1000,
            extra=extra,
        )
        return None

    def _event_failed(
        self,
        event: EventNode,
        name: str,
        passes: Passes,
        exc: Exception,
        extra: dict[str, object],
    ) -> RunResult:
        classified = classify(exc)
        failure = Failure(type="local", event_id=event.id, loop_iteration=list(passes))
        if classified.kind is FailureKind.UI:
            run_log.debug(
                "Event %s failed: UI failure", name, exc_info=exc, extra=extra
            )
            run_log.warning(
                "Event %s failed: UI failure (%s: %s); not handed to Reparo",
                name,
                classified.error_code,
                classified.summary,
                extra=extra,
            )
            failure.error_code = classified.error_code
            failure.error_description = classified.summary
        else:
            run_log.error(
                "Event %s failed: %s (%s)",
                name,
                classified.kind.value,
                classified.summary,
                exc_info=exc,
                extra=extra,
            )
        return RunResult(outcome="local", reason=classified.reason, failure=failure)


async def run(
    process: Process,
    *,
    context: BrowserContext,
    reparo: ReparoSettings,
    case_id: CaseId | Callable[[], CaseId],
    variables: Mapping[str, object] | None = None,
    modalities: Sequence[Modality] = (),
) -> RunResult:
    """Run `process` on the author's `context` and return how it ended.

    Never raises for a failed run, and never closes the context or any of its pages.
    """
    extra: dict[str, object] = {}
    try:
        result = await _run(
            process, context, reparo, case_id, variables or {}, modalities, extra
        )
    # An engine internal is a Process bug too; run() never raises for a failed run.
    except Exception as exc:  # noqa: BLE001
        summary = classify(exc).summary
        run_log.error(
            "Run failed: Process bug (%s)", summary, exc_info=exc, extra=extra
        )
        result = RunResult(
            outcome="local",
            reason=f"Process bug: {summary}",
            failure=Failure(type="local"),
        )
    level = logging.INFO if result.outcome == "completed" else logging.ERROR
    run_log.log(level, "%s", result, extra=extra)
    return result


async def _run(
    process: Process,
    context: BrowserContext,
    reparo: ReparoSettings,
    case_id: CaseId | Callable[[], CaseId],
    variables: Mapping[str, object],
    modalities: Sequence[Modality],
    extra: dict[str, object],
) -> RunResult:
    definition = definition_of(process)
    try:
        resolved = _resolve_case_id(case_id)
        extra["case_id"] = resolved
        run_log.info(
            "Run started: process %s, case %s, modalities [%s], model format %s",
            definition.name,
            resolved,
            ", ".join(m.name for m in modalities),
            reparo.model_format,
            extra=extra,
        )
        _check_inputs(definition, variables)
    except _LocalFailure as failure:
        return RunResult(
            outcome="local", reason=failure.reason, failure=Failure(type="local")
        )
    return await _Run(definition, context, resolved).walk(
        copy.deepcopy(dict(variables))
    )
