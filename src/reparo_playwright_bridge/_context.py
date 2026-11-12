"""What an Event body receives."""

from __future__ import annotations

from typing import Any, NoReturn

from playwright.async_api import Page

Variables = dict[str, Any]  # pyright: ignore[reportExplicitAny]  # Variables hold any serialisable value

EXPECTATION_NOT_MET = "ExpectationNotMet"


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
