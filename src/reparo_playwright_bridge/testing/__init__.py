"""Test doubles for Reparo: `FakeReparo` plays hand-written Recovery scripts on localhost.

Needs the `[fake]` extra: `pip install "reparo-playwright-bridge[fake]"`.
"""

try:
    import aiohttp as _aiohttp  # noqa: F401  # pyright: ignore[reportUnusedImport]
except ImportError as exc:  # pragma: no cover - exercised in a subprocess
    raise ImportError(
        "reparo_playwright_bridge.testing needs the [fake] extra: "
        + 'pip install "reparo-playwright-bridge[fake]"'
    ) from exc

from reparo_playwright_bridge.testing._server import FakeReparo, Report
from reparo_playwright_bridge.testing._steps import (
    Close,
    Continue,
    Decline,
    SendError,
    SendRaw,
    Silence,
    Step,
)

__all__ = [
    "Close",
    "Continue",
    "Decline",
    "FakeReparo",
    "Report",
    "SendError",
    "SendRaw",
    "Silence",
    "Step",
]
