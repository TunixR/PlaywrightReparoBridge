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
same Event; a check alone is never an Event. An Event performs at most one UI action with a side
effect; an Event with none is a read of a value off the page. Each Event has a grounded name,
which another Event may share; only its **Event id** is unique. Every **Continuation** resumes at an Event, so an
Event depends on nothing produced by another Event except **Variables**. Corresponds to a task of
the process model Reparo receives.
_Avoid_: step, task, action (reserved for what Reparo sends back during a **Recovery**).

**Event id**: The stable identifier of an **Event** within its **Process**, the same on every run
and every loop pass; the id Reparo names in a **Continuation**, and the link from each **Logged
event** to the Event it performed. Never a **Logged event**'s number. Corresponds to Reparo's
`model_event_id`.
_Avoid_: model event id (Reparo's wording for the same thing).

**Logged event**: One performance of an **Event** in a run, an entry of the **UI log**, numbered in
the order it happened; an Event inside a loop yields one Logged event per pass, named after the
Event plus that pass. The failing performance is not a Logged event: it is reported apart,
as the **Errored activity**. Corresponds to
Reparo's `event_id` / `event_name`.
_Avoid_: event (alone, when the performance is meant), UI log entry.

**Process model**: The description of a **Process**'s structure (its **Events** with their
**Event ids** and grounded names, and the conditions and loops connecting them) that Reparo
receives with a failure. It comes from the Process alone and is the same for every run.
Corresponds to Reparo's `model`.
_Avoid_: BPMN (one format the Process model can take, not the concept), diagram.

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

**Recovery id**: The identifier Reparo gives a **Recovery** once it has run, by which the
Recovery is found in Reparo and its result reported back.
_Avoid_: exception id, robot exception id.

**Recovery task**: A description, written by the process author, of what recovering an
**Activity** should achieve, offered to Reparo as a preferred plan.
_Avoid_: recovery hint.

**Modality**: A way the bridge lets Reparo see the browser and target elements during a
**Recovery**, pairing a kind of **Observation** with a way of aiming **Recovery actions**: vision
(screenshots, coordinates) today, DOM anticipated. A robot offers one or more, in order of
preference. Corresponds to Reparo's interaction modality.
_Avoid_: recovery mode, observation mode, agent mode.

**Observation**: What the bridge gives Reparo to see the browser's state during a **Recovery**, in
one of its **Modalities**. Today a screenshot; DOM or accessibility-tree observations are
anticipated.

**Recovery action**: A UI action Reparo asks the bridge to perform on the browser during a
**Recovery** (click, type, scroll...), aimed at its target through a **Modality** (coordinates
today).

**Continuation**: Resuming the **Process** after a **Recovery**, at the **Event** (named by its
**Event id**, plus the loop pass) Reparo says should run next, skipping whatever the **Recovery**
already did and nothing it did not.

**Completion check**: A check, written by the process author, of the state a **Process** should
have reached once its last **Event** has run, on every run, whether or not a **Recovery** happened.
It reports whether it is met, with a reason when it is not, and never fails the run itself. It
states what the robot can believe about its own completion, not an audit of whether the run was
truly correct. It is not an **Event**: it is never part of the **Process model**, never resumed at, and never
handed to Reparo.
_Avoid_: final assertion, verification step.

**Run outcome**: How a run of a **Process** ends: completed (the **Completion checks** passed),
checks failed (the Process reached its end but a Completion check was not met), or failed before
reaching its end. A failed run is further told apart by when it failed relative to a **Recovery**:
before any, because Reparo declined or could not be reached, because Reparo's answer could not be
continued from, or during the **Continuation**. Only a completed run counts as a successful
Recovery when its result is reported to Reparo.
_Avoid_: status, result (alone).
