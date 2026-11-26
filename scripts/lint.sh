#!/bin/sh
# The static checks, all of them: the pre-commit hook and CI (.github/workflows/lint.yml) run this.
# Each tool is a dev dependency (uv.lock sets its version) and is configured in pyproject.toml,
# except jscpd (.jscpd.json, .jscpd-tests.json).
cd "$(git rev-parse --show-toplevel)" || exit 1

failed=""
# Prints a check's output only when it fails.
run() {
    name=$1
    shift
    if output=$("$@" 2>&1); then
        echo "ok    $name"
    else
        echo "FAIL  $name"
        echo "$output"
        failed="$failed, $name"
    fi
}

run "ruff (lint)" uv run ruff check
run "ruff (format)" uv run ruff format --check
run "basedpyright (types)" uv run basedpyright
run "import-linter (which module imports which)" uv run lint-imports
run "deptry (imports match declared dependencies)" uv run deptry .
run "vulture (unused code)" uv run vulture
run "complexipy (cognitive complexity)" uv run complexipy
run "jscpd (duplicated code)" uv run jscpd --no-colors
# Tests repeat their setup more than code should, so only longer blocks count.
run "jscpd (duplicated code in tests)" uv run jscpd --no-colors --config .jscpd-tests.json

if [ -n "$failed" ]; then
    echo "Failed: ${failed#, }" >&2
    exit 1
fi
