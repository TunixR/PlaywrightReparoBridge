"""The Recovery handler: serves Reparo's Observations and Recovery actions through the Modalities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from playwright.async_api import BrowserContext, Page, ViewportSize

from reparo_playwright_bridge._context import latest_open_page
from reparo_playwright_bridge._dom import DomModality
from reparo_playwright_bridge._jpeg import blank_jpeg
from reparo_playwright_bridge._log import recovery_log
from reparo_playwright_bridge._modality import Modality

VISION = "vision"
COORDINATES = "coordinates"
"""The target kind of a bare `start_box` / `end_box`."""

type Reply = bytes | Mapping[str, object]
"""One reply frame: binary for a binary Observation, JSON text otherwise."""


class RecoveryHandler:
    """Routes Reparo's requests to the loaded Modalities, most preferred first.

    Built once per run. Duplicate Modality names raise `ValueError`.
    """

    def __init__(self, modalities: Sequence[Modality]) -> None:
        names = [m.name for m in modalities]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError("duplicate Modality names: " + ", ".join(duplicates))
        self.modalities: tuple[Modality, ...] = tuple(modalities)

    @property
    def capabilities(self) -> dict[str, object]:
        """The payload's `capabilities`, as Reparo#217 defines them."""
        return {"modalities": [m.name for m in self.modalities], "ocr": False}

    @property
    def usable(self) -> bool:
        """Whether Reparo can use any loaded Modality: one that isn't a stub."""
        return any(not isinstance(m, DomModality) for m in self.modalities)

    @property
    def needs_fixed_viewport(self) -> bool:
        """`vision` maps screenshot pixels onto the mouse, so it needs a fixed viewport."""
        return self.modality_named(VISION) is not None

    def modality_named(self, name: str) -> Modality | None:
        return next((m for m in self.modalities if m.name == name), None)

    def modality_for(self, kind: str | None) -> Modality | None:
        """The most preferred Modality that owns target `kind`; any, for an untargeted action."""
        if kind is None:  # an action with no target: the most preferred Modality
            return self.modalities[0] if self.modalities else None
        return next((m for m in self.modalities if kind in m.target_kinds), None)

    def recovering(
        self, context: BrowserContext, extra: Mapping[str, object]
    ) -> RecoverySession:
        """A session serving one Recovery on `context`."""
        return RecoverySession(self, context, extra)


class RecoverySession:
    """Serves the requests of one Recovery on the active page.

    The active page is the most recently opened page still open, so a tab Reparo opens is
    followed, and stays the page the Continuation's Events run on. The viewport is kept at its
    size when the Recovery started.
    """

    def __init__(
        self,
        handler: RecoveryHandler,
        context: BrowserContext,
        extra: Mapping[str, object],
    ) -> None:
        self._handler: RecoveryHandler = handler
        self._context: BrowserContext = context
        self._extra: dict[str, object] = dict(extra)
        page = latest_open_page(context)
        self._viewport: ViewportSize | None = (
            None if page is None else page.viewport_size
        )

    async def _active_page(self) -> Page | None:
        page = latest_open_page(self._context)
        viewport = self._viewport
        if page is not None and viewport is not None and page.viewport_size != viewport:
            await page.set_viewport_size(viewport)
        return page

    def _cannot_serve[R: Reply](self, what: str, reply: R) -> R:
        recovery_log.warning(
            "cannot serve Reparo's request: %s", what, extra=self._extra
        )
        return reply

    def _blank(self) -> bytes:
        width, height = (
            (self._viewport["width"], self._viewport["height"])
            if self._viewport is not None
            else (1, 1)
        )
        return blank_jpeg(width, height)

    async def observe(self, modality_name: str) -> Reply:
        """An Observation in `modality_name`; a binary frame for `vision`, JSON otherwise."""
        modality = self._handler.modality_named(modality_name)
        if modality is None:
            if modality_name == VISION:
                return self._cannot_serve(
                    "a screenshot with no vision Modality loaded; a blank JPEG instead",
                    self._blank(),
                )
            return self._cannot_serve(
                f"an Observation in modality {modality_name}, which isn't loaded",
                {"success": False, "error": f"modality {modality_name} not supported"},
            )
        page = await self._active_page()
        try:
            if page is None:
                raise RuntimeError("no page is open")
            observation = await modality.observe(page, {"modality": modality_name})
        except Exception as exc:  # noqa: BLE001  # the failure becomes the reply
            fallback: Reply = (
                self._blank()
                if modality_name == VISION
                else {"success": False, "error": f"observation failed: {exc}"}
            )
            return self._cannot_serve(
                f"the {modality_name} Observation failed ({type(exc).__name__})",
                fallback,
            )
        if isinstance(observation, bytes):
            recovery_log.debug(
                "%s Observation sent: %s, %d bytes",
                modality_name,
                _viewport_text(page.viewport_size),
                len(observation),
                extra=self._extra,
            )
        else:
            recovery_log.debug("%s Observation sent", modality_name, extra=self._extra)
        return observation

    async def act(self, content: object) -> Mapping[str, object]:
        """Perform one Recovery action and reply `{success, action_type, error?}`."""
        if not isinstance(content, Mapping):
            return self._cannot_serve(
                "an action whose content isn't an object",
                _failed("unknown", "Invalid action content"),
            )
        action = cast("Mapping[str, object]", content)
        action_type = str(action.get("action_type", "unknown"))
        kind = _target_kind(action.get("action_inputs"))
        modality = self._handler.modality_for(kind)
        if modality is None:
            return self._cannot_serve(
                f"a {action_type} action targeted by {kind}",
                _failed(action_type, f"{kind} targeting not supported by this robot"),
            )
        page = await self._active_page()
        try:
            if page is None:
                raise RuntimeError("no page is open")
            reply = await modality.act(page, action)
        except Exception as exc:  # noqa: BLE001  # the failure becomes the reply
            reply = _failed(action_type, str(exc))
        recovery_log.debug(
            "Recovery action %s: %s",
            action_type,
            "success"
            if reply.get("success") is True
            else f"failed ({reply.get('error')})",
            extra=self._extra,
        )
        return reply


def _failed(action_type: str, error: str) -> dict[str, object]:
    return {"success": False, "action_type": action_type, "error": error}


def _target_kind(inputs: object) -> str | None:
    """How an action is aimed: its `target.by`, `coordinates` for a bare box, or None."""
    if not isinstance(inputs, Mapping):
        return None
    inputs = cast("Mapping[str, object]", inputs)
    target = inputs.get("target")
    if isinstance(target, Mapping):
        return str(cast("Mapping[str, object]", target).get("by"))
    if "start_box" in inputs or "end_box" in inputs:
        return COORDINATES
    return None


def _viewport_text(viewport: ViewportSize | None) -> str:
    return (
        "no viewport"
        if viewport is None
        else f"{viewport['width']}x{viewport['height']}"
    )
