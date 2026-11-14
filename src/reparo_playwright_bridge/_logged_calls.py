# pyright: reportAny=false, reportExplicitAny=false
# (A call's bound arguments are whatever the author passed to Playwright.)
"""Which Playwright calls the UI log records, and the `action_type` and `input` each one logs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any, cast


@dataclass(frozen=True)
class Attempt:
    """A logged call as it starts: what the Errored activity reports if the Event then fails."""

    action_type: str
    input: str | None


NO_ATTEMPT = Attempt("check", None)
"""The Errored activity's attempt when the Event attempted no logged call."""
READ = Attempt("read", None)


@dataclass(frozen=True)
class LoggedCall:
    """The UI log fields of one logged call, as Playwright saw them (unmasked)."""

    action_type: str
    input: str | None
    target: str | None
    group: str | None
    previous_state: str
    current_state: str
    timestamp: str


def timestamp() -> str:
    """Now, as the UI log stamps a call: ISO-8601 UTC with its offset."""
    return datetime.now(UTC).isoformat()


def _file_name(file: object) -> str:
    if isinstance(file, Mapping):
        return str(cast("Mapping[str, object]", file).get("name", ""))
    return PurePath(str(file)).name


def _files(files: object) -> str:
    items = cast("list[object]", files) if isinstance(files, list | tuple) else [files]
    return ", ".join(_file_name(f) for f in items)


def _options(args: Mapping[str, Any]) -> str | None:
    for key in ("label", "value", "index"):
        value = args.get(key)
        if value is not None:
            items = (
                cast("list[object]", value)
                if isinstance(value, list | tuple)
                else [value]
            )
            return ", ".join(str(item) for item in items)
    return None


Inputs = Callable[[Mapping[str, Any]], Attempt]
"""From a Locator call's bound arguments: the attempt it logs."""

ACTIONS: dict[str, Inputs] = {
    "click": lambda _: Attempt("click", None),
    "dblclick": lambda _: Attempt("double_click", None),
    "fill": lambda a: Attempt("type", str(a["value"])),
    "type": lambda a: Attempt("type", str(a["text"])),
    "press_sequentially": lambda a: Attempt("type", str(a["text"])),
    "press": lambda a: Attempt("press", str(a["key"])),
    "select_option": lambda a: Attempt("select", _options(a)),
    "check": lambda _: Attempt("check", "true"),
    "uncheck": lambda _: Attempt("uncheck", "false"),
    "set_checked": lambda a: (
        Attempt("check", "true") if a["checked"] else Attempt("uncheck", "false")
    ),
    "hover": lambda _: Attempt("hover", None),
    "drag_to": lambda _: Attempt("drag", None),
    "set_input_files": lambda a: Attempt("upload", _files(a["files"])),
}
"""Locator actions whose target is the element itself."""

SCROLL_INTO_VIEW = "scroll_into_view_if_needed"
"""The Locator action whose target is the element's scrollable ancestor."""

READS = (
    "inner_text",
    "text_content",
    "input_value",
    "get_attribute",
    "all_inner_texts",
    "all_text_contents",
)

PAGE_SHORTCUTS = (
    *(name for name in ACTIONS if name not in ("press_sequentially", "drag_to")),
    *(name for name in READS if not name.startswith("all_")),
)
"""Page methods that take a selector first and do what the Locator method of the same name does."""
