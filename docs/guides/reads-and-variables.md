# Idempotent reads and deliberate Variable writes

Two habits make a Process behave well when Reparo recovers it:

1. **Reads are idempotent.** Running a read Event again, on the same page, gives the same Variables
   and changes nothing on the page.
2. **Each Variable write comes from one deliberate read.** A Variable that holds a fact about the
   application is assigned from a read of that fact, in its own read Event, not computed from
   earlier Variables, accumulated, or picked up in passing by an action Event.

Both follow from two things the bridge does: what it puts in the **UI log** Reparo receives, and
how a **Continuation** resumes the run. Vocabulary is in home's
[GLOSSARY.md](https://github.com/TunixR/ReparoHome/blob/main/GLOSSARY.md), Playwright terms in
[this repo's](../../GLOSSARY.md).

## What Reparo sees: the UI log

Each Event performs one logged call, and each performance becomes one **Logged event** in the UI
log:

- An **action Event** (`click`, `fill`, `select_option`, …) logs the action, its target, its input
  and the target's state just before and just after.
- A **read Event** (no action) logs one `read` entry built from its **last** read call
  (`inner_text`, `input_value`, `get_attribute`, …). A read puts the value it read in both states.
- Reads inside an action Event are not logged at all: the action is the Event's logged call.

When a UI failure hands the run to Reparo, the payload carries the UI log, the **Variables** as
the failing Event started, and the Process model. Reparo uses the read entries to tell where each
Variable came from, and to judge what the Variables should be once it has acted on the browser.

So a Variable is **explained** to Reparo only when a read entry shows its value:

```python
@p.event("Read the amount")
async def read_amount(ctx: EventContext) -> None:
    ctx.vars["amount"] = int(await ctx.page.locator("#amount").inner_text())
    # UI log: read on "#amount", state "300". Reparo sees exactly where amount=300 came from.
```

Compare the ways a Variable ends up unexplained:

```python
# Two reads, two writes: only the last read is logged; `amount` has no entry.
@p.event("Read the request")
async def read_request(ctx: EventContext) -> None:
    ctx.vars["amount"] = int(await ctx.page.locator("#amount").inner_text())
    ctx.vars["budget"] = int(await ctx.page.locator("#budget").inner_text())


# A read inside an action Event: the click is logged, the read isn't.
@p.event("Click Approve")
async def click_approve(ctx: EventContext) -> None:
    await ctx.page.get_by_role("button", name="Approve").click()
    ctx.vars["budget"] = int(await ctx.page.locator("#budget").inner_text())


# Computed, not read: no entry shows the page ever said this.
@p.event("Click Approve")
async def click_approve(ctx: EventContext) -> None:
    await ctx.page.get_by_role("button", name="Approve").click()
    ctx.vars["budget"] -= ctx.vars["amount"]
```

Reading two facts in one Event is sometimes a fair trade (fewer Events, and the facts sit side
by side on the page), but know that Reparo sees only the last one. The spending-approval
example makes that trade: its `read_request` reads `amount` and then `remaining_budget`, so
Reparo sees the budget read and not the amount. When the provenance of a Variable matters to
Reparo (it decides a branch, or Reparo may have to return it), give it its own read Event.

## How the run resumes: Continuation

Reparo ends a successful Recovery by naming the Event the run should continue from (its Event id
and loop pass), with the Variables it returns. The bridge then:

- starts from the Variables **as the failing Event started** (its start snapshot) and overrides
  them, key by key, with the keys Reparo returned; if you registered `@p.on_continuation`, the
  hook gets both, unmerged (`returned`, `local`), and its return value is used instead;
- runs from the named Event, which may be **the failing Event itself** (retry), **an earlier
  Event** (re-run from there) or **a later one** (Reparo already did what lies in between).

Each of these is why the two habits matter.

### Retrying and re-running repeat your Events

When Reparo names the failing Event or an earlier one, Events run a second time. A read Event that
changes something, or that depends on how many times it ran, then gives a different answer:

```python
# Not idempotent: re-running it double-counts.
@p.event("Read the amount")
async def read_amount(ctx: EventContext) -> None:
    ctx.vars["total"] = ctx.vars.get("total", 0) + int(
        await ctx.page.locator("#amount").inner_text()
    )


# Not idempotent: the "read" changes the page (expands a panel), so a second run collapses it.
@p.event("Read the details")
async def read_details(ctx: EventContext) -> None:
    await ctx.page.get_by_role("button", name="Details").click()  # an action, really
    ...
```

The Variables a re-run starts from are the failing Event's start snapshot, which already holds
what the earlier Events wrote. A read that **assigns** a value off the page (`vars[x] = <read>`)
gives the same result however often it runs. A read that **accumulates** (`+=`, `append`) or
derives a value from what the Variable held before does not. If something must change on the page
before you can read it, that is an action: make it its own Event.

### Skipping ahead relies on the page, not on your arithmetic

When Reparo names a later Event, it has done the skipped work itself, through the UI. Your
Process never ran the Events in between, so any Variable those Events would have **computed** is
stale, unless Reparo returned it. Reparo can only return Variables it can explain, which brings us
back to the UI log: if `budget` was read off the page in a read Event, Reparo knows what the page
shows and can return it; if `budget` was computed in Python, Reparo has nothing to go on.

The safest Variable is one the Process re-reads itself after the point it may resume at. The
spending-approval example never computes the budget: it reads it off each request, and
`read_final_budget` goes back to the list and reads it once more before the Completion check.
Its fake Recovery script continues at `read_final_budget`, so the check sees the real budget even
though Reparo, not the Process, approved and rejected the remaining requests.

## Checklist

- A read Event only reads. Anything that changes the page is an action and gets its own Event.
- Assign Variables from reads (`vars[x] = await ...`). Never accumulate across runs of an Event or
  derive a fact about the application from earlier Variables.
- One read Event per Variable that matters; remember only an Event's last read is logged.
- Don't read in action Events into Variables Reparo should understand.
- Re-read facts after the points the run may resume at, instead of carrying them forward.
- Keep Variables JSON-serialisable: they travel to Reparo and back.

The same reasoning, applied to the end of the run, gives
[Atomic Completion checks](completion-checks.md).
