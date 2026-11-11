# Events are the continuation unit, and share nothing but Variables

After a Recovery, the bridge resumes the Process at the **Event** Reparo names, not at the start of
an Activity. Reparo's agent may stop partway through an Activity, or do an extra action on the way,
so resuming at Activity granularity would either re-run Events the Recovery already did or skip
Events it did not; and if the Recovery moved the browser to another screen, re-running code written
for the previous screen fails. We therefore model each Event as an atomic node of the Process
(like an activity in BPMN) and group Events into Activities only for labelling.

## Consequences

- An Event may depend on no object produced by another Event (no Playwright locators, element
  handles, or Python locals carried across). The only state that passes between Events is the
  Process's **Variables**, which are serialisable and are what Reparo receives.
- Control flow (conditions, branches, loops) is owned by the bridge's engine and connects Events, so
  the engine can start a run at any Event given only the Variables. Plain Python control flow wrapped
  around Events cannot meet this and is ruled out as the process format.
