"""The vision Modality seam: `VisionModality.observe` / `act` on real headless Chromium.

Actions are fed as Reparo sends them (`action_type` plus `action_inputs`) and checked on the
fixture page's DOM state. Reparo's reference WebSocket client (`scripts/recovery_ws_client`)
is the reference for the action and alias behaviour.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta

import pytest
from playwright.async_api import Browser, BrowserContext, Page

from reparo_playwright_bridge import DomModality, Modality, VisionModality
from tests.fixture_server import FixtureServer


def test_vision_is_a_modality_targeting_coordinates_with_moderate_defaults() -> None:
    vision: Modality = VisionModality()

    assert vision.name == "vision"
    assert vision.target_kinds == {"coordinates"}
    assert vision.scroll_magnitude == 300
    assert vision.drag_steps == 20
    assert vision.screenshot_budget == timedelta(seconds=15)
    assert vision.key_table["pagedown"] == "PageDown"


def jpeg_size(data: bytes) -> tuple[int, int]:
    """The (width, height) in a JPEG's start-of-frame header."""
    assert data[:3] == b"\xff\xd8\xff", "not a JPEG"
    i = 2
    while i < len(data):
        marker, length = data[i + 1], int.from_bytes(data[i + 2 : i + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5 : i + 7])
            width = int.from_bytes(data[i + 7 : i + 9])
            return width, height
        i += 2 + length
    raise AssertionError("no start-of-frame marker")


@pytest.fixture
async def page(context: BrowserContext, site: FixtureServer) -> Page:
    page = await context.new_page()
    _ = await page.goto(site.page("/plain", "<h1>Plain</h1>"))
    return page


async def test_observe_returns_a_viewport_jpeg(page: Page) -> None:
    shot = await VisionModality().observe(page, {"type": "screenshot"})

    assert isinstance(shot, bytes)
    assert jpeg_size(shot) == (1280, 720)


@pytest.fixture
async def hidpi_page(browser: Browser, site: FixtureServer) -> AsyncIterator[Page]:
    context = await browser.new_context(
        viewport={"width": 800, "height": 600}, device_scale_factor=2
    )
    page = await context.new_page()
    _ = await page.goto(site.page("/tall", "<div style='height:5000px'>Tall</div>"))
    yield page
    await context.close()


async def test_observe_is_at_css_scale_and_viewport_only_on_a_hidpi_page(
    hidpi_page: Page,
) -> None:
    shot = await VisionModality().observe(hidpi_page, {"type": "screenshot"})

    assert isinstance(shot, bytes)
    assert jpeg_size(shot) == (800, 600)


# --------------------------------------------------------------------------- Actions

# Every target sits at a fixed CSS position in the 1280x720 viewport; its centre is in the
# comment. The page records what happened to it in `window.events`.
CONTROLS = """
<style>
  body { margin: 0; }
  .at { position: absolute; width: 100px; height: 40px; margin: 0; padding: 0; box-sizing: border-box; }
</style>
<button id="click" class="at" style="left:0; top:0">Click</button>          <!-- (50, 20) -->
<button id="double" class="at" style="left:200px; top:0">Double</button>    <!-- (250, 20) -->
<button id="right" class="at" style="left:400px; top:0">Right</button>      <!-- (450, 20) -->
<div id="hover" class="at" style="left:600px; top:0">Hover</div>             <!-- (650, 20) -->
<form id="form" onsubmit="record('submit'); return false">
  <input id="text" class="at" style="left:0; top:100px; width:300px">        <!-- (150, 120) -->
</form>
<div id="scroller" class="at" style="left:0; top:200px; width:300px; height:200px; overflow:auto">
  <div style="width:3000px; height:3000px"></div>
</div>                                                                       <!-- (150, 300) -->
<div id="source" class="at" style="left:400px; top:200px">Source</div>      <!-- (450, 220) -->
<div id="target" class="at" style="left:800px; top:200px">Target</div>      <!-- (850, 220) -->
<script>
  window.events = [];
  const record = (event) => window.events.push(event);
  const on = (id, type, handler) => document.getElementById(id).addEventListener(type, handler);
  on("click", "click", () => record("click"));
  on("double", "dblclick", () => record("dblclick"));
  on("right", "contextmenu", (e) => { e.preventDefault(); record("contextmenu"); });
  on("hover", "mouseenter", () => record("hover"));
  document.addEventListener("keydown", (e) => record("keydown:" + e.key));
  document.addEventListener("keyup", (e) => record("keyup:" + e.key));
  let moves = null;
  on("source", "mousedown", () => { moves = 0; });
  document.addEventListener("mousemove", () => { if (moves !== null) moves += 1; });
  document.addEventListener("mouseup", (e) => {
    if (moves === null) return;
    record("drop:" + document.elementFromPoint(e.clientX, e.clientY).id + ":" + moves);
    moves = null;
  });
</script>
"""


@pytest.fixture
async def controls(context: BrowserContext, site: FixtureServer) -> Page:
    page = await context.new_page()
    _ = await page.goto(site.page("/controls", CONTROLS))
    return page


async def act(page: Page, action_type: str, **inputs: object) -> Mapping[str, object]:
    return await VisionModality().act(
        page, {"action_type": action_type, "action_inputs": inputs}
    )


async def events(page: Page) -> list[str]:
    return await page.evaluate("window.events")  # pyright: ignore[reportAny]


@pytest.mark.parametrize(
    ("action_type", "box", "event"),
    [
        ("left_single", [50, 20], "click"),
        ("click", [50, 20], "click"),
        ("left_double", [250, 20], "dblclick"),
        ("right_single", [450, 20], "contextmenu"),
        ("hover", [650, 20], "hover"),
    ],
)
async def test_pointer_actions_reach_the_element_at_the_coordinates(
    controls: Page, action_type: str, box: list[float], event: str
) -> None:
    reply = await act(controls, action_type, start_box=box)

    assert reply == {"success": True, "action_type": action_type}
    assert event in await events(controls)


@pytest.mark.parametrize(
    "box",
    [
        pytest.param([0, 0, 100, 40], id="4-element box, its centre"),
        pytest.param([50 / 1280, 20 / 720], id="normalised"),
        pytest.param([50 / 1280, 20], id="normalised x only"),
        pytest.param([0, 0, 100, 40 / 720], id="box in pixels on x, normalised on y"),
    ],
)
async def test_boxes_are_reduced_to_their_centre_and_normalised_per_axis(
    controls: Page, box: list[float]
) -> None:
    _ = await act(controls, "left_single", start_box=box)

    assert await events(controls) == ["click"]


async def test_type_inserts_the_text_where_the_focus_is(controls: Page) -> None:
    _ = await act(controls, "left_single", start_box=[150, 120])

    reply = await act(controls, "type", content="Ana López")

    assert reply == {"success": True, "action_type": "type"}
    assert await controls.locator("#text").input_value() == "Ana López"
    assert "submit" not in await events(controls)


async def test_type_with_a_trailing_newline_inserts_the_text_then_presses_enter(
    controls: Page,
) -> None:
    _ = await act(controls, "left_single", start_box=[150, 120])

    _ = await act(controls, "type", content="Ana\n")

    assert await controls.locator("#text").input_value() == "Ana"
    assert await events(controls) == ["keydown:Enter", "submit", "keyup:Enter"]


@pytest.mark.parametrize(
    ("field", "chord"),
    [
        ("hotkey", "ctrl a"),
        ("key", "ctrl a"),
        ("press", "ctrl a"),
        ("hotkey", "ctrl+a"),
        ("hotkey", "control + a"),
    ],
)
async def test_hotkey_presses_the_mapped_chord(
    controls: Page, field: str, chord: str
) -> None:
    _ = await act(controls, "left_single", start_box=[150, 120])
    _ = await act(controls, "type", content="old text")

    reply = await act(controls, "hotkey", **{field: chord})
    _ = await act(controls, "type", content="new")

    assert reply == {"success": True, "action_type": "hotkey"}
    assert await controls.locator("#text").input_value() == "new"


@pytest.mark.parametrize(
    ("down", "up", "field"),
    [
        ("keydown", "keyup", "key"),
        ("press", "release", "key"),
        ("keydown", "keyup", "press"),
        ("keydown", "keyup", "hotkey"),
    ],
)
async def test_keydown_and_keyup_hold_and_release_the_mapped_key(
    controls: Page, down: str, up: str, field: str
) -> None:
    assert await act(controls, down, **{field: "shift"}) == {
        "success": True,
        "action_type": down,
    }
    assert await act(controls, up, **{field: "shift"}) == {
        "success": True,
        "action_type": up,
    }

    assert await events(controls) == ["keydown:Shift", "keyup:Shift"]


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("ctrl", "Control"),
        ("control", "Control"),
        ("shift", "Shift"),
        ("alt", "Alt"),
        ("win", "Meta"),
        ("cmd", "Meta"),
        ("enter", "Enter"),
        ("esc", "Escape"),
        ("escape", "Escape"),
        ("tab", "Tab"),
        ("space", " "),
        ("backspace", "Backspace"),
        ("delete", "Delete"),
        ("up", "ArrowUp"),
        ("down", "ArrowDown"),
        ("left", "ArrowLeft"),
        ("right", "ArrowRight"),
        ("pageup", "PageUp"),
        ("pagedown", "PageDown"),
        ("pgdown", "PageDown"),
        ("home", "Home"),
        ("end", "End"),
        ("f5", "F5"),
        ("minus", "-"),
        ("a", "a"),
        ("A", "A"),
        ("-", "-"),
        ("ENTER", "Enter"),
    ],
)
async def test_the_key_table_maps_pyautogui_names_to_playwright_keys(
    controls: Page, name: str, key: str
) -> None:
    _ = await act(controls, "keydown", key=name)
    _ = await act(controls, "keyup", key=name)

    assert await events(controls) == [f"keydown:{key}", f"keyup:{key}"]


async def scroll_position(page: Page) -> list[int]:
    return await page.evaluate(  # pyright: ignore[reportAny]
        "(() => { const s = document.getElementById('scroller'); return [s.scrollLeft, s.scrollTop]; })()"
    )


@pytest.mark.parametrize(
    ("direction", "position"),
    [("down", [0, 300]), ("right", [300, 0])],
)
async def test_scroll_wheels_the_element_under_the_point_by_the_magnitude(
    controls: Page, direction: str, position: list[int]
) -> None:
    reply = await act(controls, "scroll", start_box=[150, 300], direction=direction)

    assert reply == {"success": True, "action_type": "scroll"}
    await expect_scroll(controls, position)


async def test_scroll_up_and_left_wheel_back_with_a_configured_magnitude(
    controls: Page,
) -> None:
    vision = VisionModality(scroll_magnitude=100)
    for direction in ("down", "down", "right", "right", "up", "left"):
        _ = await vision.act(
            controls,
            {
                "action_type": "scroll",
                "action_inputs": {"start_box": [150, 300], "direction": direction},
            },
        )

    await expect_scroll(controls, [100, 100])


async def expect_scroll(page: Page, position: list[int]) -> None:
    # Wheel scrolling lands asynchronously; poll briefly.
    for _ in range(50):
        if await scroll_position(page) == position:
            return
        await page.wait_for_timeout(20)
    assert await scroll_position(page) == position


@pytest.mark.parametrize("action_type", ["drag", "select"])
async def test_drag_presses_moves_in_steps_and_releases_over_the_end_box(
    controls: Page, action_type: str
) -> None:
    reply = await act(
        controls, action_type, start_box=[450, 220], end_box=[800, 200, 900, 240]
    )

    assert reply == {"success": True, "action_type": action_type}
    assert await events(controls) == ["drop:target:20"]


async def test_drag_steps_are_a_setting(controls: Page) -> None:
    _ = await VisionModality(drag_steps=5).act(
        controls,
        {
            "action_type": "drag",
            "action_inputs": {"start_box": [450, 220], "end_box": [850, 220]},
        },
    )

    assert await events(controls) == ["drop:target:5"]


@pytest.mark.parametrize("field", ["seconds", "duration", "time"])
async def test_wait_sleeps_for_the_given_seconds(controls: Page, field: str) -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()

    reply = await act(controls, "wait", **{field: 0.3})

    assert reply == {"success": True, "action_type": "wait"}
    assert loop.time() - started >= 0.3


async def test_wait_defaults_to_no_time(controls: Page) -> None:
    assert await act(controls, "wait") == {"success": True, "action_type": "wait"}


# --------------------------------------------------------------------------- Failures


async def test_a_playwright_error_while_acting_is_the_reply_not_a_raise(
    controls: Page,
) -> None:
    await controls.close()

    reply = await act(controls, "left_single", start_box=[50, 20])

    assert reply["success"] is False
    assert reply["action_type"] == "left_single"
    assert "closed" in str(reply["error"])


async def test_a_key_playwright_does_not_know_fails_the_action(controls: Page) -> None:
    reply = await act(controls, "hotkey", hotkey="ctrl notakey")

    assert reply["success"] is False
    assert reply["action_type"] == "hotkey"
    assert "notakey" in str(reply["error"])


@pytest.mark.parametrize(
    ("action_type", "inputs"),
    [
        ("left_single", {}),
        ("left_single", {"start_box": [1, 2, 3]}),
        ("scroll", {"start_box": [150, 300], "direction": "sideways"}),
        ("wait", {"seconds": "soon"}),
        ("wait", {"seconds": -1}),
        ("keydown", {}),
        ("teleport", {}),
    ],
)
async def test_malformed_actions_fail_with_a_reason(
    controls: Page, action_type: str, inputs: dict[str, object]
) -> None:
    reply = await act(controls, action_type, **inputs)

    assert reply["success"] is False
    assert reply["action_type"] == action_type
    assert reply["error"]


def test_dom_modality_is_not_available_yet() -> None:
    with pytest.raises(NotImplementedError, match="217"):
        _ = DomModality()
