"""Failure classification: what an Event body raised is a UI failure, an Environment failure or a Process bug."""

from __future__ import annotations

import enum
import traceback
from dataclasses import dataclass
from pathlib import PurePath

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from reparo_playwright_bridge._context import EXPECTATION_NOT_MET, UIFailure


class FailureKind(enum.Enum):
    UI = "UI failure"
    ENVIRONMENT = "Environment failure"
    PROCESS_BUG = "Process bug"


# Error codes for UI failures. The full catalogue is left to the Recovery tickets.
ELEMENT_TIMEOUT = "UiElementTimeout"
ELEMENT_AMBIGUOUS = "UiElementAmbiguous"
UI_ACTION_FAILED = "UiActionFailed"

_ENVIRONMENT_MARKERS = (
    "net::ERR_",
    "Target page, context or browser has been closed",
    "Target closed",
    "Browser has been closed",
    "Browser closed",
    "browser has disconnected",
    "crashed",
    "Protocol error",
)
# Playwright prefixes each error with the API call that raised it.
_ENVIRONMENT_PREFIXES = ("APIRequestContext.", "APIResponse.")
_COMPONENT_PREFIXES = ("Locator.", "ElementHandle.", "FrameLocator.")


@dataclass(frozen=True)
class Classified:
    kind: FailureKind
    summary: str
    """One line saying what went wrong; never a traceback or a Playwright call log."""
    error_code: str | None = None
    """For UI failures: the class code Reparo would receive."""

    @property
    def reason(self) -> str:
        return f"{self.kind.value}: {self.summary}"


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0].strip() if lines else ""


def _raised_by_playwright_expect(exc: BaseException) -> bool:
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return False
    path = PurePath(frames[-1].filename)
    return path.name == "_assertions.py" and "playwright" in path.parts


def classify(exc: BaseException) -> Classified:
    if isinstance(exc, UIFailure):
        return Classified(FailureKind.UI, exc.description, exc.code)
    if isinstance(exc, PlaywrightError):
        # Only the first line is the error; the rest is Playwright's call log, which
        # repeats selectors and so can contain any text. Shapes whose first line quotes
        # the selector (strict mode) are matched before the Environment markers.
        message = _first_line(exc.message)
        if isinstance(exc, PlaywrightTimeoutError):
            return Classified(FailureKind.UI, message, ELEMENT_TIMEOUT)
        if "strict mode violation" in message:
            return Classified(FailureKind.UI, message, ELEMENT_AMBIGUOUS)
        if any(marker in message for marker in _ENVIRONMENT_MARKERS):
            return Classified(FailureKind.ENVIRONMENT, message)
        if message.startswith(_ENVIRONMENT_PREFIXES):
            return Classified(FailureKind.ENVIRONMENT, message)
        if message.startswith(_COMPONENT_PREFIXES):
            return Classified(FailureKind.UI, message, UI_ACTION_FAILED)
    if isinstance(exc, AssertionError) and _raised_by_playwright_expect(exc):
        return Classified(
            FailureKind.UI,
            _first_line(str(exc)) or "expectation not met",
            EXPECTATION_NOT_MET,
        )
    detail = _first_line(str(exc))
    name = type(exc).__name__
    return Classified(FailureKind.PROCESS_BUG, f"{name}: {detail}" if detail else name)
