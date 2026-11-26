# pyright: reportAny=false, reportExplicitAny=false
# (The proxies forward any Playwright arguments; their declared types stay Playwright's own.)
"""The recording page behind `ctx.page`: it records the one logged call of each Event.

`RecordingPage`, `RecordingLocator`, `RecordingFrameLocator` and `RecordingMouse` subclass
Playwright's own classes over the same objects, so author code (and `expect`) sees a normal Page.
Each logged call goes through the Event's `Recorder`, which enforces one action per Event and
captures the UI log fields; every other call is Playwright's, untouched.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast, override

from playwright.async_api import FrameLocator, Locator, Mouse, Page

from reparo_playwright_bridge._classify import ProcessBug
from reparo_playwright_bridge._logged_calls import (
    ACTIONS,
    NO_ATTEMPT,
    PAGE_SHORTCUTS,
    READ,
    READS,
    SCROLL_INTO_VIEW,
    Attempt,
    LoggedCall,
    timestamp,
)
from reparo_playwright_bridge._state_reads import (
    CURRENT_UNAVAILABLE,
    NO_LONGER_ON_SCREEN,
    PREVIOUS_UNAVAILABLE,
    application,
    cut,
    gone,
    page_scroll,
    read_snapshot,
    read_state,
    scroll_delta,
    scroll_text,
    selector_of,
)


class Recorder:
    """What one Event did through `ctx.page`: its action, its last read, and what it attempted."""

    def __init__(self, event_label: str) -> None:
        self.event_label: str = event_label
        """The Event as log lines name it (`click_approve #0`), for the Process bugs it raises."""
        self.application: str | None = None
        """The page title at Event start, else the URL host; set by `open`."""
        self._action: LoggedCall | None = None
        self._read: LoggedCall | None = None
        self._attempted_action: Attempt | None = None
        self._attempted_read: bool = False
        self._broken: ProcessBug | None = None

    async def open(self, page: Page) -> RecordingPage:
        """`page` as the Event's `ctx.page`; reads the application the Event starts on."""
        self.application = await application(page)
        return RecordingPage(page, self)

    @property
    def attempted(self) -> Attempt:
        """The Event's action if it attempted one, else a read if it attempted one."""
        if self._attempted_action is not None:
            return self._attempted_action
        return READ if self._attempted_read else NO_ATTEMPT

    def begin_action(self, method: str, attempt: Attempt) -> None:
        """Called before an action runs: a second action is a Process bug, raised instead."""
        if self._attempted_action is not None:
            self._broken = ProcessBug(
                f"Event {self.event_label} performed a second action ({method}) after its"
                + f" {self._attempted_action.action_type}; an Event performs exactly one action"
            )
            raise self._broken
        self._attempted_action = attempt

    def begin_read(self) -> None:
        self._attempted_read = True

    def action(self, call: LoggedCall) -> None:
        self._action = call

    def read(self, call: LoggedCall) -> None:
        self._read = call

    def finish(self) -> LoggedCall:
        """The Event's logged call: its action, else its last read. None at all is a Process bug."""
        if self._broken is not None:
            raise self._broken
        call = self._action or self._read
        if call is None:
            raise ProcessBug(
                f"Event {self.event_label} performed no logged call; an Event performs one"
                + " action or at least one read"
            )
        return call


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
        attempt = ACTIONS[method](
            inspect.signature(base).bind(self, *args, **kwargs).arguments
        )
        self._recorder.begin_action(method, attempt)
        before = await read_snapshot(self)
        result = await base(self, *args, **kwargs)
        after = await read_snapshot(self)
        known = before or after
        input = attempt.input
        if attempt.action_type == "select" and after and after.selected is not None:
            input = after.selected  # the labels chosen, however the options were named
        if after:
            current = after.state
        elif await gone(self):
            current = NO_LONGER_ON_SCREEN
        else:
            current = CURRENT_UNAVAILABLE
        self._recorder.action(
            LoggedCall(
                attempt.action_type,
                input,
                known.target if known and known.target else selector_of(self),
                known.group if known else "root",
                cut(before.state if before else PREVIOUS_UNAVAILABLE),
                cut(current),
                timestamp(),
            )
        )
        return result

    async def _scroll(
        self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        """A scroll's target is the element's scrollable ancestor; its states, that offset."""
        self._recorder.begin_action(method, Attempt("scroll", None))
        before = await read_snapshot(self)
        await Locator.scroll_into_view_if_needed(self, *args, **kwargs)
        after = await read_snapshot(self)
        known = before or after
        delta = (
            scroll_text(scroll_delta(before.scroll, after.scroll))
            if before and after
            else None
        )
        self._recorder.action(
            LoggedCall(
                "scroll",
                delta,
                known.scroller if known else None,
                known.group if known else "root",
                scroll_text(before.scroll) if before else PREVIOUS_UNAVAILABLE,
                scroll_text(after.scroll) if after else CURRENT_UNAVAILABLE,
                timestamp(),
            )
        )

    async def _read_call(
        self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        self._recorder.begin_read()
        value: object = await getattr(Locator, method)(self, *args, **kwargs)
        snapshot = await read_snapshot(
            self.first if method.startswith("all_") else self
        )
        state = cut(read_state(value))
        self._recorder.read(
            LoggedCall(
                "read",
                None,
                snapshot.target if snapshot and snapshot.target else selector_of(self),
                snapshot.group if snapshot else "root",
                state,
                state,
                timestamp(),
            )
        )
        return value

    @override
    async def all(self) -> list[Locator]:
        """The Locators for the elements this one matches, each recording too."""
        return [
            RecordingLocator(locator, self._recorder)
            for locator in await Locator.all(self)
        ]


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
        self._recorder.begin_action("mouse.wheel", Attempt("scroll", delta))
        before = await page_scroll(self._page)
        await super().wheel(delta_x, delta_y)
        after = await page_scroll(self._page)
        self._recorder.action(
            LoggedCall(
                "scroll",
                delta,
                None,
                "root",
                scroll_text(before) if before else PREVIOUS_UNAVAILABLE,
                scroll_text(after) if after else CURRENT_UNAVAILABLE,
                timestamp(),
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
        self._recorder.begin_action("goto", Attempt("navigate", None))
        previous = self.url
        response = await super().goto(url, **kwargs)
        current = self.url
        self._recorder.action(
            LoggedCall(
                "navigate",
                None,
                current,
                "root",
                cut(previous),
                cut(current),
                timestamp(),
            )
        )
        return response

    def _selected(self, selector: str, strict: bool | None) -> Locator:
        """What a selector shortcut acts on: without `strict`, Playwright takes the first match."""
        locator = self.locator(selector)
        return locator if strict else locator.first

    def _shortcut(
        self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Awaitable[Any]:
        rest = list(args)
        selector = rest.pop(0) if rest else kwargs.pop("selector")
        locator = self._selected(selector, kwargs.pop("strict", None))
        return getattr(locator, method)(*rest, **kwargs)

    @override
    async def drag_and_drop(self, source: str, target: str, **kwargs: Any) -> None:
        strict = kwargs.pop("strict", None)
        await self._selected(source, strict).drag_to(
            self._selected(target, strict), **kwargs
        )


# Install the recorded calls and the chains that keep later Locators recording. They are set
# after the classes so each keeps the signature and docs of the Playwright method it covers.

_GET_BY = (
    "get_by_alt_text",
    "get_by_label",
    "get_by_placeholder",
    "get_by_role",
    "get_by_test_id",
    "get_by_text",
    "get_by_title",
)
_MAKES_LOCATORS = ("locator", "frame_locator", *_GET_BY)
"""Methods that Page, Locator and FrameLocator share, each returning a Locator or FrameLocator."""


def _install(cls: type[Any], base: type[Any], method: str, handler: str) -> None:
    """`cls.method` hands its call to `self.<handler>(method, args, kwargs)`."""

    @functools.wraps(getattr(base, method))
    def call(self: Any, *args: Any, **kwargs: Any) -> Awaitable[Any]:
        return getattr(self, handler)(method, args, kwargs)

    setattr(cls, method, call)


def _install_chain(cls: type[Any], base: type[Any], method: str) -> None:
    original = getattr(base, method)

    @functools.wraps(original)
    def call(self: Any, *args: Any, **kwargs: Any) -> object:
        return _wrap(self._recorder, original(self, *args, **kwargs))

    setattr(cls, method, call)


def _install_chain_property(cls: type[Any], base: type[Any], name: str) -> None:
    getter = cast("Callable[[Any], object]", cast("property", getattr(base, name)).fget)
    setattr(cls, name, property(lambda self: _wrap(self._recorder, getter(self))))


for _method in ACTIONS:
    _install(RecordingLocator, Locator, _method, "_act")
_install(RecordingLocator, Locator, SCROLL_INTO_VIEW, "_scroll")
for _method in READS:
    _install(RecordingLocator, Locator, _method, "_read_call")
for _method in PAGE_SHORTCUTS:
    _install(RecordingPage, Page, _method, "_shortcut")

for _method in (*_MAKES_LOCATORS, "filter", "and_", "or_", "nth"):
    _install_chain(RecordingLocator, Locator, _method)
for _method in (*_MAKES_LOCATORS, "nth"):
    _install_chain(RecordingFrameLocator, FrameLocator, _method)
for _method in _MAKES_LOCATORS:
    _install_chain(RecordingPage, Page, _method)
for _name in ("first", "last", "content_frame"):
    _install_chain_property(RecordingLocator, Locator, _name)
for _name in ("first", "last", "owner"):
    _install_chain_property(RecordingFrameLocator, FrameLocator, _name)
