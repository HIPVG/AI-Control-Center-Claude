# WC-02 new work-window authority

- Decision ID: `AUTH-G6-WC02-WINDOW-20260928-001`
- Decision maker: 広瀬剛
- Source: `RECORDED_DIRECT_CONVERSATION`, this Codex chat
- Exact human message: 「新しい作業枠を承認します。」
- Message ID and exact time: `UNKNOWN`; recorded 2026-09-28.

## Subject, effect, and limits

The preceding `G6-ACC-WC02-BUDGET-20260928-001` reviewer response identified the
single unresolved authority: whether to grant a new G6 work window of no more
than 30 ACTIVE_WORK minutes for the already-approved foreground WC-02 route.
This decision grants that one window to the foreground Codex actor.

The existing WC-02 scope, allowed production targets, focused test targets,
zero-cost limit, and two-fixture-attempt cap remain unchanged.  The window is
only for the smallest WC-02 admission/preflight patch and fresh-process focused
fixtures.  It does not authorize a new host writer, Watcher-child source write,
service reload, Day selection/Go, model activity, credential change, paid work,
WC-03/G7/G8, destructive Git, or a wider redesign.  Normal 10-minute progress
reporting (and 15-minute maximum without a report) applies to this new window.

## Correlation and review handling

`G6-ACC-WC02-BUDGET-20260928-001` has the matching Reviewer reply
`5868498498` with `RESULT: HUMAN_REQUIRED`, but its persisted entry has no
`REVIEWED_COMMIT`.  Therefore this record must not invent an original commit,
alter the old entry, or claim `RESOLVED_BY_CONFIRMATION`.  The old human-wait
entry remains traceable as historical unbound bookkeeping.

Instead, the new fixed-commit report `G6-ACC-WC02-WINDOW-20260928-001` records
this direct authority and asks Reviewer to confirm only the stated, unchanged
window.  A matching positive response is required before foreground implementation.
That review check is scope/provenance control; it is not a request for the human
to repeat approval in GitHub.  On mismatch, rejection, or no response, no WC-02
source or fixture work starts.

### Delivery correction

The `001` PR trigger omitted `AUTHORITY_RECORD` even though its immutable request
contained that field.  The Watcher correctly retained the request and recorded
`REQUEST_BINDING_MISMATCH_AUTHORITY_RECORD`; no Reviewer response can authorize it.
Do not alter `001` or its state.  Replacement `G6-ACC-WC02-WINDOW-20260928-002`
uses the same decision and limits with a new immutable request and an identical
complete binding in its trigger.  This is a delivery correction, not a new human
decision or a reset of the work window.

### Clock correction

`0/30` means `EXECUTION_WINDOW_ACTIVE_WORK`: the explicit gate for this window is
the matching Reviewer response, so no WC-02 source or fixture activity has begun.
It does not mean that no work occurred after approval.  Authority recording,
fixed-request preparation and the `001` delivery correction are
`TRANSPORT_ACTIVE_WORK` and must be reported separately.  The prior report omitted
that distinction; future file-mode delivery must pass the deterministic
request/trigger binding preflight before publication.

## Planned evidence and stop

Before implementation, re-read the accepted foreground-route response and this
record, then set the new window's active-work tally to zero and preserve the
existing fixture tally at `0/2`.  Evidence will be the explicit source diff,
exact focused test command, state transitions showing allowed admission reaches
`PREFLIGHT` only, and denial fixtures that create a blocker without Day work.
Stop at the first reviewer boundary, exhausted 30-minute window, two fixture
attempts, failed/unavailable authority, or any scope expansion.  This decision
does not itself implement or accept WC-02.
