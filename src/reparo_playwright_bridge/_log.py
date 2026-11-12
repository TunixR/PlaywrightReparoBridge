"""The `reparo_playwright_bridge` logger tree. The library adds only a NullHandler."""

import logging

ROOT = logging.getLogger("reparo_playwright_bridge")
ROOT.addHandler(logging.NullHandler())

run_log = ROOT.getChild("run")
recovery_log = ROOT.getChild("recovery")
checks_log = ROOT.getChild("checks")
report_log = ROOT.getChild("report")
testing_log = ROOT.getChild("testing")
