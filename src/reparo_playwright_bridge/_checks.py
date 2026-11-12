"""The Completion checks runner: runs the declared checks once the Process reaches its end."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from types import MappingProxyType
from typing import cast

from playwright.async_api import BrowserContext

from reparo_playwright_bridge._classify import FailureKind, classify
from reparo_playwright_bridge._context import CheckContext, Variables, active_page
from reparo_playwright_bridge._definition import CompletionCheck
from reparo_playwright_bridge._log import checks_log
from reparo_playwright_bridge._result import Failure, RunResult


class _BadReturn(Exception):
    """A check returned something other than `(True, None)` or `(False, reason)`."""


def _unmet_reason(returned: object) -> str | None:
    """None if `returned` means met, the reason if it means unmet; `_BadReturn` otherwise."""
    # A bad return names only its shape: its value may hold Variable values, which are never logged.
    if not isinstance(returned, tuple):
        raise _BadReturn(f"returned a {type(returned).__name__}, {_EXPECTED}")
    items = cast("tuple[object, ...]", returned)
    if len(items) == 2:
        met, reason = items
        if met is True and reason is None:
            return None
        if met is False and isinstance(reason, str) and reason.strip():
            return reason
    raise _BadReturn(f"returned {_shape(items)}, {_EXPECTED}")


_EXPECTED = "expected (True, None) or (False, <non-empty str reason>)"


def _shape(items: tuple[object, ...]) -> str:
    return "(" + ", ".join(type(item).__name__ for item in items) + ")"


async def run_checks(
    checks: Sequence[CompletionCheck],
    context: BrowserContext,
    variables: Variables,
    case_id: str | int,
) -> RunResult:
    """Run every check in declaration order: `completed` if all are met, else `checks_failed`.

    A check that raises or returns anything else crashes: it stops the remaining checks and ends
    the run `local`, as a Process bug unless it hit an Environment failure.
    """
    extra: dict[str, object] = {"case_id": case_id}
    unmet: list[str] = []
    for check in checks:
        try:
            page = await active_page(context)
            # Checks verify the final state and never change it: each gets its own read-only copy.
            view = MappingProxyType(copy.deepcopy(variables))
            reason = _unmet_reason(await check.fn(CheckContext(page, view)))
        except Exception as exc:  # noqa: BLE001  # a crashing check, never re-raised
            return _crashed(check, exc, extra)
        if reason is None:
            checks_log.info("Completion check %s met", check.id, extra=extra)
        else:
            checks_log.warning(
                "Completion check %s not met: %s", check.id, reason, extra=extra
            )
            unmet.append(f"{check.id}: {reason}")
    if not unmet:
        return RunResult(outcome="completed")
    count = len(unmet)
    plural = "" if count == 1 else "s"
    return RunResult(
        outcome="checks_failed",
        reason=f"{count} completion check{plural} not met: " + "; ".join(unmet),
    )


def _crashed(
    check: CompletionCheck, exc: Exception, extra: dict[str, object]
) -> RunResult:
    if isinstance(exc, _BadReturn):
        kind, summary = FailureKind.PROCESS_BUG, str(exc)
    else:
        classified = classify(exc)
        # Only an Environment failure keeps its class; a UI failure in a check is the check's own bug.
        kind = (
            classified.kind
            if classified.kind is FailureKind.ENVIRONMENT
            else FailureKind.PROCESS_BUG
        )
        summary = classified.summary
    checks_log.error(
        "Completion check %s crashed: %s (%s)",
        check.id,
        kind.value,
        summary,
        exc_info=None if isinstance(exc, _BadReturn) else exc,
        extra=extra,
    )
    return RunResult(
        outcome="local",
        reason=f"{kind.value}: completion check {check.id} crashed: {summary}",
        failure=Failure(type="local"),
    )
