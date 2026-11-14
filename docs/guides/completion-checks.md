# Atomic Completion checks

A **Completion check** states what the robot can believe about its own completion: once the
Process reaches its end, with or without a Recovery, the check looks at the page and the final
Variables and answers **met** or **not met, because …**. Vocabulary is in
[GLOSSARY.md](../../GLOSSARY.md).

Write each check so it always gives that answer, whatever it meets:

- **Atomic.** One check verifies one fact, and gives one reason when it isn't met.
- **Total.** Every way of failing to read that fact becomes `(False, reason)`. The check never
  raises.

## Why: what a check's answer does to the run

```python
@p.completion_check("the remaining budget is as expected")
async def budget_matches(ctx: CheckContext) -> tuple[bool, str | None]: ...
```

All checks run in declaration order, even after one is unmet, and the run's outcome follows from
what they return:

| The check… | The run |
|---|---|
| returns `(True, None)` | met; if every check is met, `completed` |
| returns `(False, "reason")` | `checks_failed`; `reason` is joined into `RunResult.reason`, e.g. `1 completion check not met: budget_matches: expected 50, got 250` |
| raises, or returns anything else (including `(False, "")`: a reason is required) | **the check's own code crashed**: the remaining checks don't run, and the run is `local` (or `continuation_failed` after a Recovery), as a Process bug |

`checks_failed` says what you want it to say: the Process ran to its end, and here is the state
that isn't right. A crash says something else: your check has a bug. Worse, after a Recovery a
crash turns a run Reparo recovered into `continuation_failed`, and it is reported to Reparo as a
failed Recovery when what failed was the check.

Only an Environment failure (the browser closed, the page crashed) keeps its class when a check
raises; a missing element, a timeout or a failed `expect` is a crash of your check, reported as a
Process bug.

## Turning every read error into `(False, reason)`

A check reads the page or the Variables, and every read can fail: the element is missing, the text
isn't a number, the Variable was never written. Catch those failures where they happen and give
them a reason:

```python
from playwright.async_api import Error as PlaywrightError


@p.completion_check("the remaining budget is as expected")
async def budget_matches(ctx: CheckContext) -> tuple[bool, str | None]:
    try:
        text = await ctx.page.locator("#remaining-budget").inner_text(timeout=2_000)
    except PlaywrightError as exc:  # missing element, timeout
        return False, f"the remaining budget isn't shown ({type(exc).__name__})"
    try:
        remaining = int(text)
    except ValueError:
        return False, f"the remaining budget isn't a number: {text!r}"
    if remaining != 50:
        return False, f"expected 50, got {remaining}"
    return True, None
```

The same for Variables:

```python
@p.completion_check("every request was decided")
async def all_decided(ctx: CheckContext) -> tuple[bool, str | None]:
    pending = ctx.vars.get("pending")  # .get, never ctx.vars["pending"]
    if not isinstance(pending, list):
        return False, "the pending requests were never read"
    ...
```

Things to avoid:

- **`assert`**, which raises `AssertionError`: a plain `assert` is a Process bug, and a failed
  Playwright `expect(...)` is a crash of the check. Use `if` and return the reason; if you like
  `expect`'s waiting, wrap it in `try` / `except AssertionError`.
- **`ctx.vars[...]`** for a Variable that may be absent; use `.get` and check the type.
- **Default timeouts**, which can make a check wait 30 s on an element that isn't coming. Pass a
  short `timeout=`.
- **Writing to `ctx.vars`**: it is read-only, and a write is a Process bug.
- **One check for several facts.** Split them: each fact gets its own grounded name and its own
  reason, and all of them still run when one is unmet.

## What a check gets

- `ctx.page` is the active page, **not recording**: nothing a check does reaches the UI log, so a
  check may navigate (`goto`), and the next check sees the page it left.
- `ctx.vars` is a read-only view of the final Variables, including any Reparo returned.
- There is no `ctx.fail`: a check reports by returning.

## Where the expected value comes from

Close over expectations rather than putting them in Variables: Variables are sent to Reparo, and a
check is about what the robot can believe, not something Reparo should aim for. The example builds
its check through `build_process(expected_budget)` for that reason.

Prefer reading the fact off the page in the check, or in a read Event right before the end (see
[Idempotent reads and deliberate Variable writes](reads-and-variables.md)), over trusting a
Variable the Process computed: after a Recovery, the Events that would have computed it may never
have run.
