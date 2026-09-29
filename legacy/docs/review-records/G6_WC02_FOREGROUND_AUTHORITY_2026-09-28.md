# WC-02 foreground route: human authority and preimplementation review

Decision ID: AUTH-G6-WC02-FOREGROUND-20260928-001
Decision maker: 広瀬剛. Source: RECORDED_DIRECT_CONVERSATION, this Codex chat.
Exact latest human message: 「はい、そうしてください。」
Context: immediately answers the proposal to use this chat's existing edit/test/push
route for WC-02, instead of creating a new host-side writing program; submit that
route to Reviewer before resuming G6. Preceding instruction:
「じゃ、これで出来上がりですね。G6経戻りましょう。」
Original message IDs/exact message times: UNKNOWN; recorded 2026-09-28.
This is a trusted operational relay, not independent authentication of the chat.

## Effect and exclusions

Authorize the existing foreground Codex actor's scoped edit and focused verification
of the already-approved G5 WC-02 after matching route review. No new privileged
writer, worker escalation mechanism, watcher runtime change, service restart/reload,
Day/Go/model, credential, paid action or destructive Git operation is authorized.
Normal scoped commit/push and Reviewer delivery remain the existing workflow.
Human does not need to repeat this approval in GitHub or the Reviewer chat.

## Original blocker and changed condition

G6-ACC-WC02-WRITE-PATH-DIAG-20260928-001 (request comment 5867456660) received
HUMAN_REQUIRED in response 5867480489. The failed actor was the Watcher's separate
`codex exec --sandbox workspace-write` child. Repeating that patch path is forbidden.
This foreground actor subsequently edited source/tests and published 5447427 and
8cfffd7 with successful focused tests. That is evidence for this actor only, not
proof that the sandboxed child was repaired or that WC-02 already passes.

New request: G6-ACC-WC02-FOREGROUND-20260928-001. Its reviewed baseline is the
current WC-02 source at 8cfffd7b5c8c8b9ca06c1cc027a0243dcbaf2800; this record and
CURRENT_WORK will be pinned separately in the request's AUTHORITY_RECORD.
The old diagnosis omitted REVIEWED_COMMIT and its registry has no reviewed_commit.
The existing confirmation validator requires an exact nonempty original commit.
Do not invent one, mutate the old comment/state, or send a knowingly invalid
automatic confirmation. This new separately pinned route request references the
old IDs for Reviewer assessment but does not claim RESOLVED_BY_CONFIRMATION.
The old HUMAN_REQUIRED marker remains as legacy unresolved bookkeeping; it must
not be treated as authority for a new host writer. Ask Reviewer to distinguish
permission for this new route from automated closure of that old unbound entry.
No validator/policy relaxation is implemented in this task.

## Bounded implementation plan and responsibilities

- Foreground Codex: minimal WC-02 preflight/deny implementation and isolated tests.
  Allowed production targets: backend/control/day_git.py,
  backend/control/local_llm_day_program.py, backend/orchestrator/engine.py; edit only
  those actually needed. WC-01 contracts are reused, not redesigned.
  Test targets: tests/test_day_admission.py (new focused fixture module),
  tests/test_day_git.py and tests/test_local_llm_day_program.py only if required
  for existing preflight coverage. Evidence target: docs/review-records/G6_WC02_2026-09-28.md.
- Reviewer: inspect fixed plan/diff/evidence and issue matching bounded direction.
- Watcher: acquire/correlate the new file reply and record the route-review result.
  Its child must not attempt WC-02 source writes. On positive review its action is
  NO_REPORT with the complete response available for foreground implementation.
- Human: genuine additional authority boundaries only, not a copy/paste relay.

Happy path: matching positive route review -> foreground reloads current files
and checks residual budget -> minimal patch -> fresh Python test process imports
patched source with temporary fixtures -> observed admission/denial transitions ->
explicit-path diff/commit and evidence to Reviewer. A fresh test process supplies
code loading; the live app need not reload because WC-02 does not run a real Day.

Failure path: mismatch/rejection holds the checkpoint; write failure stops without
repeating the old child path; failed fixture is classified and may be corrected
only within the existing two-attempt cap. Missing permission/budget is a blocker,
not a guessed default. No clean/reset or live-state manipulation for a pass.
Rollback: before commit, reverse only this actor's reviewed WC-02 patch hunks using
apply_patch if safe; preserve unrelated work. After publication, use a forward
corrective change and new review, not history rewriting. No live deployment to undo.

Terminal evidence: fixed source/diff and exact test command; valid admission only
enters PREFLIGHT, dirty Git / unknown permissions / missing limits / contract
mismatch enter recorded blockers without Day execution. Assert state and side-effect
boundaries, not exit code alone. Prior diagnosis reported fixtures 0/2; no fixture
is run by this authority-recording turn. Recover remaining ACTIVE_WORK from prior
records before implementation; do not reset stage budget merely by changing actor.

UI review ACCEPT_COMPLETE at Bridge 27c46c1813ef1210e55a00b8ba341408de9a9864 was read
in full. Its scope is UI-only; this human decision, not UI acceptance, supplies the
new foreground route authority. G6/WC-02 implementation is not completed by this record.
