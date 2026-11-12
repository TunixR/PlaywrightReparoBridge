from collections.abc import AsyncIterator, Iterator

import pytest
from playwright.async_api import Browser, BrowserContext, async_playwright

from reparo_playwright_bridge import ReparoSettings
from tests.fixture_server import FixtureServer


@pytest.fixture
def site() -> Iterator[FixtureServer]:
    server = FixtureServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
async def browser() -> AsyncIterator[Browser]:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        yield browser
        await browser.close()


@pytest.fixture
async def context(
    browser: Browser, site: FixtureServer
) -> AsyncIterator[BrowserContext]:
    context = await browser.new_context(
        viewport={"width": 1280, "height": 720}, base_url=site.url
    )
    yield context
    await context.close()


@pytest.fixture
def reparo() -> ReparoSettings:
    return ReparoSettings(base_url="http://127.0.0.1:1", robot_key="test-robot-key")
