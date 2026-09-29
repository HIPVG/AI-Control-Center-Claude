# Current Work

## Current G6 checkpoint — WC-02 bounded repair confirmation (2026-09-28)

Human chose the bounded repair option in `AUTH-G6-WC02-REPAIR-20260928-001`,
recorded at `docs/review-records/G6_WC02_REPAIR_AUTHORITY_2026-09-28.md`.
The existing foreground Codex edit route is named and limited; the only intended
source change is fail-closed rejection of `active_work_seconds > 1800` or
`max_attempts > 2`, followed by one new focused fixture attempt.

First send a new exact confirmation report for the existing
`G6-ACC-WC02-IMPLEMENTATION-20260928-001` `HUMAN_REQUIRED` response. Do not
patch or run the new fixture until its matching positive confirmation is applied.
Then perform only the bounded repair, publish its fixed commit for review, and
stop. WC-02 is not yet accepted; WC-03, Day selection/Go, model/service work,
credential actions, paid work, and destructive Git remain excluded.

## Current maintenance — rejection recovery (2026-09-28)

Human explicitly requested implementation and design update of the missing
post-rejection path. Scope: G4 §15.1 deterministic transport recovery, bounded
create-only replacement request/trigger, readback, response/application tracking,
persistent dashboard escalation. No WC-02 source execution in this maintenance.
Record: docs/review-records/REVIEWER_REJECTION_RECOVERY_2026-09-28.md.
On the matching review of this maintenance, record the checkpoint with NO_REPORT;
deployment/restart and live recovery proof are distinct from fixture results.

## Current G6 checkpoint — WC-02 new work-window review (2026-09-28)

The matching foreground route review has been applied, but the former 30-minute
G6 window had only about five active minutes remaining before WC-02 source or
fixture work.  Human now explicitly approved one new window with
「新しい作業枠を承認します。」.  Record and exact limits:
docs/review-records/G6_WC02_BUDGET_AUTHORITY_2026-09-28.md.
The first delivery `G6-ACC-WC02-WINDOW-20260928-001` was retained fail-closed:
its PR trigger omitted `AUTHORITY_RECORD` and the Watcher recorded
`REQUEST_BINDING_MISMATCH_AUTHORITY_RECORD`.  Do not alter that fixed request.
Replacement `G6-ACC-WC02-WINDOW-20260928-002` carries the complete same binding;
await its full matching positive response before patching.  It is the same
decision `AUTH-G6-WC02-WINDOW-20260928-001`, not a new request to the human.
The new window is 30 ACTIVE_WORK minutes, starts at zero only after that response,
and retains the existing 0/2 fixture cap and WC-02 scope.

The Watcher child may only record this window-review checkpoint (NO_REPORT on a
positive result); it is not the approved source-writing actor and must not retry
the failed WC-02 patch.  Foreground chat owns the minimal implementation and
fresh-process fixtures after review.  No service reload, Day/model work, new host
writer, or broader card begins.  The old budget HUMAN_REQUIRED entry lacks
`REVIEWED_COMMIT`; retain it as unbound historical bookkeeping without inventing
confirmation or hand-editing state.  Maintenance replies below remain limited to
their own IDs and cannot start WC-02.

## Current maintenance — reviewer progress visibility (2026-09-28)

Human requested visible progress in the dashboard or this chat. Implement a read-only
dashboard projection of the existing persisted report registry: pending/human waits,
processed records, exact file/comment links, timestamps, and explicit stale/offline
status. Record: docs/review-records/REVIEWER_PROGRESS_UI_2026-09-28.md.
No Watcher/state contract changes, restart, replay, chat automation or WC02 work.
On acceptance of REVIEWER-PROGRESS-UI-20260928-001, record this UI-only checkpoint
and return NO_REPORT; do not begin another stage. Concrete review gaps remain scoped
to this display. Older maintenance instructions below are historical checkpoints.

## Current maintenance — file-response reader (2026-09-28)

### Live deployment verified; completion-review checkpoint

Human said "おけ。進めてください。" after the full matching file review
FILE-WATCHER-REVIEW-20260928-001 (blob 3a1919ad241691cb8a751cab91b021e9a8be4fae)
requested deployment of 5447427 and live/restart verification. The foreground
chat completed these checks: the review was APPLIED by one live continuation;
FILE-WATCHER-LIVE-ACK-20260928-001 became ACKNOWLEDGED without another continuation;
both survived a restart without replay. Evidence is appended to the record below.
Completion report FILE-WATCHER-DEPLOYMENT-20260928-001 requests acceptance of this
transport maintenance only. On matching acceptance, read the full response and
record only that bounded checkpoint with `{"action":"NO_REPORT"}`; no duplicate
deployment/test/report, WC02, or next-stage start. Rejection must retain its concrete
gap. Do not edit watcher state by hand. The separate WC02 authority wait remains.

Human requested design and implementation of file replies in the existing Watcher.
Scope/DoD: explicitly opted-in new reports, immutable request/response correlation,
pending-file revalidation, informational ACK without execution, restart deduplication
and unchanged legacy comment handling. Record:
`docs/review-records/FILE_RESPONSE_WATCHER_2026-09-28.md`.
Deliver source and focused fixture evidence for review. Runtime reload and live
continuation are separate deployment evidence, not implied by passing fixtures.
WC02's host source-write/reload handoff remains HUMAN_REQUIRED under reply
5867480489; this transport implementation does not grant that authority.

## Active engineering work — G6 dependency-order continuation (2026-09-28)

- Latest human instruction: 「では設計変更し、進めてください。」
  Decision `AUTH-G6-CONTINUITY-20260928-001`, recorded in
  `docs/review-records/G6_CONTINUITY_2026-09-28.md`.
- Objective: implement the approved G5 cards serially within G6. G4 §14,
  G5 §3.1 and WORKING_RULES define card selection and genuine stop boundaries.
- Current checkpoint: continuity design is being submitted under
  `G6-ACC-CONTINUITY-20260928-001`; await its matching response after publication.
  On positive review, consume WC-01 evidence at bf34b6a and review 5865174825,
  then begin WC-02 admission/preflight using isolated fixtures. Do not stop merely
  after recording the design review. If prerequisites fail, repair within the card.
- WC-02 DoD: contract/scope/Git/permission/budget/external-prerequisite admission
  returns only permitted preflight or a recorded blocker; no Day execution.
  Evidence must show valid admission and dirty Git, unknown permission, missing
  limits and contract mismatch rejection. See G5 WC-02 for target files/limits.
- On each card: record evidence, next eligible card and remaining work-window
  time/retry limits; retain the 30-minute ACTIVE_WORK ceiling and two-attempt cap.
  Review response resets only the progress counter, not the work-window budget.
- Stage DoD: G5 implementation cards have traceable evidence and required review;
  separately report VC-11 actor evidence and unavailable product-E2E inputs.
  G6 completion requires its own completion review. G7/G8 and product Day/Go,
  models, service operations, authentication and paid work remain separate.

## Historical checkpoint — chat approval handoff (2026-09-28)

The following card-only stopping instructions describe the earlier completed
handoff. The later continuity decision above supersedes their WC-02 restriction;
their original records and already-applied responses must not be replayed.

Human selected approval completion in this chat and instructed 「対応してください。」.
Apply WORKING_RULES' chat approval policy, deploy the bounded confirmation handling,
and send existing G6 authority for Reviewer confirmation under a new report ID.
Canonical decision/progress record: `docs/review-records/CHAT_APPROVAL_HANDOFF_2026-09-28.md`.
No repeat human approval is needed solely for cross-chat visibility. Once the
confirmation reply is applied, record only its allowed effect and the WC-01
review result. If necessary, reconcile the older G5 authority wait through the
same new-report confirmation path using its existing direct approval/acceptance
evidence; do not request a fresh human approval or replay its old response.
This maintenance authorizes reloading the existing local watcher implementation;
it does not select WC-02, another Day, or authorize model execution.

## Historical checkpoint — G6 WC-01 (2026-09-28)

- Human instruction: 「Reviewerからの返信を確認後、G6を開始してください。」
- G5 exit: accepted only at `4a2b7a2269adae8318903179b10bb59ef424b145`,
  Reviewer response `5864471565` to `G5-ACC-REVIEW-20260928-004`.
- Active card: G5 v2 WC-01, RunIntent/RunControl contract and isolated JSON tests.
  Select the first implementation card in the approved dependency order; reuse the
  already published WC-00 baseline. This is not permission to execute a Day.
- Record, scope, evidence and checkpoint:
  `docs/review-records/G6_WC01_2026-09-28.md`.
- DoD: versioned JSON round-trip, duplicate ID rejection, identity mismatch
  rejection, current/history separation, legacy snapshot compatibility.
- Stop: WC-01 implementation/self-check done, awaiting its matching review;
  do not automatically start WC-02, G7/G8, services, models or a research Day.

## Product operation (unchanged; no selected Day)

- **System:** AI Control Center Day Runner v1
- **Current scenario:** LocalLLM-Lab Day 1-14
- **Authoritative scenario/runbook:**
  `C:\LocalLLM-Lab\docs\runbooks\work-plan-day1-14.md`
- **Interaction model:** The human selects one Day and presses **Go**. Control
  Center executes that selected Day autonomously under
  the externally frozen `docs/DAY_RUNNER_EXECUTION_SPEC.md` and stops only at selected-Day completion
  or a genuine human/external authority boundary.
- **Active task-specific DoD:** The selected Day Contract and its completion
  criteria. Until a Day is selected, there is no active Day-specific DoD.
- **Next operational action:** Wait for the human to select a Day.

Do not automatically advance to another Day, implement scenario switching, or
start Day 5 or another research Day because maintenance work has finished.
