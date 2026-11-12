# Reparo Playwright Bridge

Playwright connector for [Reparo](https://reparo.tunixr.com): run a browser process defined as
activities, send failures to Reparo's recovery WebSocket, and continue the process from where
Reparo hands it back.

Status: planning. See the map issue
[Playwright bridge for Reparo: spec](https://github.com/TunixR/PlaywrightReparoBridge/issues/1).

## Development

```bash
uv sync
uv run playwright install chromium  # once: tests use real headless Chromium
uv run pytest
uv run ruff check
uv run basedpyright
```
