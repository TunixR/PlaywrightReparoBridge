"""What an Event body and a Completion check receive."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

from playwright.async_api import BrowserContext, Page

Variables = dict[str, Any]  # pyright: ignore[reportExplicitAny]  # Variables hold any serialisable value

EXPECTATION_NOT_MET = "ExpectationNotMet"


async def active_page(context: BrowserContext) -> Page:
    """The most recently opened page that is still open; a new page only if there is none."""
    pages = [page for page in context.pages if not page.is_closed()]
    return pages[-1] if pages else await context.new_page()


class UIFailure(Exception):
    """A UI failure the author declared with `ctx.fail`."""

    def __init__(self, description: str, code: str) -> None:
        super().__init__(description)
        self.description: str = description
        self.code: str = code


class EventContext:
    """The context an Event body runs with.

    `page` is the context's active page at the start of the Event (the most recently opened page
    that is still open). `vars` is the mutable Variables mapping, shared across Events.
    """

    def __init__(self, page: Page, variables: Variables) -> None:
        self.page: Page = page
        self.vars: Variables = variables

    def fail(self, description: str, *, code: str = EXPECTATION_NOT_MET) -> NoReturn:
        """Declare a UI failure: the page is in a state the Process didn't expect."""
        raise UIFailure(description, code)


class CheckContext:
    """The context a Completion check runs with.

    `page` is the context's active page when the check starts; nothing done on it reaches the UI
    log. `vars` is a read-only view of the final Variables: writing to it is a Process bug.
    There is no `fail`: a check returns `(True, None)` or `(False, reason)`.
    """

    def __init__(self, page: Page, variables: Mapping[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
        self.page: Page = page
        self.vars: Mapping[str, Any] = variables  # pyright: ignore[reportExplicitAny]
