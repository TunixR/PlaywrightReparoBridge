"""`run()`: the engine that walks a Process against the author's BrowserContext."""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import Callable, Mapping, Sequence

from playwright.async_api import BrowserContext, Page

from reparo_playwright_bridge._classify import FailureKind, classify
from reparo_playwright_bridge._context import EventContext, Variables
from reparo_playwright_bridge._definition import Activity, EventNode, ProcessDefinition
from reparo_playwright_bridge._log import run_log
from reparo_playwright_bridge._modality import Modality
from reparo_playwright_bridge._process import Process, definition_of
from reparo_playwright_bridge._result import Failure, RunResult
from reparo_playwright_bridge._settings import ReparoSettings

CaseId = str | int


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

    def _extra(self, event: EventNode) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "event_id": event.id,
            "loop_iteration": [],
            "activity_id": event.activity.id,
        }

    async def walk(self, variables: Variables) -> RunResult:
        activity: Activity | None = None
        for event in self.definition.root.nodes:
            extra = self._extra(event)
            if event.activity is not activity:
                activity = event.activity
                run_log.info("Activity %s entered", activity.name, extra=extra)
            failure = await self._run_event(event, variables, extra)
            if failure is not None:
                return failure
        return RunResult(outcome="completed")

    async def _run_event(
        self, event: EventNode, variables: Variables, extra: dict[str, object]
    ) -> RunResult | None:
        run_log.debug("Event %s started", event.id, extra=extra)
        started = time.monotonic()
        try:
            page = await _active_page(self.context)
            await event.fn(EventContext(page, variables))
        except Exception as exc:  # noqa: BLE001  # every raise is classified, never re-raised
            return self._event_failed(event, exc, extra)
        run_log.debug(
            "Event %s ended in %.0f ms",
            event.id,
            (time.monotonic() - started) * 1000,
            extra=extra,
        )
        return None

    def _event_failed(
        self, event: EventNode, exc: Exception, extra: dict[str, object]
    ) -> RunResult:
        classified = classify(exc)
        failure = Failure(type="local", event_id=event.id, loop_iteration=[])
        if classified.kind is FailureKind.UI:
            run_log.debug(
                "Event %s failed: UI failure", event.id, exc_info=exc, extra=extra
            )
            run_log.warning(
                "Event %s failed: UI failure (%s: %s); not handed to Reparo",
                event.id,
                classified.error_code,
                classified.summary,
                extra=extra,
            )
            failure.error_code = classified.error_code
            failure.error_description = classified.summary
        else:
            run_log.error(
                "Event %s failed: %s (%s)",
                event.id,
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
