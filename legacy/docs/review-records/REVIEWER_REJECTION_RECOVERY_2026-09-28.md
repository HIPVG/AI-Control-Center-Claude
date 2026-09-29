# Reviewer rejection recovery

Human authority: 「実装して設計文書に反映してください。」 in this Codex chat,
RECORDED_DIRECT_CONVERSATION, recorded 2026-09-28; exact message ID/time UNKNOWN.

Previously the missing AUTHORITY_RECORD in the 001 trigger left WAITING_RESPONSE
with a file_error indefinitely. Pre-publication validation did not handle that
runtime failure. This change implements G4 §15.1 and its G5 WC-07/07A test mapping.

The deterministic worker owns transport-only recovery under the existing cycle
lock. It creates a new request only when the fixed request proves a missing
authority reference is the sole discrepancy. It retains the old evidence, immutable
baseline, decision and scope. It does not run Codex to interpret a rejected reply.
Post outcomes are read back; failed/ambiguous POSTs are not blindly retried. Existing
explicit replacements are followed. A successful delivery is not yet resolution:
matching reply application/ACK is required. Unsupported failures and time/trial
exhaustion appear as persistent dashboard escalation with a concrete next action.

Tests use fresh persisted JSON state and simulated GitHub only. No live Watcher
restart, source-load claim, real automatic repair or product execution is included.
Existing runtime config, engineering history and prior acceptance notes are preserved.

Validation: `python -m pytest tests/test_reviewer_recovery.py tests/test_reviewer_files.py
tests/test_reviewer_confirmation.py tests/test_reviewer_bus.py -q --basetemp
.pytest-transport-recovery-deadline` -> 60 passed. `node tests/test_reviewer_status.cjs`
-> 7 passed. Evidence assertions cover rejection with zero continuations, retained
old evidence, new request/trigger and readback, one application across JSON restart,
ambiguous POST without a duplicate, two failed create attempts, late valid response
blocked after deadline, changed authority refused, existing replacement reused and
recursive repair refused. UI tests confirm persistent escalation/action/deadline.
The first run exposed one test that mutated an already quarantined request to test
a second failure; independent fixtures now test both failures without rewriting
the first rejection. No tests were weakened to accept a rejected response.

Work in this turn is maintenance ACTIVE_WORK (including code/docs/tests); it is
not WC-02 progress. Implementation began about 20:42 JST; report preparation follows
focused verification around 20:51 JST. No previous unmeasured time is claimed zero.
Deployment and real transport validation remain unverified; restart is not performed.
