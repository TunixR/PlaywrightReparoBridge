# Glossary

**Reparo-wide terms live in home's
[`GLOSSARY.md`](https://github.com/TunixR/ReparoHome/blob/main/GLOSSARY.md)** (private; `../ReparoHome/GLOSSARY.md`
in a local clone): Process, Event, Event id, Activity, Variables, Process model, Logged event, UI
log, Completion check, Run outcome, Modality, Observation, Recovery action, Text targeting, the
kinds of Failure, Recovery, Recovery id, Errored activity, Recovery task, Continuation. They bind
this repo. This file adds only what is particular to Playwright and this Bridge, and contradicts
nothing there.

## The page

**Active page**: The page of the run's `BrowserContext` that the Bridge works on: the most
recently opened page that is still open, or a new page when none is. An Event's `ctx.page` is
the Active page when the Event starts, a Completion check's when the check starts. During a
Recovery, Observations and Recovery actions follow it, so a tab Reparo opens becomes the page the
Continuation runs on; a Recovery that loses its last page ends with an Infrastructure failure.
_Avoid_: current page, main page

**Recording page**: The `ctx.page` an Event receives: the Active page wrapped so that its `goto`,
its selector shortcuts and the Locators it makes (frames and `mouse.wheel` included) record the
Event's Logged call. Every other call is Playwright's, untouched, and `expect` works on it as on
any page. A Completion check's `ctx.page` is the Active page unwrapped: nothing it does reaches
the UI log.

## What an Event records

**Logged call**: The one Playwright call an Event is recorded as, which becomes its Logged event
in the UI log: its action if it performed one, else its last read. An Event with no logged call,
or with a second action, is a Process bug.
_Avoid_: logged action (a read can be the logged call)

**Action call**: A Playwright call with a side effect on the UI, made through the Recording page:
`click`, `dblclick`, `fill`, `type`, `press_sequentially`, `press`, `select_option`, `check`,
`uncheck`, `set_checked`, `hover`, `drag_to`, `set_input_files`, `scroll_into_view_if_needed`,
`goto` and `mouse.wheel`. An Event makes at most one. It is the Event's UI action, not a Recovery
action, which only Reparo sends.
_Avoid_: action (alone: home reserves it for Recovery actions)

**Read**: A call that takes a value off the page without changing it: `inner_text`,
`text_content`, `input_value`, `get_attribute`, `all_inner_texts`, `all_text_contents`. An Event
with no Action call is a read Event; its last read is its Logged call. Reads in an Event that has
an Action call are not logged.

**Unlogged call**: Any other Playwright call: checks such as `expect(...)`, `is_visible()`,
`count()` and `wait_for()`, and locator construction. It is never logged and never counts towards
the one action, which is why the check of an action's outcome goes in the same Event.

**Locator rule**: A Locator, element handle or Python local never crosses from one Event to
another; each Event builds its own from the Recording page. Only Variables cross, because a
Continuation may start at any Event (home ADR 0003).

**Grounded name**: An Event's name as the author writes it, saying what it does on screen ("Click
Approve", "Type the amount into Amount"). Several Events may share one; only the Event id, the
decorated function's name, is unique. Logged events are named after it plus the loop pass.

**Secret Event**: An Event declared `secret=True`: its typed input and the states read around its
Logged call are masked as `***` everywhere Reparo could see them.

## Failures and Recovery here

**Infrastructure failure**: As home defines it; in this Bridge, a Playwright error that says the
browser, context or page closed or crashed, a network error (`net::ERR_…`), or a failed
`APIRequestContext` call, and a Recovery whose Active page disappears. The Bridge ends the run
locally (outcome `local`, or the failed outcome after a Recovery) and, during a Recovery, aborts
with the wire code `EnvironmentFailure`, an older name for the same thing.
_Avoid_: environment failure (home's umbrella term, which UI failures also fall under)

**Recoverable**: Whether the Bridge hands an Event's UI failures to Reparo. `recoverable=False` on
an Event or an Activity keeps them local. Which failures a Robot may hand over follows what Reparo
supports; today that is UI failures, one Recovery per run, before any Continuation.

**Recovery task** (per Activity): A recovery task the process author supplies for failures in that
Activity (`p.activity(..., recovery_task=...)`): experimental, Reparo may ignore it.

**Vision Modality** (`VisionModality`): The Modality this Bridge offers: JPEG screenshots of the
Active page, Recovery actions aimed at viewport coordinates, which is why the viewport must be
fixed. Text targeting is not offered (`ocr: false`). `DomModality` names the DOM Modality but is
not available.

**Failed outcomes**: The Bridge splits home's "failed before its end" Run outcome by when the run
failed: `local` (before any Recovery, not handed over), `recovery_failed`, `recovery_unavailable`
(Reparo unreachable, payload rejected, or timed out), `continuation_rejected` (Reparo's answer
named no valid Event and loop pass) and `continuation_failed` (after resuming).
