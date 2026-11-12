"""Reparo Playwright Bridge: run Playwright Processes and hand UI failures to Reparo."""

from reparo_playwright_bridge import (
    _log as _log,  # installs the logger tree's NullHandler
)
from reparo_playwright_bridge._context import CheckContext, EventContext
from reparo_playwright_bridge._dom import DomModality
from reparo_playwright_bridge._modality import Modality
from reparo_playwright_bridge._process import Process
from reparo_playwright_bridge._result import Failure, RunResult
from reparo_playwright_bridge._run import run
from reparo_playwright_bridge._settings import ReparoSettings
from reparo_playwright_bridge._vision import VisionModality

__all__ = [
    "CheckContext",
    "DomModality",
    "EventContext",
    "Failure",
    "Modality",
    "Process",
    "ReparoSettings",
    "RunResult",
    "VisionModality",
    "run",
]
