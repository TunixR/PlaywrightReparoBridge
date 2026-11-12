"""`run()`: the engine that walks a Process against the author's BrowserContext."""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import cast

from playwright.async_api import BrowserContext

from reparo_playwright_bridge._checks import run_checks
from reparo_playwright_bridge._classify import FailureKind, classify
from reparo_playwright_bridge._context import EventContext, Variables, active_page
from reparo_playwright_bridge._definition import (
    Activity,
    EventNode,
    LoopNode,
    ProcessDefinition,
    SequenceNode,
    XorNode,
)
from reparo_playwright_bridge._log import recovery_log, run_log
from reparo_playwright_bridge._modality import Modality
from reparo_playwright_bridge._payload import (
    ErroredActivity,
    LoggedEvent,
    build_payload,
    pass_suffix,
)
from reparo_playwright_bridge._process import Process, definition_of
from reparo_playwright_bridge._protocol import Ended, recover
from reparo_playwright_bridge._report import report_result
from reparo_playwright_bridge._result import Failure, RunResult
from reparo_playwright_bridge._settings import ReparoSettings

CaseId = str | int
Passes = tuple[int, ...]
"""The pass of each enclosing loop, outermost first."""


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


class _Run:
    """One run of a Process: its case, its Variables and its log context."""

    def __init__(
        self,
        definition: ProcessDefinition,
        context: BrowserContext,
        settings: ReparoSettings,
        case_id: CaseId,
    ) -> None:
        self.definition: ProcessDefinition = definition
        self.context: BrowserContext = context
        self.settings: ReparoSettings = settings
        self.case_id: CaseId = case_id

        self._ui_log: list[LoggedEvent] = []
        """Every Logged event since run start, oldest first."""

        self._entered: tuple[Activity, Passes] | None = None
        """The Activity and pass of the last Event run, to log each Activity entered once per pass."""

    async def walk(self, variables: Variables) -> RunResult:
        failure = await self._sequence(self.definition.root, variables, ())
        if failure is not None:
            return failure
        return await run_checks(
            self.definition.checks, self.context, variables, self.case_id
        )

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
                pass_suffix(passes),
                extra=extra,
            )
        name = event.id + pass_suffix(passes)
        run_log.debug("Event %s started", name, extra=extra)
        started = time.monotonic()
        snapshot = copy.deepcopy(variables)
        try:
            page = await active_page(self.context)
            await event.fn(EventContext(page, variables))
        except Exception as exc:  # noqa: BLE001  # every raise is classified, never re-raised
            return await self._event_failed(event, name, passes, exc, extra, snapshot)
        self._ui_log.append(LoggedEvent.now(len(self._ui_log) + 1, event, passes))
        run_log.debug(
            "Event %s ended in %.0f ms",
            name,
            (time.monotonic() - started) * 1000,
            extra=extra,
        )
        return None

    async def _event_failed(
        self,
        event: EventNode,
        name: str,
        passes: Passes,
        exc: Exception,
        extra: dict[str, object],
        snapshot: Variables,
    ) -> RunResult:
        classified = classify(exc)
        failure = Failure(type="local", event_id=event.id, loop_iteration=list(passes))
        if classified.kind is not FailureKind.UI:
            run_log.error(
                "Event %s failed: %s (%s)",
                name,
                classified.kind.value,
                classified.summary,
                exc_info=exc,
                extra=extra,
            )
            return RunResult(outcome="local", reason=classified.reason, failure=failure)
        failure.error_code = classified.error_code
        failure.error_description = classified.summary
        run_log.debug("Event %s failed: UI failure", name, exc_info=exc, extra=extra)
        # An Event can't opt back into Recovery inside an opted-out Activity.
        if not event.activity.recoverable:
            why = f"Activity {event.activity.name!r} is not recoverable"
        elif not event.recoverable:
            why = f"Event {event.id} is not recoverable"
        else:
            return await self._recover(event, name, passes, failure, extra, snapshot)
        run_log.warning(
            "Event %s failed: UI failure (%s: %s); not handed to Reparo: %s",
            name,
            classified.error_code,
            classified.summary,
            why,
            extra=extra,
        )
        return RunResult(
            outcome="local",
            reason=f"{classified.reason} (not handed to Reparo: {why})",
            failure=failure,
        )

    async def _recover(
        self,
        event: EventNode,
        name: str,
        passes: Passes,
        failure: Failure,
        extra: dict[str, object],
        snapshot: Variables,
    ) -> RunResult:
        """Hand the UI failure to Reparo as the run's one Recovery."""
        code, description = failure.error_code or "", failure.error_description or ""
        recovery_log.info(
            "Recovery started: Event %s failed (%s: %s)",
            name,
            code,
            description,
            extra=extra,
        )
        errored = ErroredActivity(
            len(self._ui_log) + 1, event, passes, code, description
        )
        payload = build_payload(
            self.definition,
            self.settings,
            self.case_id,
            self._ui_log,
            errored,
            snapshot,
        )
        answer = await recover(self.settings, payload, extra)
        if isinstance(answer, Ended):
            failure.type = answer.failure_type
            return RunResult(
                outcome=answer.outcome, reason=answer.reason, failure=failure
            )
        recovery_log.info(
            "done received: Recovery %s, success %s",
            answer.recovery_id,
            str(answer.content.success).lower(),
            extra=extra,
        )
        if not answer.content.success:
            failure.type = "recovery_declined"
            return RunResult(
                outcome="recovery_declined",
                reason="Reparo declined the Recovery",
                failure=failure,
                recovery_id=answer.recovery_id,
            )
        # Resuming at Reparo's Continuation comes with the Continuation resolver.
        failure.type = "continuation_rejected"
        return RunResult(
            outcome="continuation_rejected",
            reason="resuming after a Recovery is not supported yet",
            failure=failure,
            recovery_id=answer.recovery_id,
        )


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
    result = await _Run(definition, context, reparo, resolved).walk(
        copy.deepcopy(dict(variables))
    )
    if result.recovery_id is not None:
        await report_result(
            reparo, result.recovery_id, result.outcome == "completed", extra
        )
    return result
