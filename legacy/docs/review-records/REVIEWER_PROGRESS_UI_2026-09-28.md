# Reviewer progress visibility — 2026-09-28

Authority: human requested "ダッシュボードで見せるんでも良いし、このチャットで出すのでもいいけど、経過が見れるようにしておいてほしいなぁ".
Scope: read-only dashboard improvement, not chat delivery, Watcher control or WC02.

Before: the panel showed only global running/detail and five browser-memory events;
its fingerprint omitted file-response identity. Closing the page lost those events.
After: existing GET /api/reviewer-bus/status supplies per-report persisted snapshots.
Pending/human/failed/unknown/error entries are visible separately from processed
history, ordered by recorded update time. Report ID, transport, state, recorded
update/apply times, and fixed GitHub request/response links are shown. Missing times
are explicitly unknown. APPLIED is response application, NOT approval to start the
next stage; ACKNOWLEDGED is receipt without execution. No review result is invented
from APPLIED. Read the linked complete response for the actual decision/instructions.

Last successful browser fetch and last Watcher PR fetch are distinct. Errors,
stopped/unavailable worker and a PR fetch older than 2*poll_seconds+60 seconds are
explicit. A long continuation can delay the next poll; stale is not proof of death.
HTTP failures/timeouts keep the last rows but mark them unverified; recovery clears
that warning. Requests do not overlap. No rendering failure triggers execution.

Saved rows survive browser reopen via server persistence. A separately labelled
20-item page-session log shows only observed state/response/error changes; it is
NOT a complete durable transition audit and cannot reconstruct unobserved steps.
No extra database or backend history contract was introduced. This is the existing
read-only UI/API projection responsibility; G4/G5 execution contracts are unchanged.
No browser notification subscription or this-chat delivery is claimed.

Implementation: frontend/reviewer-status.js (pure projection), app.js (safe DOM,
GET refresh and failure state), index.html (top-level panel), style.css (bounded
history scroll). Existing Day controls unchanged. Text uses textContent; response
URLs require fixed GitHub origin plus a valid commit/path or numeric comment ID.

Verification:
- `node tests/test_reviewer_status.cjs`: 6 passed. Fresh-page persistent APPLIED and
  HUMAN_REQUIRED; waiting -> received -> applying -> applied, no repeated-poll
  duplicate; ACK vs execution; failed/unknown/file-error visibility; stale/stopped/
  error health; failed GET retains rows and recovery clears warning; literal untrusted
  text and invalid response URL rejection. No network in these fixtures.
- `node --test ...` first failed with spawn EPERM before tests. Direct execution of
  the same node:test file avoids worker spawn; no dependency/tooling installation.
- `python -m pytest tests/test_api.py::test_dashboard_is_the_single_local_llm_day_runner
  -q -p no:cacheprovider`: 1 passed, existing framework deprecation warnings only.
- Live in-app browser at http://127.0.0.1:8000/#review-progress: observed running,
  73 persisted records and 2 human waits; FILE-WATCHER-DEPLOYMENT-20260928-001
  APPLIED at 19:29:12 JST with response link pinned to 4419104e047a88803df72b41c3878bf9ba333f2a.
  Reload retained those rows. No Day/Go/control clicked. Static assets served by
  the existing process: no service restart or actual reply reapplication.

ARTIFACT_QUALITY_CHECK: PASS for the read-only visibility scope. No product E2E,
G6 completion, human-wait resolution, or complete historic transition log claimed.
Next: bounded UI review, then stop; WC02 remains separate.
