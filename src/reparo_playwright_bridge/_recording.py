# pyright: reportAny=false, reportExplicitAny=false
# (The proxies forward any Playwright arguments; their declared types stay Playwright's own.)
"""The recording page behind `ctx.page`: it records the one logged call of each Event.

`RecordingPage`, `RecordingLocator`, `RecordingFrameLocator` and `RecordingMouse` subclass
Playwright's own classes over the same objects, so author code (and `expect`) sees a normal Page.
Each logged call goes through the Event's `Recorder`, which enforces one action per Event and
captures the UI log fields; every other call is Playwright's, untouched.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any, cast, override
from urllib.parse import urlsplit

from playwright.async_api import FrameLocator, Locator, Mouse, Page

from reparo_playwright_bridge._classify import ProcessBug

STATE_LIMIT = 200
READ_TIMEOUT_MS = 500
PREVIOUS_UNAVAILABLE = "[Previous state unavailable]"
CURRENT_UNAVAILABLE = "[Current state unavailable]"
NO_LONGER_ON_SCREEN = "[No longer on screen]"

_SNAPSHOT_JS = """
(el) => {
  const text = (n) => (n.innerText ?? n.textContent ?? "").replace(/\\s+/g, " ").trim();
  const name = (n) => {
    const ids = n.getAttribute("aria-labelledby");
    if (ids) {
      const t = ids.split(/\\s+/).map((id) => document.getElementById(id))
        .filter(Boolean).map(text).join(" ").trim();
      if (t) return t;
    }
    const label = (n.getAttribute("aria-label") || "").trim();
    if (label) return label;
    if (n.labels && n.labels.length) {
      const t = [...n.labels].map(text).join(" ").trim();
      if (t) return t;
    }
    if (n.tagName === "FIELDSET") {
      const legend = n.querySelector(":scope > legend");
      if (legend && text(legend)) return text(legend);
    }
    for (const attr of ["alt", "title", "placeholder"]) {
      const v = (n.getAttribute(attr) || "").trim();
      if (v) return v;
    }
    return "";
  };
  const IMPLICIT = {DIALOG: "dialog", FORM: "form", SECTION: "region", TR: "row",
                    LI: "listitem", FIELDSET: "group", DETAILS: "group"};
  const CONTAINERS = ["dialog", "menu", "form", "region", "row", "listitem", "group", "toolbar"];
  let group = "root";
  for (let n = el.parentElement; n; n = n.parentElement) {
    const role = n.getAttribute("role") || IMPLICIT[n.tagName];
    if (CONTAINERS.includes(role) && name(n)) { group = name(n); break; }
  }
  const scrollable = (n) => {
    const style = getComputedStyle(n);
    return /(auto|scroll)/.test(style.overflowY + style.overflowX)
      && (n.scrollHeight > n.clientHeight || n.scrollWidth > n.clientWidth);
  };
  let scroller = null;
  for (let n = el.parentElement; n && n !== document.body && n !== document.documentElement;
       n = n.parentElement) {
    if (scrollable(n)) { scroller = n; break; }
  }
  const toggle = el.tagName === "INPUT" && ["checkbox", "radio"].includes(el.type);
  const field = ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
  const button = el.tagName === "INPUT" && ["button", "submit", "reset"].includes(el.type);
  return {
    target: name(el) || (button ? el.value : field ? "" : text(el)),
    group,
    state: toggle ? String(el.checked) : field ? el.value : text(el),
    scroller: scroller ? (name(scroller) || text(scroller).slice(0, 80) || scroller.tagName.toLowerCase()) : null,
    scroll: scroller ? [scroller.scrollLeft, scroller.scrollTop] : [window.scrollX, window.scrollY],
    selected: el.tagName === "SELECT" ? [...el.selectedOptions].map((o) => o.label).join(", ") : null,
  };
}
"""
_PAGE_SCROLL_JS = "() => [window.scrollX, window.scrollY]"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _cut(state: str) -> str:
    return state[:STATE_LIMIT]


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


@dataclass(frozen=True)
class _Snapshot:
    target: str
    group: str
    state: str
    scroller: str | None
    scroll: tuple[int, int]
    selected: str | None


class Recorder:
    """What one Event did through `ctx.page`: its action, its last read, and what it attempted."""

    def __init__(self, event: str, application: str | None) -> None:
        self.event: str = event
        """The Event as log lines name it, for the Process bugs it raises."""
        self.application: str | None = application
        """The page title at Event start, else the URL host."""
        self._action: LoggedCall | None = None
        self._read: LoggedCall | None = None
        self._attempted_action: tuple[str, str | None] | None = None
        self._attempted_read: bool = False
        self._broken: ProcessBug | None = None

    @classmethod
    async def start(cls, event: str, page: Page) -> Recorder:
        return cls(event, await _application(page))

    @property
    def attempted(self) -> tuple[str, str | None] | None:
        """The `action_type` and `input` of the call the Event attempted, or None for none.

        The Event's action wins over its reads, recorded when it started.
        """
        if self._attempted_action is not None:
            return self._attempted_action
        return ("read", None) if self._attempted_read else None

    def begin_action(self, method: str, action_type: str, input: str | None) -> None:
        """Called before an action runs: a second action is a Process bug, raised instead."""
        if self._attempted_action is not None:
            self._broken = ProcessBug(
                f"Event {self.event} performed a second action ({method}) after its"
                + f" {self._attempted_action[0]}; an Event performs exactly one action"
            )
            raise self._broken
        self._attempted_action = (action_type, input)

    def begin_read(self) -> None:
        self._attempted_read = True

    def action(self, call: LoggedCall) -> None:
        self._action = call

    def read(self, call: LoggedCall) -> None:
        self._read = call

    def finish(self) -> LoggedCall:
        """The Event's one entry: its action, else its last read. No logged call is a Process bug."""
        if self._broken is not None:
            raise self._broken
        entry = self._action or self._read
        if entry is None:
            raise ProcessBug(
                f"Event {self.event} performed no logged call; an Event performs one action"
                + " or at least one read"
            )
        return entry


async def _within_timeout[T](awaitable: Awaitable[T]) -> T | None:
    """`awaitable`'s value, or None if it fails or takes longer than the state-read timeout."""
    try:
        return await asyncio.wait_for(awaitable, READ_TIMEOUT_MS / 1000)
    except Exception:  # noqa: BLE001  # a state read never fails the Event
        return None


async def _application(page: Page) -> str | None:
    title = await _within_timeout(page.title())
    if title and title.strip():
        return title.strip()
    return urlsplit(page.url).hostname or None


async def _snapshot(locator: Locator) -> _Snapshot | None:
    try:
        raw = await locator.evaluate(_SNAPSHOT_JS, timeout=READ_TIMEOUT_MS)
    except Exception:  # noqa: BLE001  # a state read never fails the Event
        return None
    values = cast("dict[str, Any]", raw)
    scroll = cast("list[int]", values["scroll"])
    return _Snapshot(
        target=str(values["target"]),
        group=str(values["group"]),
        state=str(values["state"]),
        scroller=values["scroller"],
        scroll=(int(scroll[0]), int(scroll[1])),
        selected=values["selected"],
    )


async def _gone(locator: Locator) -> bool:
    """Whether the element is confirmed gone; False when that can't be told in time."""
    return await _within_timeout(locator.count()) == 0


def _selector(locator: Locator) -> str:
    impl = cast("object", locator._impl_obj)  # pyright: ignore[reportPrivateUsage]
    selector = getattr(impl, "_selector", None)
    return selector if isinstance(selector, str) else repr(locator)


def _read_state(value: object) -> str:
    """What a read returned, as a state: a list joined, a missing text empty."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in cast("list[object]", value))
    return "" if value is None else str(value)


def _scroll_text(scroll: tuple[int, int]) -> str:
    return f"{scroll[0]},{scroll[1]}"


# ------------------------------------------------------------------------------- what each call logs


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


Inputs = Callable[[Mapping[str, Any]], tuple[str, str | None]]
"""From a call's bound arguments: its `action_type` and its `input`."""

_ACTIONS: dict[str, Inputs] = {
    "click": lambda _: ("click", None),
    "dblclick": lambda _: ("double_click", None),
    "fill": lambda a: ("type", str(a["value"])),
    "type": lambda a: ("type", str(a["text"])),
    "press_sequentially": lambda a: ("type", str(a["text"])),
    "press": lambda a: ("press", str(a["key"])),
    "select_option": lambda a: ("select", _options(a)),
    "check": lambda _: ("check", "true"),
    "uncheck": lambda _: ("uncheck", "false"),
    "set_checked": lambda a: (
        ("check", "true") if a["checked"] else ("uncheck", "false")
    ),
    "hover": lambda _: ("hover", None),
    "drag_to": lambda _: ("drag", None),
    "set_input_files": lambda a: ("upload", _files(a["files"])),
    "scroll_into_view_if_needed": lambda _: ("scroll", None),
}
_READS = (
    "inner_text",
    "text_content",
    "input_value",
    "get_attribute",
    "all_inner_texts",
    "all_text_contents",
)
_LOCATOR_CHAINS = (
    "locator",
    "get_by_alt_text",
    "get_by_label",
    "get_by_placeholder",
    "get_by_role",
    "get_by_test_id",
    "get_by_text",
    "get_by_title",
    "filter",
    "and_",
    "or_",
    "nth",
    "frame_locator",
)
_LOCATOR_CHAIN_PROPERTIES = ("first", "last", "content_frame")
_FRAME_CHAINS = (
    "locator",
    "get_by_alt_text",
    "get_by_label",
    "get_by_placeholder",
    "get_by_role",
    "get_by_test_id",
    "get_by_text",
    "get_by_title",
    "frame_locator",
    "nth",
)
_FRAME_CHAIN_PROPERTIES = ("first", "last", "owner")
_PAGE_CHAINS = (
    "locator",
    "get_by_alt_text",
    "get_by_label",
    "get_by_placeholder",
    "get_by_role",
    "get_by_test_id",
    "get_by_text",
    "get_by_title",
    "frame_locator",
)
_PAGE_SHORTCUTS = (
    "click",
    "dblclick",
    "fill",
    "type",
    "press",
    "select_option",
    "check",
    "uncheck",
    "set_checked",
    "hover",
    "set_input_files",
    "inner_text",
    "text_content",
    "input_value",
    "get_attribute",
)
"""Page methods that take a selector first and do what the Locator method of the same name does."""


def _wrap(recorder: Recorder, value: object) -> object:
    if isinstance(value, Locator):
        return RecordingLocator(value, recorder)
    if isinstance(value, FrameLocator):
        return RecordingFrameLocator(value, recorder)
    return value


class RecordingLocator(Locator):
    """A Locator whose logged calls go through the Event's Recorder; the Locators it makes do too."""

    def __init__(self, locator: Locator, recorder: Recorder) -> None:
        super().__init__(locator._impl_obj)
        self._recorder: Recorder = recorder

    async def _act(
        self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        base = getattr(Locator, method)
        bound = inspect.signature(base).bind(self, *args, **kwargs).arguments
        action_type, input = _ACTIONS[method](bound)
        self._recorder.begin_action(method, action_type, input)
        before = await _snapshot(self)
        result = await base(self, *args, **kwargs)
        after = await _snapshot(self)
        known = before or after
        target = known.target if known and known.target else _selector(self)
        group = known.group if known else "root"
        if action_type == "scroll":
            before_scroll = before.scroll if before else None
            after_scroll = after.scroll if after else None
            if before_scroll and after_scroll:
                delta = (
                    after_scroll[0] - before_scroll[0],
                    after_scroll[1] - before_scroll[1],
                )
                input = _scroll_text(delta)
            target = known.scroller if known else None
            previous = (
                _scroll_text(before_scroll) if before_scroll else PREVIOUS_UNAVAILABLE
            )
            current = (
                _scroll_text(after_scroll) if after_scroll else CURRENT_UNAVAILABLE
            )
        else:
            if action_type == "select" and after and after.selected is not None:
                input = after.selected
            previous = before.state if before else PREVIOUS_UNAVAILABLE
            if after:
                current = after.state
            else:
                current = (
                    NO_LONGER_ON_SCREEN if await _gone(self) else CURRENT_UNAVAILABLE
                )
        self._recorder.action(
            LoggedCall(
                action_type, input, target, group, _cut(previous), _cut(current), _now()
            )
        )
        return result

    async def _read(
        self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        self._recorder.begin_read()
        value: object = await getattr(Locator, method)(self, *args, **kwargs)
        snapshot = await _snapshot(
            self if not method.startswith("all_") else self.first
        )
        target = snapshot.target if snapshot and snapshot.target else _selector(self)
        state = _cut(_read_state(value))
        self._recorder.read(
            LoggedCall(
                "read",
                None,
                target,
                snapshot.group if snapshot else "root",
                state,
                state,
                _now(),
            )
        )
        return value


async def _all(self: RecordingLocator) -> list[Locator]:
    locators = await Locator.all(self)
    return [RecordingLocator(locator, self._recorder) for locator in locators]  # pyright: ignore[reportPrivateUsage]


class RecordingFrameLocator(FrameLocator):
    """A FrameLocator whose Locators record."""

    def __init__(self, frame_locator: FrameLocator, recorder: Recorder) -> None:
        super().__init__(frame_locator._impl_obj)
        self._recorder: Recorder = recorder


class RecordingMouse(Mouse):
    """The page's Mouse, whose `wheel` is logged as a page scroll."""

    def __init__(self, mouse: Mouse, page: Page, recorder: Recorder) -> None:
        super().__init__(mouse._impl_obj)
        self._page: Page = page
        self._recorder: Recorder = recorder

    @override
    async def wheel(self, delta_x: float, delta_y: float) -> None:
        delta = f"{delta_x:g},{delta_y:g}"
        self._recorder.begin_action("mouse.wheel", "scroll", delta)
        before = await _within_timeout(self._page.evaluate(_PAGE_SCROLL_JS))
        await super().wheel(delta_x, delta_y)
        after = await _within_timeout(self._page.evaluate(_PAGE_SCROLL_JS))
        self._recorder.action(
            LoggedCall(
                "scroll",
                delta,
                None,
                "root",
                _scroll_text(tuple(before)) if before else PREVIOUS_UNAVAILABLE,
                _scroll_text(tuple(after)) if after else CURRENT_UNAVAILABLE,
                _now(),
            )
        )


class RecordingPage(Page):
    """`ctx.page`: the active page, whose `goto`, selector shortcuts and Locators record."""

    def __init__(self, page: Page, recorder: Recorder) -> None:
        super().__init__(page._impl_obj)
        self._recorder: Recorder = recorder

    @property
    @override
    def mouse(self) -> Mouse:
        return RecordingMouse(super().mouse, self, self._recorder)

    @override
    async def goto(self, url: str, **kwargs: Any) -> Any:
        self._recorder.begin_action("goto", "navigate", None)
        previous = self.url
        response = await super().goto(url, **kwargs)
        current = self.url
        self._recorder.action(
            LoggedCall(
                "navigate", None, current, "root", _cut(previous), _cut(current), _now()
            )
        )
        return response

    def _shortcut(
        self, method: str, selector: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Awaitable[Any]:
        strict = kwargs.pop("strict", None)
        locator = self.locator(selector)
        if not strict:
            locator = locator.first
        return getattr(locator, method)(*args, **kwargs)

    @override
    async def drag_and_drop(self, source: str, target: str, **kwargs: Any) -> None:
        strict = kwargs.pop("strict", None)
        source_locator, target_locator = self.locator(source), self.locator(target)
        if not strict:
            source_locator, target_locator = source_locator.first, target_locator.first
        await source_locator.drag_to(target_locator, **kwargs)


# Install the recorded calls and the chains that keep later Locators recording. They are set
# after the classes so each keeps the signature and docs of the Playwright method it covers.


def _install_action(method: str) -> None:
    @functools.wraps(getattr(Locator, method))
    def call(self: RecordingLocator, *args: Any, **kwargs: Any) -> Awaitable[Any]:
        return self._act(method, args, kwargs)  # pyright: ignore[reportPrivateUsage]

    setattr(RecordingLocator, method, call)


def _install_read(method: str) -> None:
    @functools.wraps(getattr(Locator, method))
    def call(self: RecordingLocator, *args: Any, **kwargs: Any) -> Awaitable[Any]:
        return self._read(method, args, kwargs)  # pyright: ignore[reportPrivateUsage]

    setattr(RecordingLocator, method, call)


def _install_chain(cls: type[Any], base: type[Any], method: str) -> None:
    original = getattr(base, method)

    @functools.wraps(original)
    def call(self: Any, *args: Any, **kwargs: Any) -> object:
        return _wrap(self._recorder, original(self, *args, **kwargs))

    setattr(cls, method, call)


def _install_chain_property(cls: type[Any], base: type[Any], name: str) -> None:
    original = cast("property", getattr(base, name))
    getter = cast("Callable[[Any], object]", original.fget)
    setattr(cls, name, property(lambda self: _wrap(self._recorder, getter(self))))


def _install_shortcut(method: str) -> None:
    @functools.wraps(getattr(Page, method))
    def call(
        self: RecordingPage, selector: str, *args: Any, **kwargs: Any
    ) -> Awaitable[Any]:
        return self._shortcut(method, selector, args, kwargs)  # pyright: ignore[reportPrivateUsage]

    setattr(RecordingPage, method, call)


RecordingLocator.all = functools.wraps(Locator.all)(_all)
for _method in _ACTIONS:
    _install_action(_method)
for _method in _READS:
    _install_read(_method)
for _method in _LOCATOR_CHAINS:
    _install_chain(RecordingLocator, Locator, _method)
for _name in _LOCATOR_CHAIN_PROPERTIES:
    _install_chain_property(RecordingLocator, Locator, _name)
for _method in _FRAME_CHAINS:
    _install_chain(RecordingFrameLocator, FrameLocator, _method)
for _name in _FRAME_CHAIN_PROPERTIES:
    _install_chain_property(RecordingFrameLocator, FrameLocator, _name)
for _method in _PAGE_CHAINS:
    _install_chain(RecordingPage, Page, _method)
for _method in _PAGE_SHORTCUTS:
    _install_shortcut(_method)
