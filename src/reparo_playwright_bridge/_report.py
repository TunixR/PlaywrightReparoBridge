"""The report client: tells Reparo once, at the end of the run, whether its Recovery worked."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Mapping

from reparo_playwright_bridge._log import report_log
from reparo_playwright_bridge._protocol import ROBOT_KEY_HEADER
from reparo_playwright_bridge._settings import ReparoSettings

REPORT_TIMEOUT_SECONDS = 10.0


def _post(url: str, key: str, body: bytes) -> int:
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={ROBOT_KEY_HEADER: key, "Content-Type": "application/json"},
    )
    # The URL is derived from an http(s) base_url.
    with urllib.request.urlopen(request, timeout=REPORT_TIMEOUT_SECONDS) as response:  # pyright: ignore[reportAny]
        return int(response.status)  # pyright: ignore[reportAny]


async def report_result(
    settings: ReparoSettings,
    recovery_id: str,
    success: bool,
    extra: Mapping[str, object],
) -> None:
    """`POST report_result` once. A failed send only logs a WARNING; it is never retried."""
    body = json.dumps({"success": success}).encode()
    try:
        status = await asyncio.to_thread(
            _post,
            settings.report_result_url(recovery_id),
            settings.robot_key.get_secret_value(),
            body,
        )
    except (OSError, urllib.error.URLError) as exc:
        detail = (
            f"HTTP {exc.code}"
            if isinstance(exc, urllib.error.HTTPError)
            else type(exc).__name__
        )
        report_log.warning(
            "report_result for Recovery %s failed (%s)",
            recovery_id,
            detail,
            extra=dict(extra),
        )
        return
    report_log.info(
        "report_result sent for Recovery %s: success %s (HTTP %d)",
        recovery_id,
        str(success).lower(),
        status,
        extra=dict(extra),
    )
