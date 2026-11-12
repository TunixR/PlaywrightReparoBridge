# Agent notes

## Issue tracker

GitHub Issues on `TunixR/PlaywrightReparoBridge`, driven with `gh`. Conventions, including the
`/wayfinder` map and ticket operations, are in
`.agents/skills/setup-matt-pocock-skills/issue-tracker-github.md`.

## Domain

- Vocabulary: [`GLOSSARY.md`](GLOSSARY.md). Decisions: [`docs/adr/`](docs/adr/).
- The bridge is a robot-side connector for Reparo. Reparo's backend source is at
  `C:\Users\Antonio Rodriguez\Code\reparo\backend` (recovery contract: `routers/recovery.py`,
  `templates/recovery_payload.py`, `scripts/recovery_ws_client/`); public overview at
  https://reparo.tunixr.com.

## Tooling

`uv sync`, `uv run playwright install chromium` (once; tests drive real headless Chromium),
`uv run pytest`, `uv run ruff check`, `uv run ruff format`, `uv run basedpyright`.
