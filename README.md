# Reparo Playwright Bridge

The Playwright Bridge for [Reparo](https://reparo.tunixr.com). You describe a browser automation
as a **Process** of Events grouped into Activities, and `run()` runs it on your Playwright
`BrowserContext`. When the application's UI doesn't behave as the Process expects, the bridge
hands the failure to Reparo as one **Recovery**: Reparo sees the browser through screenshots and
acts on it, then names the Event the run should continue from. The run resumes there.

Vocabulary (Process, Event, Activity, Variables, UI log, Recovery, Continuation, Completion check)
is defined in Reparo's shared glossary, in the private
[ReparoHome](https://github.com/TunixR/ReparoHome/blob/main/GLOSSARY.md) repo; the Playwright terms
(Active page, Logged call, Locator rule, …) are in [GLOSSARY.md](GLOSSARY.md).

- [Install](#install)
- [Authoring a Process](#authoring-a-process)
- [Running it](#running-it)
- [ReparoSettings](#reparosettings)
- [Outcomes](#outcomes)
- [Logging](#logging)
- [Testing with FakeReparo](#testing-with-fakereparo)
- [The spending-approval example](#the-spending-approval-example)
- [Usage guides](#usage-guides)
- [Development](#development)

## Install

Python 3.13+. As a dependency (add `[fake]` to get `FakeReparo` for your tests):

```bash
uv add "reparo-playwright-bridge[fake] @ git+https://github.com/TunixR/PlaywrightReparoBridge"
uv run playwright install chromium
```

On a clean Linux machine (a server, a container, CI), Chromium's system libraries are missing.
Playwright's driver also needs `libatomic1` before it can install anything. On Debian or Ubuntu:

```bash
sudo apt-get install -y libatomic1
uv run playwright install --with-deps chromium   # --with-deps installs Chromium's libraries (sudo)
```

## Authoring a Process

A Process is built with `with` blocks and decorators. Mistakes in its structure (an Event outside
an Activity, a duplicate id, a Completion check inside a block) raise at definition time.

```python
from reparo_playwright_bridge import CheckContext, EventContext, Process


def build_process() -> Process:
    p = Process("approve_requests", variables={"approval_limit": int})

    with p.activity("Open the requests"):

        @p.event("Open the requests page")
        async def open_requests(ctx: EventContext) -> None:
            await ctx.page.goto("/requests")

        @p.event("Read the pending requests")
        async def read_pending(ctx: EventContext) -> None:
            ids = await ctx.page.locator(".request-id").all_inner_texts()
            ctx.vars["pending"] = [int(i) for i in ids]

    with p.for_each("each_request", over="pending", as_="request_id"):
        with p.activity("Open the request"):

            @p.event("Open the request")
            async def open_request(ctx: EventContext) -> None:
                await ctx.page.get_by_role(
                    "link", name=f"Open request {ctx.vars['request_id']}"
                ).click()

            @p.event("Read the amount")
            async def read_amount(ctx: EventContext) -> None:
                ctx.vars["amount"] = int(await ctx.page.locator("#amount").inner_text())

        with p.if_(
            "decide",
            lambda v: v["amount"] <= v["approval_limit"],
            label="within the limit",
        ):
            with p.activity("Approve the request"):

                @p.event("Click Approve")
                async def click_approve(ctx: EventContext) -> None:
                    await ctx.page.get_by_role("button", name="Approve").click()

        with p.else_(), p.activity("Reject the request"):

            @p.event("Click Reject")
            async def click_reject(ctx: EventContext) -> None:
                await ctx.page.get_by_role("button", name="Reject").click()

    @p.completion_check("no request is left pending")
    async def none_pending(ctx: CheckContext) -> tuple[bool, str | None]:
        await ctx.page.goto("/requests")
        left = await ctx.page.locator('tr[data-status="pending"]').count()
        return (True, None) if left == 0 else (False, f"{left} requests still pending")

    return p
```

The rules that matter:

- **Events.** `@p.event("<grounded name>", recoverable=True, secret=False)` on an
  `async def <id>(ctx)`. The function name is the **Event id**, which Reparo names when it says
  where to continue; ids are unique across the Process. The grounded name says what the Event
  does on screen ("Click Approve") and may repeat.
- **One logged call per Event.** Each Event performs **exactly one action** (`click`, `fill`,
  `type`, `press`, `select_option`, `check`, `hover`, `goto`, …) or, with no action, is a **read**
  (`inner_text`, `text_content`, `input_value`, `get_attribute`, …; the last one is logged). A
  second action, or no logged call at all, is a Process bug. Checks such as `expect(...)`,
  `is_visible()`, `count()` and `wait_for()` are never logged, so put the check of an action's
  outcome in the same Event as the action.
- **Events share only Variables.** `ctx.vars` is the mutable Variables mapping; it is the only
  state that passes between Events, because a Continuation may start at any Event. Never carry a
  locator, an element handle or a Python local from one Event to another. Variables must be
  JSON-serialisable: they are sent to Reparo.
- **Activities** (`p.activity(name, recoverable=True, recovery_task=None)`) group Events into
  business steps for the UI log. Every Event belongs to exactly one Activity, and Activities
  don't nest. `recovery_task` is a recovery task you supply for failures in that Activity:
  experimental, and Reparo may ignore it on a recovery connection. Without one, the Activity's
  name is sent, since an eval needs one.
- **Control flow** belongs to the bridge, so it can resume anywhere:
  `p.for_each(id, over=<list Variable>, as_=<item Variable>)`, and
  `p.if_(id, cond, label=)` / `p.elif_(cond, label=)` / `p.else_()`. Conditions are pure
  functions of the Variables. Plain Python `if` / `for` around Events is not supported.
- **`ctx.fail(description)`** declares a UI failure when the page is in a state the Event didn't
  expect. Playwright's own timeouts and failed `expect`s count as UI failures too.
- **Opting out.** `recoverable=False` on an Event or an Activity keeps its UI failures local.
  `secret=True` masks an Event's typed input and states as `***` everywhere Reparo could see them.
- **Completion checks** (`@p.completion_check("<grounded name>")`, top level only) run in order
  once the Process reaches its end, with or without a Recovery. Each returns `(True, None)` or
  `(False, reason)` with a non-empty reason. `ctx.page` doesn't record and `ctx.vars` is read-only.
- **`@p.on_continuation`** (at most one) decides the Variables a run resumes with:
  `fn(returned, local) -> Variables`. Without it, the keys Reparo returns override the local ones.
- **The Process model.** `p.to_model("bpmn")` or `p.to_model("compact")` shows what Reparo
  receives about the Process's structure.

Before you write Events that read values, read
[Idempotent reads and deliberate Variable writes](docs/guides/reads-and-variables.md); before you
write Completion checks, read [Atomic Completion checks](docs/guides/completion-checks.md).

## Running it

```python
import asyncio
from playwright.async_api import async_playwright
from reparo_playwright_bridge import ReparoSettings, run


async def main() -> None:
    # The robot key comes from REPARO_ROBOT_KEY.
    reparo = ReparoSettings(base_url="https://reparo.example.com")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720}, base_url="https://app.example.com"
        )
        result = await run(
            build_process(),
            context=context,
            reparo=reparo,
            case_id="batch-2026-11-14",
            variables={"approval_limit": 500},
        )
        print(result)  # one line, for example "completed after Recovery 6f1c…"
        await browser.close()


asyncio.run(main())
```

- `context` is yours. The bridge opens a page only if the context has none, so you can prepare a
  starting page first (a login, say). It never closes the context or its pages.
- The viewport must be **fixed** (`viewport={...}` on the context): Reparo's screenshots and click
  coordinates rely on it.
- `case_id` (a `str | int`, or a zero-argument callable returning one, called once per run) tells
  Reparo which case of the Process this run is.
- `variables` are the starting Variables. Every input declared with `Process(..., variables=...)`
  must be present with its declared type, or the run ends `local` before touching the page.
- `modalities` (default `(VisionModality(),)`) are the ways Reparo may see and act on the browser,
  most preferred first. `DomModality` stands for a future DOM-based Modality; constructing it raises
  `NotImplementedError` for now.
- `run()` never raises for a failed run: it returns a `RunResult`.

A run makes **at most one Recovery**. A UI failure after the Continuation, a Process bug (any other
exception, including a plain `assert`) and an Infrastructure failure (the browser or a service went
away) all end the run locally and are never sent to Reparo.

## ReparoSettings

A frozen pydantic model, built once and reused across runs.

| Field | Default | Meaning |
|---|---|---|
| `base_url` | required | Reparo's `http(s)://` base URL |
| `robot_key` | `REPARO_ROBOT_KEY` | The robot's key, held as a `SecretStr`: never shown, logged or put on `RunResult`. Construction fails with neither the argument nor the variable |
| `model_format` | `"bpmn"` | How the Process model is sent: `"bpmn"` (BPMN 2.0) or `"compact"` |
| `silence_timeout` | 2 min | How long Reparo may stay silent during a Recovery before the bridge gives up |
| `recovery_deadline` | 10 min | The longest a whole Recovery may take |
| `connect_attempts` | 3 | Attempts to open Reparo's WebSocket, with backoff between them |
| `eval_agent_id` | `None` | A GUI agent's id (UUID). When set, the failure goes to Reparo's eval endpoint (`/evals/recovery/ws`) with the id as `X-AGENT-ID`, so that agent is evaluated against it. The eval reads the payload's `recovery_task` (the failing Activity's, else its name). An unknown agent id ends the run `local`, like a rejected robot key |

## Outcomes

`RunResult` has `outcome`, `reason` (`None` only for `completed`), `failure` (where and why a
failed run failed) and `recovery_id` (Reparo's id, whenever a Recovery ran). `str(result)` is one
summary line, for example `checks_failed after Recovery 6f1c: 1 completion check not met: …`.

| `outcome` | Meaning |
|---|---|
| `completed` | The Process reached its end and every Completion check was met |
| `checks_failed` | The Process reached its end, but a Completion check was not met |
| `local` | Failed before any Recovery and wasn't handed to Reparo (Process bug, Infrastructure failure, opted-out UI failure, missing inputs, …); `reason` says which |
| `recovery_failed` | Reparo tried and couldn't recover the failure |
| `recovery_unavailable` | Reparo couldn't be reached, rejected the payload (`failure.type == "payload_rejected"`) or timed out (`"timeout"`) |
| `continuation_rejected` | Reparo's answer didn't name a valid Event and loop pass to continue from |
| `continuation_failed` | The run failed after resuming from the Continuation |

When a Recovery ran, the bridge reports back to Reparo once at the end, with
`success = (outcome == "completed")`.

## Logging

The bridge logs to the `reparo_playwright_bridge` logger tree (`.run`, `.recovery`, `.checks`,
`.report`, `.testing`) and installs only a `NullHandler`: you choose where logs go.

```python
logging.basicConfig(level=logging.INFO)
```

At INFO a run reads as its Activities, the Recovery milestones and exactly one outcome line
(`str(result)`). DEBUG adds each Event, each Logged event, each Observation and Recovery action,
and each connect attempt. Records carry `case_id`, `event_id`, `loop_iteration` and `activity_id`
as `extra` fields where they apply, for filtering and indexing.

The logs are safe to ship: the robot key and auth headers, screenshot bytes, typed text (only its
length), Variable values (only their names) and the full payload are never logged, at any level.

## Testing with FakeReparo

`reparo_playwright_bridge.testing.FakeReparo` (needs the `[fake]` extra) is a Reparo on localhost
that plays a hand-written Recovery script against your real browser. It checks every payload
against Reparo's schema and every screenshot reply, and records what happened.
With `ReparoSettings(..., eval_agent_id=reparo.agent_id)` it is reached on the eval endpoint
instead; it accepts only that agent id, as it does the robot key, and plays the same script.

```python
from reparo_playwright_bridge import ReparoSettings, run
from reparo_playwright_bridge.testing import Act, Continue, FakeReparo, Locate


async def test_a_moved_approve_button_is_recovered(context) -> None:
    script = [
        Act("left_single", Locate("#actions-menu")),  # click the centre of the element
        Act("left_single", Locate("#approve")),
        Continue("read_status", []),  # resume at Event read_status, no loop pass
    ]
    async with FakeReparo(script, context=context) as reparo:
        result = await run(
            build_process(),
            context=context,
            reparo=ReparoSettings(base_url=reparo.url, robot_key=reparo.robot_key),
            case_id=1,
        )

    assert result.outcome == "completed", result.reason
    assert reparo.reports[0].body == {"success": True}
```

Script steps:

| Step | FakeReparo sends |
|---|---|
| `Observe()` | A screenshot request |
| `Act(action_type, target, **inputs)` | A screenshot request, then a Recovery action. `target` is `Locate(selector)` or a literal `[x, y]`; for example `Act("type", content="hello")` |
| `Continue(event_id, loop_iteration, variables=None, process_finished=False)` | Reparo's answer: continue at that Event and loop pass. `variables=None` echoes the payload's Variables |
| `Fail(reason)` | A failed Recovery |
| `SendError(code)` / `SendRaw(frame)` | An `error` frame / any frame as-is |
| `Silence(seconds)` / `Close()` | Nothing for a while / closes the socket |

Records: `reparo.payloads`, `reparo.replies` (screenshots as `bytes`), `reparo.aborts`,
`reparo.reports`, plus `reparo.script_exhausted` and `reparo.problems` (contract violations by the
robot). `Locate` needs `context=`; nothing else in `run()` changes for the fake.

## The spending-approval example

[`examples/spending_approval`](examples/spending_approval) runs a Process that approves or rejects
spending requests in a small synthetic web app, in priority order, against a 500 approval limit
and a remaining budget. The `changed` UI moves Approve into an "Actions ▾" menu, so the original
Process fails there and Reparo recovers it.

```bash
uv run python -m examples.spending_approval --ui original --reparo fake            # no Recovery
uv run python -m examples.spending_approval --ui changed --reparo fake             # one Recovery
uv run python -m examples.spending_approval --ui changed --reparo fake --case full -v --headed
REPARO_ROBOT_KEY=… uv run python -m examples.spending_approval --ui changed --reparo real \
    --reparo-base-url https://reparo.example.com
REPARO_ROBOT_KEY=… uv run python -m examples.spending_approval --ui changed --reparo real \
    --reparo-base-url http://localhost:8000 --eval-agent-id <gui-agent-uuid>   # an eval
```

The `real` runs need a Reparo and a robot key. To run one locally, follow the
[Reparo Quickstart](https://github.com/TunixR/Reparo/tree/develop#quickstart) on its `develop` branch, which speaks this contract: it starts Reparo on
`http://localhost:8000`, seeds the default agents, and creates the key with `POST /keys`. A
Recovery's outcome depends on the LLM, so `recovery_failed` can happen even when the setup is
correct; check `str(result)` and Reparo's traces.

`--case single` (default) has one request; `--case full` has five, exercising every branch. `-v`
logs at DEBUG and `--headed` shows the browser. It logs to stdout, and the outcome line is its
result: exit code 0 means `completed`, 1 any other outcome.

## Usage guides

- [Idempotent reads and deliberate Variable writes](docs/guides/reads-and-variables.md)
- [Atomic Completion checks](docs/guides/completion-checks.md)

Design decisions are in [docs/adr](docs/adr).

## Development

```bash
git clone --recurse-submodules https://github.com/TunixR/PlaywrightReparoBridge
cd PlaywrightReparoBridge
git config core.hooksPath .githooks  # once: the commit-msg hook checks the commit format
uv sync
uv run playwright install chromium  # once: tests use real headless Chromium (see Install for Linux deps)
uv run pytest
uv run ruff check
uv run ruff format
uv run basedpyright
```

`--recurse-submodules` fills `.agents/reparo-skills`, the agent skills shared from the private
ReparoHome repo (`git submodule update --init` does it later); `.agents/skills` links to each of
them. Without access to it the folder stays empty, and nothing else depends on it. Commits and PR titles follow `type(scope)!: what`, with the scopes
listed in [AGENTS.md](AGENTS.md#commits); the hook and a CI job on PR titles check it.
