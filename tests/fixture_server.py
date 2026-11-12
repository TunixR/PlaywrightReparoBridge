"""A small localhost HTML server for tests: register pages by path, then point a browser at it."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import override


class FixtureServer:
    """Serves registered HTML pages on a free localhost port, on a background thread."""

    def __init__(self) -> None:
        self._pages: dict[str, str] = {}
        pages = self._pages

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                body = pages.get(path)
                if body is None:
                    self.send_error(404)
                    return
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                _ = self.wfile.write(data)

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server: ThreadingHTTPServer = ThreadingHTTPServer(
            ("127.0.0.1", 0), Handler
        )
        self._thread: threading.Thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def page(self, path: str, html: str) -> str:
        """Register `html` at `path` and return its absolute URL."""
        self._pages[path] = html
        return self.url + path

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
