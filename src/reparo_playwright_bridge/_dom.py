"""DomModality: the planned DOM Modality, not available yet."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Never

from playwright.async_api import Page

_WAITING_ON = (
    "DomModality is not available yet: it waits on Reparo's DOM observations and selector "
    "targeting (TunixR/Reparo#217)"
)


class DomModality:
    """The DOM Modality: DOM observations and selector-targeted Recovery actions.

    Not available yet. It waits on Reparo's DOM observations and selector targeting
    (TunixR/Reparo#217); until then constructing it raises `NotImplementedError`.
    """

    def __init__(self) -> None:
        raise NotImplementedError(_WAITING_ON)

    @property
    def name(self) -> str:
        return "dom"

    @property
    def target_kinds(self) -> frozenset[str]:
        return frozenset({"selector"})

    async def observe(self, page: Page, request: Mapping[str, object]) -> Never:
        del page, request
        raise NotImplementedError(_WAITING_ON)

    async def act(self, page: Page, action: Mapping[str, object]) -> Never:
        del page, action
        raise NotImplementedError(_WAITING_ON)
