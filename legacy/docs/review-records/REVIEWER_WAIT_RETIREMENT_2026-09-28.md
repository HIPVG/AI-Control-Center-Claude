# Reviewer human-wait retirement

- Decision ID: `AUTH-REVIEWER-WAIT-RETIREMENT-20260928-001`
- Decision maker: 広瀬剛
- Source: `RECORDED_DIRECT_CONVERSATION`, this Codex chat
- Exact human instruction: 「めんどくさいからいらないやつcloseして」
- Recorded date: 2026-09-28; source message ID/time: UNKNOWN.

## Effect

Retire only the following persisted Reviewer waits as
`NON_CONTROLLING` / `CLOSED_BY_DIRECT_HUMAN_DIRECTION`:

- `G5-ACC-REVIEW-20260928-004`
- `G6-ACC-WC02-BUDGET-20260928-001`
- `G6-ACC-WC02-WRITE-PATH-DIAG-20260928-001`
- `G6-ACC-WC02-WRITE-ACCESS-REPRO-20260928-001`
- `G6-ACC-WC02-IMPLEMENTATION-20260928-001`

The original report/reply identities and prior `HUMAN_REQUIRED` evidence remain
in the persisted registry. Retirement removes them from control and dashboard
action queues; it does not represent Reviewer acceptance, WC-02 completion,
G6 completion, or authority to begin WC-03, select/Go a Day, operate services,
run models, change credentials, spend money, or alter Git history.

This does not retire currently active transport recovery, pending replacement
delivery, or any future report. A later implementation decision needs its own
explicit authority and review evidence.

## Correction — retain the current WC-02 implementation review

After the retirement, the human clarified that the newest report must remain
actionable: 「もちろん」 in response to restoring
`G6-ACC-WC02-IMPLEMENTATION-20260928-001`. The earlier retirement list was too
broad. Remove only that ID from `NON_CONTROLLING` and restore its persisted state
to `HUMAN_REQUIRED`; retain its existing reviewer file-response provenance and
the complete human-required outcome. This restoration is not approval, does not
consume the review, and does not start WC-02 or any later card.

Correction decision: `AUTH-REVIEWER-WAIT-RESTORE-20260928-001`.
