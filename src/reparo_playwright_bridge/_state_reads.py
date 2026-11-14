# pyright: reportAny=false, reportExplicitAny=false
# (Page evaluations return any JSON.)
"""Best-effort reads of the page around a logged call: they never fail the Event.

Each read times out after 500 ms; what can't be read comes back as None, and the recording page
turns it into the spec's state placeholder.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

from playwright.async_api import Locator, Page

STATE_LIMIT = 200
READ_TIMEOUT_MS = 500
PREVIOUS_UNAVAILABLE = "[Previous state unavailable]"
CURRENT_UNAVAILABLE = "[Current state unavailable]"
NO_LONGER_ON_SCREEN = "[No longer on screen]"

Scroll = tuple[int, int]
"""A scroll offset, `(x, y)` in CSS pixels."""

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


@dataclass(frozen=True)
class Snapshot:
    """What one element looked like, read in one evaluation."""

    target: str
    """Its accessible name or trimmed visible text; empty if it has neither."""
    group: str
    """The name of its nearest named container, else `"root"`."""
    state: str
    """Its value: checked state, field value, or trimmed text."""
    scroller: str | None
    """Its nearest scrollable ancestor, named; None for the page."""
    scroll: Scroll
    """The scroll offset of that ancestor, or of the page."""
    selected: str | None
    """For a `<select>`, the labels of its selected options."""


def cut(state: str) -> str:
    return state[:STATE_LIMIT]


def scroll_text(scroll: Scroll) -> str:
    return f"{scroll[0]},{scroll[1]}"


def scroll_delta(before: Scroll, after: Scroll) -> Scroll:
    return (after[0] - before[0], after[1] - before[1])


def read_state(value: object) -> str:
    """What a read returned, as a state: a list joined, a missing text empty."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in cast("list[object]", value))
    return "" if value is None else str(value)


async def within_timeout[T](awaitable: Awaitable[T]) -> T | None:
    """`awaitable`'s value, or None if it fails or takes longer than the state-read timeout."""
    try:
        return await asyncio.wait_for(awaitable, READ_TIMEOUT_MS / 1000)
    except Exception:  # noqa: BLE001  # a state read never fails the Event
        return None


async def application(page: Page) -> str | None:
    """The page title, else the URL host."""
    title = await within_timeout(page.title())
    if title and title.strip():
        return title.strip()
    return urlsplit(page.url).hostname or None


async def read_snapshot(locator: Locator) -> Snapshot | None:
    try:
        raw = await locator.evaluate(_SNAPSHOT_JS, timeout=READ_TIMEOUT_MS)
    except Exception:  # noqa: BLE001  # a state read never fails the Event
        return None
    values = cast("dict[str, Any]", raw)
    scroll = cast("list[int]", values["scroll"])
    return Snapshot(
        target=str(values["target"]),
        group=str(values["group"]),
        state=str(values["state"]),
        scroller=values["scroller"],
        scroll=(int(scroll[0]), int(scroll[1])),
        selected=values["selected"],
    )


async def page_scroll(page: Page) -> Scroll | None:
    scroll = await within_timeout(page.evaluate(_PAGE_SCROLL_JS))
    if scroll is None:
        return None
    x, y = cast("list[int]", scroll)
    return (int(x), int(y))


async def gone(locator: Locator) -> bool:
    """Whether the element is confirmed gone; False when that can't be told in time."""
    return await within_timeout(locator.count()) == 0


def selector_of(locator: Locator) -> str:
    """The locator's selector, the target of last resort."""
    impl = cast("object", locator._impl_obj)  # pyright: ignore[reportPrivateUsage]
    value = getattr(impl, "_selector", None)
    return value if isinstance(value, str) else repr(locator)
