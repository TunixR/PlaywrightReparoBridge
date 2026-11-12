"""The Modality protocol: how Reparo sees the browser and aims Recovery actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from playwright.async_api import Page


class Modality(Protocol):
    """A way the bridge lets Reparo see the browser and target elements during a Recovery."""

    @property
    def name(self) -> str: ...

    @property
    def target_kinds(self) -> frozenset[str]: ...

    async def observe(
        self, page: Page, request: Mapping[str, object]
    ) -> bytes | Mapping[str, object]: ...

    async def act(
        self, page: Page, action: Mapping[str, object]
    ) -> Mapping[str, object]: ...
