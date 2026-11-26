"""Failure classification: what an Event body raised is a UI failure, an Infrastructure failure or a Process bug."""

from __future__ import annotations

import enum
import re
import traceback
from dataclasses import dataclass
from pathlib import PurePath

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from reparo_playwright_bridge._context import EXPECTATION_NOT_MET, UIFailure


class ProcessBug(Exception):
    """A rule of the Process broken at run time, detected by the bridge; its message says which."""


class FailureKind(enum.Enum):
    UI = "UI failure"
    INFRASTRUCTURE = "Infrastructure failure"
    PROCESS_BUG = "Process bug"


# Error codes for UI failures. The full catalogue is left to the Recovery tickets.
ELEMENT_TIMEOUT = "UiElementTimeout"
ELEMENT_AMBIGUOUS = "UiElementAmbiguous"
UI_ACTION_FAILED = "UiActionFailed"

_INFRASTRUCTURE_MARKERS = (
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
_INFRASTRUCTURE_PREFIXES = ("APIRequestContext.", "APIResponse.")
_COMPONENT_PREFIXES = ("Locator.", "ElementHandle.", "FrameLocator.")


_API_LINE = re.compile(r"^(?P<api>\w+)\.(?P<method>\w+): (?:Error: )?(?P<rest>.*)$")
_TIMEOUT = re.compile(r"Timeout (?P<ms>\d+)ms exceeded")
_RESOLVED_TO = re.compile(r"resolved to (?P<count>\d+) elements")
# Playwright names the call in its JS spelling; the Python spelling is accepted too.
_VERBS = {
    "click": "click",
    "dblclick": "double-click",
    "fill": "fill",
    "type": "type into",
    "pressSequentially": "type into",
    "press_sequentially": "type into",
    "press": "press a key on",
    "selectOption": "select an option in",
    "select_option": "select an option in",
    "check": "check",
    "uncheck": "uncheck",
    "setChecked": "check or uncheck",
    "set_checked": "check or uncheck",
    "hover": "hover over",
    "dragTo": "drag",
    "drag_to": "drag",
    "setInputFiles": "upload files to",
    "set_input_files": "upload files to",
    "scrollIntoViewIfNeeded": "scroll to",
    "scroll_into_view_if_needed": "scroll to",
    "waitFor": "wait for",
    "wait_for": "wait for",
    "innerText": "read",
    "inner_text": "read",
    "textContent": "read",
    "text_content": "read",
    "inputValue": "read",
    "input_value": "read",
    "getAttribute": "read",
    "get_attribute": "read",
}


def _sentence(text: str) -> str:
    """`text` as one sentence: a capital first letter and a full stop."""
    text = text.strip().rstrip(":").strip()
    if not text:
        return "The page was not in the expected state."
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else text + "."


def _describe_component_error(message: str) -> str:
    """One semantic sentence for a Playwright error about finding or acting on a component."""
    line = _API_LINE.match(message)
    if line is None:
        return _sentence(message)
    method = line["method"]
    verb = _VERBS.get(method, method)
    rest = line["rest"]
    if timeout := _TIMEOUT.search(rest):
        return (
            f"Timed out after {timeout['ms']} ms trying to {verb} the target element."
        )
    if "strict mode violation" in rest:
        count = _RESOLVED_TO.search(rest)
        matched = f"{count['count']} elements" if count else "several elements"
        return f"The target element to {verb} matched {matched} instead of one."
    detail = rest.strip().rstrip(".:")
    if not detail:
        return f"Could not {verb} the target element."
    return f"Could not {verb} the target element: {detail[0].lower()}{detail[1:]}."


def _describe_failed_expect(message: str) -> str:
    return _sentence(
        re.sub(r"^Locator expected", "The target element was expected", message)
    )


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


def _classify_playwright_error(exc: PlaywrightError) -> Classified | None:
    """The class of a Playwright error, or None when it has none of the known shapes."""
    # Only the first line is the error; the rest is Playwright's call log, which
    # repeats selectors and so can contain any text. Shapes whose first line quotes
    # the selector (strict mode) are matched before the Infrastructure markers.
    message = _first_line(exc.message)
    if isinstance(exc, PlaywrightTimeoutError):
        return Classified(
            FailureKind.UI, _describe_component_error(message), ELEMENT_TIMEOUT
        )
    if "strict mode violation" in message:
        return Classified(
            FailureKind.UI, _describe_component_error(message), ELEMENT_AMBIGUOUS
        )
    if any(marker in message for marker in _INFRASTRUCTURE_MARKERS):
        return Classified(FailureKind.INFRASTRUCTURE, message)
    if message.startswith(_INFRASTRUCTURE_PREFIXES):
        return Classified(FailureKind.INFRASTRUCTURE, message)
    if message.startswith(_COMPONENT_PREFIXES):
        return Classified(
            FailureKind.UI, _describe_component_error(message), UI_ACTION_FAILED
        )
    return None


def classify(exc: BaseException) -> Classified:
    if isinstance(exc, UIFailure):
        return Classified(FailureKind.UI, exc.description, exc.code)
    if isinstance(exc, ProcessBug):
        return Classified(FailureKind.PROCESS_BUG, str(exc))
    if isinstance(exc, PlaywrightError) and (
        classified := _classify_playwright_error(exc)
    ):
        return classified
    if isinstance(exc, AssertionError) and _raised_by_playwright_expect(exc):
        return Classified(
            FailureKind.UI,
            _describe_failed_expect(_first_line(str(exc))),
            EXPECTATION_NOT_MET,
        )
    detail = _first_line(str(exc))
    name = type(exc).__name__
    return Classified(FailureKind.PROCESS_BUG, f"{name}: {detail}" if detail else name)


def classify_outside_event(exc: BaseException) -> Classified:
    """A raise from author code that isn't an Event body (a Completion check, `on_continuation`).

    Only an Infrastructure failure keeps its class: there is no Event for a UI failure to belong to,
    so anything else is a Process bug.
    """
    classified = classify(exc)
    if classified.kind in (FailureKind.INFRASTRUCTURE, FailureKind.PROCESS_BUG):
        return classified
    return Classified(FailureKind.PROCESS_BUG, classified.summary)
