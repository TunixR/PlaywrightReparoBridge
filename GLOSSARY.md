# Glossary

Language for the Reparo Playwright Bridge. Terms align with Reparo's recovery contract where it
already names the concept.

**Process**: A browser automation made of **Events** connected by control flow (sequence,
conditions, branches, loops), grouped into **Activities**, together with the **Variables** it reads
and writes.
_Avoid_: script, workflow, robot (the robot is what runs a process).

**Activity**: A named grouping of **Events** that together achieve one business step (for example
"Open the spending request"). It labels the **UI log**; it is not the unit execution resumes from.
Corresponds to Reparo's `activity_id` / `activity_name`.
_Avoid_: block, step, task.

**Event**: The atomic node of a **Process**, as defined, not as performed: one grounded UI action
("click Approve", "type the amount into Amount"). A check of that action's outcome belongs to the
same Event; a check alone is never an Event. Every **Continuation** resumes at an Event, so an
Event depends on nothing produced by another Event except **Variables**. Corresponds to a task of
the process model Reparo receives.
_Avoid_: step, task, action (reserved for what Reparo sends back during a **Recovery**).

**Event id**: The stable identifier of an **Event** within its **Process**, the same on every run
and every loop pass; the id Reparo names in a **Continuation**. Never a **Logged event**'s number.
_Avoid_: model event id (Reparo's wording for the same thing).

**Logged event**: One performance of an **Event** in a run, an entry of the **UI log**, numbered in
the order it happened; an Event inside a loop yields one Logged event per pass. Corresponds to
Reparo's `event_id` / `event_name`.
_Avoid_: event (alone, when the performance is meant), UI log entry.

**Variables**: The named, serialisable values a **Process** reads and writes; the only state that
passes from one **Event** to another, and the state sent to Reparo with a failure.

**UI log**: The ordered record of **Logged events** in a run up to the failure, sent to Reparo as
part of the failure payload.

**UI failure**: A failure inside an **Event** because the application did not behave as the
**Process** expected: an element missing, a check the author declared not met. The only kind of
failure handed to Reparo as a **Recovery**.
_Avoid_: error (alone), exception.

**Process bug**: A mistake in the **Process**'s own code (a wrong condition, a missing
**Variable**). It ends the run locally and is never handed to Reparo, wherever it happens.
_Avoid_: UI failure, coding error.

**Environment failure**: A failure that stops the run for reasons outside both the application's
UI and the **Process**'s code: a service that cannot be reached, a browser that closed or crashed.
It ends the run locally and is never handed to Reparo.
_Avoid_: UI failure, Process bug.

**Errored activity**: The **Activity** (and the **Logged event** inside it) where a **UI failure**
stopped the run, with a short description of that failure.

**Recovery**: One hand-over of a failed run to Reparo: the bridge reports the failure, Reparo
observes and acts on the browser until it is back in a state the process can continue from, then
hands control back.

**Recovery task**: A description, written by the process author, of what recovering an
**Activity** should achieve, offered to Reparo as a preferred plan.
_Avoid_: recovery hint.

**Observation**: What the bridge gives Reparo to see the browser's state during a **Recovery**.
Today a screenshot; DOM or accessibility-tree observations are anticipated.

**Recovery action**: A UI action Reparo asks the bridge to perform on the browser during a
**Recovery** (click at coordinates, type, scroll...).

**Continuation**: Resuming the **Process** after a **Recovery**, at the **Event** (named by its
**Event id**, plus the loop pass) Reparo says should run next, skipping whatever the **Recovery**
already did and nothing it did not.
