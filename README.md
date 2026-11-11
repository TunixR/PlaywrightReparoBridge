# Reparo Playwright Bridge

Playwright connector for [Reparo](https://reparo.tunixr.com): run a browser process defined as
activities, send failures to Reparo's recovery WebSocket, and continue the process from where
Reparo hands it back.

Status: planning. See `.scratch/playwright-bridge/map.md`.

## Development

```bash
uv sync
uv run pytest
uv run ruff check
```
