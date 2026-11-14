"""Run the spending-approval example: `uv run python -m examples.spending_approval --help`.

It logs to stdout; the outcome line is its result. Exit code 0 means `completed`, 1 any other
outcome, and 2 a usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import cast

from playwright.async_api import async_playwright
from pydantic import ValidationError

from examples.spending_approval.app import UI
from examples.spending_approval.cases import CaseName
from examples.spending_approval.example import run_case
from reparo_playwright_bridge import ReparoSettings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m examples.spending_approval",
        description="Approve or reject spending requests in a synthetic web app, with Reparo"
        + " recovering the run when the UI changes.",
    )
    _ = parser.add_argument(
        "--ui",
        choices=["original", "changed"],
        required=True,
        help="the app's UI; `changed` moves Approve into an 'Actions' menu",
    )
    _ = parser.add_argument(
        "--reparo",
        choices=["fake", "real"],
        required=True,
        help="FakeReparo on localhost, or a real Reparo at --reparo-base-url",
    )
    _ = parser.add_argument(
        "--case", choices=["single", "full"], default="single", help="default: single"
    )
    _ = parser.add_argument(
        "--headed", action="store_true", help="show the browser window"
    )
    _ = parser.add_argument(
        "-v", "--verbose", action="store_true", help="log at DEBUG instead of INFO"
    )
    _ = parser.add_argument(
        "--reparo-base-url",
        metavar="URL",
        help="the real Reparo's base URL (real mode only; the robot key comes from"
        + " REPARO_ROBOT_KEY)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    reparo: ReparoSettings | None = None
    if cast("str", args.reparo) == "real":
        base_url = cast("str | None", args.reparo_base_url)
        if not base_url:
            parser.error("--reparo real needs --reparo-base-url")
        try:
            reparo = ReparoSettings(base_url=base_url)  # the key from REPARO_ROBOT_KEY
        except ValidationError as exc:
            parser.error(f"invalid Reparo settings: {exc.errors()[0]['msg']}")

    logging.basicConfig(
        stream=sys.stdout,
        level=logging.DEBUG if cast("bool", args.verbose) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # FakeReparo's server would log every request at INFO.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    return asyncio.run(
        _run(
            ui=cast("UI", args.ui),
            case=cast("CaseName", args.case),
            headed=cast("bool", args.headed),
            reparo=reparo,
        )
    )


async def _run(
    *, ui: UI, case: CaseName, headed: bool, reparo: ReparoSettings | None
) -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        try:
            example = await run_case(browser, ui=ui, case=case, reparo=reparo)
        finally:
            await browser.close()
    return 0 if example.result.outcome == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
