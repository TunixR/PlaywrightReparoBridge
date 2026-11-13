# pyright: reportAny=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
# (FakeReparo records payloads and replies as decoded JSON, whose values are any JSON.)
"""Recovery actions through the Modalities: `run()` against FakeReparo on real Chromium.

Most tests drive the vision Modality; the protocol loop and the Recovery handler are also
driven with a scripted fake Modality, to see routing and replies from the outside.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Browser, BrowserContext, Page

from reparo_playwright_bridge import (
    CheckContext,
    EventContext,
    Modality,
    Process,
    ReparoSettings,
    RunResult,
    VisionModality,
    run,
)
from reparo_playwright_bridge.testing import (
    Act,
    Continue,
    Fail,
    FakeReparo,
    FakeReparoError,
    Locate,
    Observe,
    SendRaw,
    Silence,
    Step,
)
from tests.fixture_server import FixtureServer
from tests.test_vision import jpeg_size

# The original UI shows Approve directly; the changed UI hides it behind a menu.
CHANGED_UI = """
<button id="menu" onclick="document.getElementById('approve').hidden = false">More</button>
<button id="approve" hidden
  onclick="document.getElementById('status').textContent = 'approved'">Approve</button>
<p id="status">pending</p>
"""


def settings(reparo: FakeReparo, **overrides: object) -> ReparoSettings:
    return ReparoSettings(
        base_url=reparo.url,
        robot_key=reparo.robot_key,
        **overrides,  # pyright: ignore[reportArgumentType]
    )


def approval_process() -> Process:
    """open_page -> click_approve -> read_status; met when the status read is `approved`."""
    p = Process("approve requests")
    with p.activity("Approve the request"):

        @p.event("Open the page")
        async def open_page(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/page")

        @p.event("Click Approve")
        async def click_approve(ctx: EventContext) -> None:
            await ctx.page.locator("#approve").click(timeout=300)

        @p.event("Read the status")
        async def read_status(ctx: EventContext) -> None:
            ctx.vars["status"] = await ctx.page.locator("#status").text_content()

    @p.completion_check("The request is approved")
    async def approved(ctx: CheckContext) -> tuple[bool, str | None]:
        status = ctx.vars.get("status")
        return (True, None) if status == "approved" else (False, f"status {status}")

    return p


async def test_a_changed_ui_is_recovered_by_clicks_and_the_run_completes(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", CHANGED_UI)
    script: list[Step] = [
        Act("left_single", Locate("#menu")),
        Act("left_single", Locate("#approve")),
        Continue("read_status", []),
    ]

    async with FakeReparo(script, context=context) as reparo:
        result = await run(
            approval_process(), context=context, reparo=settings(reparo), case_id=1
        )

    assert result.outcome == "completed", result.reason
    assert reparo.replies[1] == {"success": True, "action_type": "left_single"}
    assert reparo.replies[3] == {"success": True, "action_type": "left_single"}
    screenshots = [reparo.replies[0], reparo.replies[2]]
    assert all(
        isinstance(s, bytes) and jpeg_size(s) == (1280, 720) for s in screenshots
    )


async def test_by_default_the_payload_offers_vision_only(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", CHANGED_UI)
    async with FakeReparo([Fail("no idea")], context=context) as reparo:
        _ = await run(
            approval_process(), context=context, reparo=settings(reparo), case_id=1
        )

    assert reparo.payloads[0]["capabilities"] == {
        "modalities": ["vision"],
        "ocr": False,
    }


TABS = """
<a id="open" href="/report" target="_blank">Open the report</a>
<p id="status">pending</p>
"""
REPORT = '<h1 id="report">Report</h1><p id="status">approved</p>'


async def test_the_recovery_follows_a_tab_reparo_opens_and_the_continuation_runs_there(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", TABS)
    _ = site.page("/report", REPORT)
    script: list[Step] = [
        Act("left_single", Locate("#open")),
        Silence(0.5),  # the new tab opens
        Act("hover", Locate("#report")),  # resolved on the new tab
        Observe(),  # FakeReparo checks the new tab's screenshot is at the viewport size
        Continue("read_status", []),
    ]

    async with FakeReparo(script, context=context) as reparo:
        result = await run(
            approval_process(), context=context, reparo=settings(reparo), case_id=1
        )

    # read_status ran on the tab Reparo left active, so the Completion check is met
    assert result.outcome == "completed", result.reason
    assert [urlsplit(p.url).path for p in context.pages] == ["/page", "/report"]
    assert reparo.replies[3] == {"success": True, "action_type": "hover"}


async def test_an_unknown_action_type_gets_a_failure_reply_and_the_recovery_goes_on(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", CHANGED_UI)
    script: list[Step] = [
        Act("teleport", [10, 20]),
        Act("left_single", Locate("#menu")),
        Act("left_single", Locate("#approve")),
        Continue("read_status", []),
    ]

    async with FakeReparo(script, context=context) as reparo:
        result = await run(
            approval_process(), context=context, reparo=settings(reparo), case_id=1
        )

    assert result.outcome == "completed", result.reason
    assert reparo.replies[1] == {
        "success": False,
        "action_type": "teleport",
        "error": "Unsupported action_type: teleport",
    }


# --------------------------------------------------------------------------- a scripted fake Modality


class ScriptedModality:
    """A Modality that records what it is asked, on which page, and answers as scripted."""

    def __init__(
        self,
        name: str,
        target_kinds: Iterable[str] = (),
        *,
        observation: bytes | Mapping[str, object] | None = None,
        act_delay: float = 0.0,
        act_raises: Exception | None = None,
    ) -> None:
        self._name: str = name
        self._target_kinds: frozenset[str] = frozenset(target_kinds)
        self._observation: bytes | Mapping[str, object] = (
            {"tree": name} if observation is None else observation
        )
        self._act_delay: float = act_delay
        self._act_raises: Exception | None = act_raises
        self.calls: list[tuple[str, str, object]] = []
        """(`observe` or `act`, the page's URL path, the request or action)."""

    @property
    def name(self) -> str:
        return self._name

    @property
    def target_kinds(self) -> frozenset[str]:
        return self._target_kinds

    async def observe(
        self, page: Page, request: Mapping[str, object]
    ) -> bytes | Mapping[str, object]:
        self.calls.append(("observe", urlsplit(page.url).path, dict(request)))
        return self._observation

    async def act(
        self, page: Page, action: Mapping[str, object]
    ) -> Mapping[str, object]:
        self.calls.append(("act", urlsplit(page.url).path, dict(action)))
        await asyncio.sleep(self._act_delay)
        if self._act_raises is not None:
            raise self._act_raises
        return {"success": True, "action_type": action["action_type"], "by": self._name}


def action(action_type: str, **inputs: object) -> dict[str, object]:
    return {
        "type": "action",
        "content": {"action_type": action_type, "action_inputs": inputs},
    }


SETTLE = Silence(0.5)
"""Lets the robot's replies to blind `SendRaw` requests arrive before the script ends."""


async def recover_with(
    context: BrowserContext,
    site: FixtureServer,
    script: list[Step],
    modalities: Sequence[Modality],
) -> tuple[RunResult, FakeReparo]:
    _ = site.page("/page", CHANGED_UI)
    async with FakeReparo(script, context=context) as reparo:
        result = await run(
            approval_process(),
            context=context,
            reparo=settings(reparo),
            case_id=1,
            modalities=modalities,
        )
    return result, reparo


async def test_requests_are_routed_by_observation_modality_and_target_kind(
    context: BrowserContext, site: FixtureServer
) -> None:
    vision = ScriptedModality("vision", frozenset({"coordinates"}), observation=b"jpeg")
    a11y = ScriptedModality("a11y", frozenset({"a11y_selector"}))
    script: list[Step] = [
        SendRaw({"type": "observe", "modality": "a11y"}),
        SendRaw({"type": "screenshot", "content": ""}),
        SendRaw(action("left_single", target={"by": "a11y_selector", "selector": "x"})),
        SendRaw(action("left_single", start_box=[10, 20])),
        SendRaw(action("type", content="hi")),  # no target: the most preferred Modality
        SETTLE,
        Continue("read_status", []),
    ]

    _, reparo = await recover_with(context, site, script, (a11y, vision))

    assert reparo.replies == [
        {"tree": "a11y"},
        b"jpeg",
        {"success": True, "action_type": "left_single", "by": "a11y"},
        {"success": True, "action_type": "left_single", "by": "vision"},
        {"success": True, "action_type": "type", "by": "a11y"},
    ]
    assert [c[0] for c in a11y.calls] == ["observe", "act", "act"]
    assert [c[0] for c in vision.calls] == ["observe", "act"]


async def test_the_payload_carries_the_loaded_modalities_in_preference_order(
    context: BrowserContext, site: FixtureServer
) -> None:
    modalities = (ScriptedModality("a11y"), ScriptedModality("vision"))

    _, reparo = await recover_with(context, site, [Fail("no idea")], modalities)

    assert reparo.payloads[0]["capabilities"] == {
        "modalities": ["a11y", "vision"],
        "ocr": False,
    }


async def test_requests_the_bridge_cannot_serve_get_failure_replies_and_a_warning(
    context: BrowserContext, site: FixtureServer, caplog: pytest.LogCaptureFixture
) -> None:
    a11y = ScriptedModality("a11y", frozenset({"a11y_selector"}))
    script: list[Step] = [
        SendRaw(action("left_single", start_box=[10, 20])),
        SendRaw(action("left_single", target={"by": "text", "text": "Approve"})),
        SendRaw({"type": "observe", "modality": "dom"}),
        SendRaw({"type": "telepathy"}),
        SendRaw("not json"),
        SendRaw({"type": "action", "content": "click it"}),
        SETTLE,
        Continue("read_status", []),
    ]

    with caplog.at_level(logging.WARNING, logger="reparo_playwright_bridge.recovery"):
        _, reparo = await recover_with(context, site, script, (a11y,))

    assert reparo.replies == [
        {
            "success": False,
            "action_type": "left_single",
            "error": "coordinates targeting not supported by this robot",
        },
        {
            "success": False,
            "action_type": "left_single",
            "error": "text targeting not supported by this robot",
        },
        {"success": False, "error": "modality dom not supported"},
        {"success": False, "error": "unsupported message type telepathy"},
        {"success": False, "error": "unsupported message type <unparseable>"},
        {"success": False, "action_type": "unknown", "error": "Invalid action content"},
    ]
    assert a11y.calls == []
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name == "reparo_playwright_bridge.recovery"
        and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 6
    assert "unsupported message type telepathy" in warnings[3]


async def test_a_screenshot_with_no_vision_loaded_is_a_blank_jpeg_at_the_viewport_size(
    context: BrowserContext, site: FixtureServer
) -> None:
    script: list[Step] = [Observe(), Continue("read_status", [])]

    _, reparo = await recover_with(context, site, script, (ScriptedModality("a11y"),))

    blank = reparo.replies[0]
    assert isinstance(blank, bytes)
    page = context.pages[-1]
    data_url = "data:image/jpeg;base64," + base64.b64encode(blank).decode()
    decoded = await page.evaluate(
        """url => new Promise((done, failed) => {
            const img = new Image();
            img.onload = () => done([img.naturalWidth, img.naturalHeight]);
            img.onerror = () => failed(new Error('not a decodable JPEG'));
            img.src = url;
        })""",
        data_url,
    )
    assert decoded == [1280, 720]


async def test_a_modality_that_raises_gets_a_failure_reply(
    context: BrowserContext, site: FixtureServer
) -> None:
    crashing = ScriptedModality(
        "a11y", frozenset({"a11y_selector"}), act_raises=RuntimeError("boom")
    )
    script: list[Step] = [
        SendRaw(action("left_single", target={"by": "a11y_selector"})),
        SETTLE,
        Continue("read_status", []),
    ]

    _, reparo = await recover_with(context, site, script, (crashing,))

    assert reparo.replies == [
        {"success": False, "action_type": "left_single", "error": "boom"}
    ]


async def test_each_reply_is_the_next_frame_and_pings_get_none(
    context: BrowserContext, site: FixtureServer
) -> None:
    slow = ScriptedModality("vision", frozenset({"coordinates"}), act_delay=0.2)
    script: list[Step] = [
        SendRaw(action("hover", start_box=[1, 1])),
        SendRaw({"type": "ping"}),
        SendRaw(action("left_double", start_box=[2, 2])),
        SendRaw({"type": "ping"}),
        SendRaw({"type": "observe", "modality": "vision"}),
        Silence(1.0),
        Continue("read_status", []),
    ]

    _, reparo = await recover_with(context, site, script, (slow,))

    assert reparo.replies == [
        {"success": True, "action_type": "hover", "by": "vision"},
        {"success": True, "action_type": "left_double", "by": "vision"},
        {"tree": "vision"},
    ]


async def test_a_modality_is_given_the_tab_reparo_opened(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", TABS)
    _ = site.page("/report", REPORT)
    a11y = ScriptedModality("a11y")
    script: list[Step] = [
        Act("left_single", Locate("#open")),
        Silence(0.5),  # the new tab opens
        SendRaw({"type": "observe", "modality": "a11y"}),
        SETTLE,
        Continue("read_status", []),
    ]

    async with FakeReparo(script, context=context) as reparo:
        _ = await run(
            approval_process(),
            context=context,
            reparo=settings(reparo),
            case_id=1,
            modalities=(VisionModality(), a11y),
        )

    assert a11y.calls == [("observe", "/report", {"modality": "a11y"})]


# --------------------------------------------------------------------------- run-start checks


@pytest.mark.parametrize(
    ("modalities", "in_reason"),
    [
        ((), "no usable Modality"),
        (
            (ScriptedModality("vision"), ScriptedModality("vision")),
            "duplicate Modality names: vision",
        ),
    ],
    ids=["none", "duplicate names"],
)
async def test_unusable_modalities_end_the_run_local_before_any_event(
    context: BrowserContext,
    site: FixtureServer,
    modalities: Sequence[Modality],
    in_reason: str,
) -> None:
    _ = site.page("/page", CHANGED_UI)
    async with FakeReparo([Fail("unreached")], context=context) as reparo:
        result = await run(
            approval_process(),
            context=context,
            reparo=settings(reparo),
            case_id=1,
            modalities=modalities,
        )

    assert result.outcome == "local"
    assert result.failure is not None
    assert result.failure.type == "local"
    assert result.reason is not None
    assert in_reason in result.reason
    assert reparo.payloads == []
    assert all(p.url == "about:blank" for p in context.pages)


async def test_vision_needs_a_fixed_viewport_or_the_run_ends_local_before_any_event(
    browser: Browser, site: FixtureServer
) -> None:
    _ = site.page("/page", CHANGED_UI)
    context = await browser.new_context(no_viewport=True, base_url=site.url)
    try:
        async with FakeReparo([Fail("unreached")]) as reparo:
            result = await run(
                approval_process(), context=context, reparo=settings(reparo), case_id=1
            )
        pages = [p.url for p in context.pages]
    finally:
        await context.close()

    assert result.outcome == "local"
    assert result.failure is not None
    assert result.failure.type == "local"
    assert result.reason is not None
    assert "fixed viewport" in result.reason
    assert reparo.payloads == []
    assert pages == ["about:blank"]


async def test_without_vision_a_context_without_a_fixed_viewport_is_fine(
    browser: Browser, site: FixtureServer
) -> None:
    _ = site.page("/page", CHANGED_UI)
    context = await browser.new_context(no_viewport=True, base_url=site.url)
    try:
        async with FakeReparo([Fail("no idea")]) as reparo:
            result = await run(
                approval_process(),
                context=context,
                reparo=settings(reparo),
                case_id=1,
                modalities=(ScriptedModality("a11y"),),
            )
    finally:
        await context.close()

    assert result.outcome == "recovery_failed"


# --------------------------------------------------------------------------- FakeReparo's checks


async def test_a_locate_that_matches_nothing_fails_loudly(
    context: BrowserContext, site: FixtureServer
) -> None:
    _ = site.page("/page", CHANGED_UI)
    script: list[Step] = [Act("left_single", Locate("#nowhere")), Fail("unreached")]

    with pytest.raises(FakeReparoError, match="#nowhere"):
        async with FakeReparo(script, context=context) as reparo:
            _ = await run(
                approval_process(), context=context, reparo=settings(reparo), case_id=1
            )


@pytest.mark.parametrize(
    ("observation", "problem"),
    [
        ({"not": "binary"}, "not binary"),
        (b"\x89PNG\r\n", "not a JPEG"),
        ("a small JPEG", "not the viewport's 1280x720"),
    ],
    ids=["json", "png", "wrong size"],
)
async def test_fake_reparo_checks_every_screenshot_reply(
    context: BrowserContext,
    site: FixtureServer,
    observation: bytes | Mapping[str, object] | str,
    problem: str,
) -> None:
    if isinstance(observation, str):
        page = await context.new_page()
        observation = await page.screenshot(
            type="jpeg", clip={"x": 0, "y": 0, "width": 100, "height": 50}
        )
        await page.close()
    liar = ScriptedModality(
        "vision", frozenset({"coordinates"}), observation=observation
    )

    with pytest.raises(FakeReparoError, match=problem):
        _ = await recover_with(
            context, site, [Observe(), Continue("read_status", [])], (liar,)
        )
