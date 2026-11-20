# Agent notes

The Playwright Bridge: the Bridge that makes Playwright Robots speak Reparo's robot contract.
Shared language, cross-repo decisions, skills and cross-repo work live in the private home repo,
[ReparoHome](https://github.com/TunixR/ReparoHome), cloned next to this one as `../ReparoHome`.

## Issue tracker

GitHub Issues on `TunixR/PlaywrightReparoBridge`, driven with `gh`. Maps, decision tickets and
specs live in home; only this repo's implementation tickets live here. Home's
`docs/agents/issue-tracker.md` says where each kind of issue goes and how children link across
repos. This repo has no labels of its own: don't apply triage roles (`ready-for-agent` and the
rest) or `wayfinder:*` here. A ticket is ready when it is open, unblocked and unassigned on its
map's frontier.

## Domain

- Vocabulary: home's `GLOSSARY.md` binds this repo; [`GLOSSARY.md`](GLOSSARY.md) here adds only
  Playwright terms and never contradicts it.
- Decisions: [`docs/adr/`](docs/adr/) for decisions that bind only this repo; home's `docs/adr/`
  for those that bind more than one.
- The robot contract: the backend's `ARCHITECTURE.md` "Robot protocol"
  (https://github.com/TunixR/Reparo, target branch `develop`; wire code in `routers/recovery.py`
  and `templates/recovery_payload.py`; its Reference client in `scripts/recovery_ws_client/`).
  Public overview at https://reparo.tunixr.com.

## Skills

`.agents/skills` is a submodule of home's generated `skills` branch, and `.claude/skills` links to
it. Edit skills only in home; this repo picks them up when it merges a bump PR. Clone with
`--recurse-submodules`, or fill it later with `git submodule update --init`. Without access to
home it stays empty, and nothing here depends on it.

## Commits

Commit messages and PR titles are `type(scope)!: what`: `scripts/commit_format.py` checks them
in the `commit-msg` hook (enable it once per clone with `git config core.hooksPath .githooks`) and
in the PR-title CI job (`.github/workflows/pr-title.yml`). Types: `feat`, `fix`, `refactor`,
`perf`, `test`, `docs`, `build`, `ci`, `chore`, `revert`. `!` marks a breaking change. The
summary is imperative and lowercase, with no final period. The scope is optional; leave it out for
a cross-cutting change. Example: `docs(glossary): keep only Playwright terms`.

The scopes below are the whole list; the checker reads it from this section.

- `process`: authoring a Process: `_process.py`, `_definition.py`, `_context.py`
- `run`: the engine and what an Event records: `_run.py`, `_recording.py`, `_logged_calls.py`,
  `_state_reads.py`, `_classify.py`, `_checks.py`, `_continuation.py`, `_result.py`
- `recovery`: the Recovery and the wire: `_recovery.py`, `_protocol.py`, `_payload.py`,
  `_report.py`, `_modality.py`, `_vision.py`, `_dom.py`, `_jpeg.py`, `_keys.py`
- `model`: the Process model export: `_model_export.py`
- `settings`: `ReparoSettings`: `_settings.py`
- `logging`: the logger tree and log safety: `_log.py`
- `testing`: FakeReparo: `testing/`
- `examples`: `examples/`
- `guides`: `docs/guides/`
- `glossary`: `GLOSSARY.md`
- `adr`: `docs/adr/`
- `agents`: `AGENTS.md`, `CLAUDE.md`
- `skills`: the skills submodule and its link
- `ci`: `.github/`, `.githooks/`, `scripts/`
- `deps`: dependency bumps: `pyproject.toml`, `uv.lock`

## Tooling

`uv sync`, `uv run playwright install chromium` (once; tests drive real headless Chromium; on a
clean Linux box first `apt-get install -y libatomic1`, then use `install --with-deps chromium`),
`uv run pytest`, `uv run ruff check`, `uv run ruff format`, `uv run basedpyright`.
