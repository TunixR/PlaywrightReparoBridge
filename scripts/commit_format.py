"""The Bridge's commit format, `type(scope)!: what`, for commit messages and PR titles.

Subcommands:
    check-commit <file>   the commit-msg hook (`.githooks/commit-msg`): check a commit message.
    check-title <title>   CI: check a PR title, which becomes the squash commit.

The allowed scopes are the list under `## Commits` in AGENTS.md, read from there on every run.
Standard library only, so the hook and CI run it with any Python 3.10+, without `uv`.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path

AGENTS_MD = Path(__file__).resolve().parents[1] / "AGENTS.md"

TYPES = (
    "feat",
    "fix",
    "refactor",
    "perf",
    "test",
    "docs",
    "build",
    "ci",
    "chore",
    "revert",
)

MAX_HEADER_LENGTH = 100
"""A hard limit; aim for 72."""

HEADER_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\((?P<scope>[^()]*)\))?(?P<breaking>!)?: (?P<what>.*)$"
)
# A scope line of AGENTS.md's `## Commits` section: "- `scope`: what it covers".
SCOPE_LINE_RE = re.compile(r"^- `(?P<scope>[a-z][a-z0-9-]*)`: ")
# Git-generated messages the commit-msg hook lets through.
GIT_GENERATED_RE = re.compile(r'^(Merge |Revert "|fixup! |squash! |amend! )')
# `Added ...` is rejected; an acronym or identifier (`BPMN`, `FakeReparo`) may start the summary.
CAPITALISED_WORD_RE = re.compile(r"[A-Z][a-z]+(?![A-Za-z0-9])")


def check_header(header: str, scopes: Sequence[str]) -> list[str]:
    """Problems with a commit header or PR title; empty when it is well formed."""
    match = HEADER_RE.match(header)
    if match is None:
        return [
            f"{header!r} is not `type(scope)!: what` "
            + "(scope and `!` optional, one space after the colon)"
        ]
    problems: list[str] = []
    kind, scope, what = match["type"], match["scope"], match["what"]
    if kind not in TYPES:
        problems.append(f"unknown type {kind!r}; use one of: {', '.join(TYPES)}")
    if scope is not None and scope not in scopes:
        problems.append(
            f"unknown scope {scope!r}; use one of: {', '.join(scopes)}, "
            + "or no scope for a cross-cutting change"
        )
    if not what.strip():
        problems.append("empty summary")
    elif CAPITALISED_WORD_RE.match(what):
        problems.append("summary should start lowercase (`add ...`, not `Added ...`)")
    if what.endswith("."):
        problems.append("summary should not end with a period")
    if len(header) > MAX_HEADER_LENGTH:
        problems.append(
            f"header is {len(header)} characters; keep it under 72 (hard limit {MAX_HEADER_LENGTH})"
        )
    return problems


def check_commit_message(message: str, scopes: Sequence[str]) -> list[str]:
    """Problems with a commit message: its header, and the blank line after it."""
    lines = [line for line in message.splitlines() if not line.startswith("#")]
    header = next((line for line in lines if line.strip()), "")
    if GIT_GENERATED_RE.match(header):
        return []
    problems = check_header(header, scopes)
    body_start = lines.index(header) + 1
    if len(lines) > body_start and lines[body_start].strip():
        problems.append("leave a blank line between the header and the body")
    return problems


def load_scopes(agents_md: Path = AGENTS_MD) -> list[str]:
    """The scopes listed under `## Commits` in AGENTS.md; none found is an error, not "any"."""
    scopes: list[str] = []
    in_commits = False
    for line in agents_md.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_commits = line.strip() == "## Commits"
        elif in_commits and (match := SCOPE_LINE_RE.match(line)):
            scopes.append(match["scope"])
    if not scopes:
        raise SystemExit(f"no scopes found under `## Commits` in {agents_md}")
    return scopes


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("check-commit", "check-title"):
        print(__doc__, file=sys.stderr)
        return 2
    command, arg = argv
    scopes = load_scopes()
    if command == "check-commit":
        problems = check_commit_message(Path(arg).read_text(encoding="utf-8"), scopes)
        what = "commit message"
    else:
        problems = check_header(arg.strip(), scopes)
        what = "PR title"
    for problem in problems:
        print(f"{what}: {problem}", file=sys.stderr)
    if problems:
        print("See AGENTS.md -> Commits.", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
