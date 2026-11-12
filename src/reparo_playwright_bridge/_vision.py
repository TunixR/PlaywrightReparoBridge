"""VisionModality: Reparo sees screenshots and aims Recovery actions at coordinates."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta
from typing import Literal, cast

from playwright.async_api import Page

from reparo_playwright_bridge._keys import DEFAULT_KEY_TABLE, map_key, split_hotkey

DEFAULT_SCROLL_MAGNITUDE = 300
DEFAULT_DRAG_STEPS = 20
DEFAULT_SCREENSHOT_BUDGET = timedelta(seconds=15)

_ACTION_ALIASES: Mapping[str, str] = {
    "click": "left_single",
    "select": "drag",
    "press": "keydown",
    "release": "keyup",
}
"""Action types Reparo's reference robot also accepts, and the action each one is."""


class VisionModality:
    """Screenshot Observations and coordinate Recovery actions, as Reparo's reference robot does them.

    Settings: `scroll_magnitude` (CSS px per `scroll`), `drag_steps` (interpolated mouse moves
    per `drag`), `screenshot_budget` (how long a screenshot may take) and `key_table` (pyautogui
    key names to Playwright key names; defaults to `DEFAULT_KEY_TABLE`).
    """

    def __init__(
        self,
        *,
        scroll_magnitude: int = DEFAULT_SCROLL_MAGNITUDE,
        drag_steps: int = DEFAULT_DRAG_STEPS,
        screenshot_budget: timedelta = DEFAULT_SCREENSHOT_BUDGET,
        key_table: Mapping[str, str] = DEFAULT_KEY_TABLE,
    ) -> None:
        if scroll_magnitude < 1 or drag_steps < 1:
            raise ValueError("scroll_magnitude and drag_steps must be at least 1")
        if screenshot_budget <= timedelta(0):
            raise ValueError("screenshot_budget must be positive")
        self.scroll_magnitude: int = scroll_magnitude
        self.drag_steps: int = drag_steps
        self.screenshot_budget: timedelta = screenshot_budget
        self.key_table: Mapping[str, str] = key_table

    @property
    def name(self) -> str:
        return "vision"

    @property
    def target_kinds(self) -> frozenset[str]:
        return frozenset({"coordinates"})

    async def observe(self, page: Page, request: Mapping[str, object]) -> bytes:
        """A viewport-only JPEG at CSS scale, so its pixels are the coordinates Reparo sends."""
        del request  # a screenshot request carries nothing the capture depends on
        return await page.screenshot(
            type="jpeg",
            scale="css",
            full_page=False,
            timeout=self.screenshot_budget.total_seconds() * 1000,
        )

    async def act(
        self, page: Page, action: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Perform one Recovery action on `page` and reply `{success, action_type, error?}`.

        Never raises for a bad action or a Playwright error; the failure goes in the reply.
        """
        action_type = str(action.get("action_type", "unknown"))
        try:
            inputs = action.get("action_inputs", {})
            if not isinstance(inputs, Mapping):
                raise TypeError("action_inputs must be an object")
            await self._perform(
                page,
                _ACTION_ALIASES.get(action_type, action_type),
                cast("Mapping[object, object]", inputs),
            )
        except Exception as exc:  # noqa: BLE001 - every failure becomes the reply
            return {"success": False, "action_type": action_type, "error": str(exc)}
        return {"success": True, "action_type": action_type}

    async def _perform(
        self, page: Page, action_type: str, inputs: Mapping[object, object]
    ) -> None:
        mouse, keyboard = page.mouse, page.keyboard
        match action_type:
            case "left_single":
                await mouse.click(*_point(page, inputs, "start_box"))
            case "left_double":
                await mouse.dblclick(*_point(page, inputs, "start_box"))
            case "right_single":
                await mouse.click(*_point(page, inputs, "start_box"), button="right")
            case "hover":
                await mouse.move(*_point(page, inputs, "start_box"))
            case "type":
                content = str(inputs.get("content") or "")
                await keyboard.insert_text(content.rstrip("\n"))
                if content.endswith("\n"):
                    await keyboard.press("Enter")
            case "hotkey":
                keys = split_hotkey(_key_input(inputs))
                if not keys:
                    raise ValueError("hotkey requires one or more keys")
                await keyboard.press("+".join(self._key(k) for k in keys))
            case "keydown":
                await keyboard.down(self._single_key(inputs))
            case "keyup":
                await keyboard.up(self._single_key(inputs))
            case "scroll":
                await self._scroll(page, inputs)
            case "drag":
                sx, sy = _point(page, inputs, "start_box")
                ex, ey = _point(page, inputs, "end_box")
                await mouse.move(sx, sy)
                await mouse.down()
                await mouse.move(ex, ey, steps=self.drag_steps)
                await mouse.up()
            case "wait":
                await asyncio.sleep(_wait_seconds(inputs))
            case _:
                raise ValueError(f"Unsupported action_type: {action_type}")

    async def _scroll(self, page: Page, inputs: Mapping[object, object]) -> None:
        direction = str(inputs.get("direction", "")).lower()
        step = self.scroll_magnitude
        match direction:
            case "down":
                dx, dy = 0, step
            case "up":
                dx, dy = 0, -step
            case "right":
                dx, dy = step, 0
            case "left":
                dx, dy = -step, 0
            case _:
                raise ValueError(
                    f"Invalid scroll direction: {direction}. Must be 'up', 'down', 'left', or 'right'."
                )
        await page.mouse.move(*_point(page, inputs, "start_box"))
        await page.mouse.wheel(dx, dy)

    def _key(self, name: str) -> str:
        return map_key(name, self.key_table)

    def _single_key(self, inputs: Mapping[object, object]) -> str:
        raw = _key_input(inputs)
        name = raw if raw == " " else raw.strip()  # a bare space is the space bar
        if not name:
            raise ValueError("requires a key")
        return self._key(name)


def _point(
    page: Page, inputs: Mapping[object, object], field: str
) -> tuple[float, float]:
    """The CSS-pixel point the `[x, y]` or `[x1, y1, x2, y2]` box in `inputs[field]` aims at.

    Assumes absolute coordinates in the viewport's coordinate space: CSS pixels from the
    viewport's top-left corner, the same space as `observe`'s screenshot, not the page's.
    A box is reduced to its centre. On each axis, values all ≤ 1.0 are a fraction of the viewport.
    """
    box = inputs.get(field)
    if not isinstance(box, list | tuple):
        raise TypeError(f"{field} must be a list")
    match [float(v) for v in box]:  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        case [x, y]:
            x1, y1, x2, y2 = x, y, x, y
        case [x1, y1, x2, y2]:
            pass
        case _:
            raise ValueError(f"{field} must have length 2 or 4")
    return (
        _axis_centre(page, x1, x2, "width"),
        _axis_centre(page, y1, y2, "height"),
    )


def _axis_centre(
    page: Page, low: float, high: float, extent: Literal["width", "height"]
) -> float:
    """The CSS-pixel centre of one axis of a box; values all ≤ 1.0 scale by the viewport."""
    centre = (low + high) / 2
    if low > 1.0 or high > 1.0:
        return centre
    viewport = page.viewport_size
    if viewport is None:
        raise ValueError("normalised coordinates need a fixed viewport")
    return centre * viewport[extent]


def _key_input(inputs: Mapping[object, object]) -> str:
    return str(inputs.get("hotkey") or inputs.get("key") or inputs.get("press") or "")


def _wait_seconds(inputs: Mapping[object, object]) -> float:
    raw = inputs.get("seconds", inputs.get("duration", inputs.get("time", 0)))
    try:
        seconds = float(raw)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        raise ValueError("wait requires a numeric seconds/duration") from None
    if seconds < 0:
        raise ValueError("wait seconds must be non-negative")
    return seconds
