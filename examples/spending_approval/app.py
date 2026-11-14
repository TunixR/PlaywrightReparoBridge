"""The synthetic spending-approval web app: a stdlib HTTP server with fresh in-memory state.

`/requests` lists the requests by priority, `/requests/<id>` shows one with its decision
controls, and every page's header shows the remaining budget. Approving a request takes its
amount off the budget. Reasons for a rejection come from a custom dropdown (a button and an
option list), not a native `<select>`, whose popup headless screenshots don't show.

The `changed` UI differs only in one place: Approve sits inside an "Actions ▾" menu on the
detail page instead of being a button of its own.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Literal, Self, override
from urllib.parse import parse_qs

from examples.spending_approval.cases import BUDGET_REASON, LIMIT_REASON, Row

type UI = Literal["original", "changed"]

REASONS = (LIMIT_REASON, BUDGET_REASON)

_DETAIL = re.compile(r"^/requests/(\d+)$")
_DECISION = re.compile(r"^/requests/(\d+)/(approve|reject)$")

_STYLE = """
body { font-family: system-ui, sans-serif; margin: 0; color: #1d2433; }
header { display: flex; justify-content: space-between; padding: 12px 24px;
         background: #1d2433; color: #fff; }
main { padding: 16px 24px; }
table { border-collapse: collapse; }
th, td { padding: 6px 14px; border-bottom: 1px solid #d5d9e0; text-align: left; }
dl { display: grid; grid-template-columns: max-content auto; gap: 6px 16px; }
dt { font-weight: 600; }
button { font: inherit; padding: 6px 14px; cursor: pointer; }
.decision { display: flex; gap: 32px; align-items: flex-start; margin-top: 16px; }
.popup { position: relative; display: inline-block; }
[role=menu], [role=listbox] { position: absolute; z-index: 1; margin: 2px 0 0; padding: 4px 0;
  list-style: none; background: #fff; border: 1px solid #aab1bd; min-width: 100%; }
[role=menu] button { border: 0; background: none; width: 100%; text-align: left; }
[role=option] { padding: 6px 14px; white-space: nowrap; cursor: pointer; }
[role=option]:hover, [role=menu] button:hover { background: #e8ecf3; }
[hidden] { display: none !important; }
"""

_SCRIPT = """
for (const toggle of document.querySelectorAll('[aria-haspopup]')) {
  const popup = document.getElementById(toggle.getAttribute('aria-controls'));
  toggle.addEventListener('click', () => {
    popup.hidden = !popup.hidden;
    toggle.setAttribute('aria-expanded', String(!popup.hidden));
  });
}
for (const option of document.querySelectorAll('[role=option]')) {
  option.addEventListener('click', () => {
    const reason = option.textContent.trim();
    document.getElementById('reason-value').value = reason;
    const toggle = document.getElementById('reason-button');
    toggle.textContent = 'Reason: ' + reason + ' \\u25be';
    toggle.setAttribute('aria-expanded', 'false');
    option.closest('[role=listbox]').hidden = true;
  });
}
"""


@dataclass
class _Request:
    request_id: int
    priority: int
    requester: str
    amount: int
    status: str = "Pending"
    reason: str | None = None


class SpendingApp:
    """The app on a free localhost port, on a background thread; `with SpendingApp(...) as app:`."""

    def __init__(self, *, ui: UI, rows: tuple[Row, ...], budget: int) -> None:
        self.ui: UI = ui
        self.budget: int = budget
        self.requests: dict[int, _Request] = {
            row.request_id: _Request(
                row.request_id, row.priority, row.requester, row.amount
            )
            for row in rows
        }
        self._lock: threading.Lock = threading.Lock()
        self._server: ThreadingHTTPServer = ThreadingHTTPServer(
            ("127.0.0.1", 0), self._handler()
        )
        self._thread: threading.Thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()

    # --- pages ---------------------------------------------------------------------------

    def _layout(self, title: str, body: str) -> str:
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{escape(title)} · Spending approvals</title>
<style>{_STYLE}</style></head>
<body>
<header><strong>Spending approvals</strong>
<span>Remaining budget: <span id="remaining-budget">{self.budget}</span></span></header>
<main>{body}</main>
<script>{_SCRIPT}</script>
</body></html>"""

    def _list_page(self) -> str:
        rows = "".join(
            f"""<tr data-status="{r.status.lower()}">
<td>{r.priority}</td><td class="request-id">{r.request_id}</td>
<td>{escape(r.requester)}</td><td>{r.amount}</td><td>{r.status}</td>
<td><a href="/requests/{r.request_id}" aria-label="Open request {r.request_id}">Open</a></td></tr>"""
            for r in sorted(self.requests.values(), key=lambda r: r.priority)
        )
        return self._layout(
            "Spending requests",
            f"""<h1>Spending requests</h1>
<table><thead><tr><th>Priority</th><th>Request</th><th>Requester</th><th>Amount</th>
<th>Status</th><th></th></tr></thead><tbody>{rows}</tbody></table>""",
        )

    def _detail_page(self, request: _Request) -> str:
        reason = (
            f'<dt>Reason</dt><dd id="reason">{escape(request.reason)}</dd>'
            if request.reason
            else ""
        )
        decision = self._decision(request) if request.status == "Pending" else ""
        return self._layout(
            f"Request {request.request_id}",
            f"""<p><a href="/requests">Back to requests</a></p>
<h1>Request {request.request_id}</h1>
<dl><dt>Requester</dt><dd id="requester">{escape(request.requester)}</dd>
<dt>Amount</dt><dd id="amount">{request.amount}</dd>
<dt>Status</dt><dd id="status">{request.status}</dd>{reason}</dl>
{decision}""",
        )

    def _decision(self, request: _Request) -> str:
        approve_action = f"/requests/{request.request_id}/approve"
        if self.ui == "original":
            approve = f"""<form method="post" action="{approve_action}">
<button type="submit">Approve</button></form>"""
        else:
            approve = f"""<div class="popup">
<button type="button" id="actions-button" aria-haspopup="menu" aria-expanded="false"
  aria-controls="actions-menu">Actions ▾</button>
<div id="actions-menu" role="menu" hidden>
<form method="post" action="{approve_action}">
<button type="submit" role="menuitem">Approve</button></form></div></div>"""
        options = "".join(
            f'<li role="option" aria-selected="false">{escape(r)}</li>' for r in REASONS
        )
        reject = f"""<form method="post" action="/requests/{request.request_id}/reject">
<input type="hidden" name="reason" id="reason-value">
<div class="popup">
<button type="button" id="reason-button" aria-haspopup="listbox" aria-expanded="false"
  aria-controls="reason-options">Reason: choose ▾</button>
<ul id="reason-options" role="listbox" aria-label="Reasons" hidden>{options}</ul>
</div>
<button type="submit">Reject</button></form>"""
        return f'<div class="decision">{approve}{reject}</div>'

    # --- decisions -------------------------------------------------------------------------

    def _decide(self, request: _Request, decision: str, reason: str | None) -> bool:
        """Apply a decision to a pending request; False if it can't be applied."""
        if request.status != "Pending":
            return False
        if decision == "approve":
            request.status = "Approved"
            self.budget -= request.amount
            return True
        if reason not in REASONS:
            return False
        request.status = "Rejected"
        request.reason = reason
        return True

    # --- HTTP ------------------------------------------------------------------------------

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        app = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                with app._lock:
                    if path in ("/", "/requests"):
                        if path == "/":
                            self._redirect("/requests")
                            return
                        self._html(200, app._list_page())
                        return
                    match = _DETAIL.match(path)
                    request = match and app.requests.get(int(match[1]))
                    if not request:
                        self._not_found()
                        return
                    self._html(200, app._detail_page(request))

            def do_POST(self) -> None:
                match = _DECISION.match(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(length).decode())
                with app._lock:
                    request = match and app.requests.get(int(match[1]))
                    if not match or not request:
                        self._not_found()
                        return
                    reason = form.get("reason", [None])[0]
                    if not app._decide(request, match[2], reason):
                        self._html(
                            400,
                            app._layout(
                                "Not allowed",
                                f"""<h1>That decision is not allowed</h1>
<p><a href="/requests/{request.request_id}">Back to the request</a></p>""",
                            ),
                        )
                        return
                self._redirect(f"/requests/{request.request_id}")

            def _html(self, status: int, body: str) -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                _ = self.wfile.write(data)

            def _not_found(self) -> None:
                self._html(404, app._layout("Not found", "<h1>Not found</h1>"))

            def _redirect(self, location: str) -> None:
                self.send_response(303)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        return Handler
