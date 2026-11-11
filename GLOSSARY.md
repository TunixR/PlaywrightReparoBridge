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

**Event**: The atomic node of a **Process**: one grounded UI action ("click Approve", "type the
amount into Amount"), recorded as one entry of the **UI log** when performed. Every **Continuation**
resumes at an Event, so an Event depends on nothing produced by another Event except **Variables**.
Corresponds to Reparo's `event_id` / `event_name`.
_Avoid_: step, action (reserved for what Reparo sends back during a **Recovery**).

**Variables**: The named, serialisable values a **Process** reads and writes; the only state that
passes from one **Event** to another, and the state sent to Reparo with a failure.

**UI log**: The ordered record of **Events** performed in a run up to the failure, sent to Reparo
as part of the failure payload.

**Errored activity**: The **Activity** (and the **Event** inside it) where the run failed, with the
error that stopped it.

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

**Continuation**: Resuming the **Process** after a **Recovery**, at the **Event** Reparo says should
run next, skipping whatever the **Recovery** already did and nothing it did not.
