"""Run one case of the example, under one UI, against FakeReparo or a real Reparo."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from playwright.async_api import Browser, BrowserContext

from examples.spending_approval.app import UI, SpendingApp
from examples.spending_approval.cases import APPROVAL_LIMIT, CASES, CaseName
from examples.spending_approval.process import build_process
from reparo_playwright_bridge import ReparoSettings, RunResult, run

if TYPE_CHECKING:
    from reparo_playwright_bridge.testing import FakeReparo

__all__ = ["ExampleRun", "new_context", "run_case"]

TIMEOUT_MS = 3_000
"""Playwright's default timeout for the example: the app is local, so a missing element shows up fast."""


@dataclass(frozen=True)
class ExampleRun:
    """How one example run ended."""

    result: RunResult
    reparo: FakeReparo | None
    """The FakeReparo the run used, for its records; None against a real Reparo."""


async def new_context(browser: Browser, base_url: str) -> BrowserContext:
    """A context at a fixed 1280×720 viewport (vision needs one), on the app's URL."""
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720},
        base_url=base_url,
    )
    context.set_default_timeout(TIMEOUT_MS)
    return context


async def run_case(
    browser: Browser,
    *,
    ui: UI,
    case: CaseName,
    reparo: ReparoSettings | None = None,
) -> ExampleRun:
    """Run `case` under `ui` on a fresh app and context, closing both afterwards.

    Without `reparo` settings it runs against a FakeReparo playing the case's Recovery script.
    """
    seeded = CASES[case]
    process = build_process(seeded.expected_budget)
    variables = {"approval_limit": APPROVAL_LIMIT}
    with SpendingApp(ui=ui, rows=seeded.rows, budget=seeded.budget) as app:
        context = await new_context(browser, app.url)
        try:
            if reparo is not None:
                result = await run(
                    process,
                    context=context,
                    reparo=reparo,
                    case_id=case,
                    variables=variables,
                )
                return ExampleRun(result, None)

            # Imported here: FakeReparo needs the [fake] extra, which real mode doesn't.
            from examples.spending_approval._scripts import recovery_script
            from reparo_playwright_bridge.testing import FakeReparo

            async with FakeReparo(recovery_script(seeded), context=context) as fake:
                result = await run(
                    process,
                    context=context,
                    reparo=ReparoSettings(base_url=fake.url, robot_key=fake.robot_key),
                    case_id=case,
                    variables=variables,
                )
            return ExampleRun(result, fake)
        finally:
            await context.close()
