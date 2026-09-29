# File response reader: design and verification record

Authority: human in this Codex chat requested design and implementation of file
response reading by the existing Watcher. This does not authorize the separate
WC02 host source-write/reload handoff. Implementation baseline: 4ac5550.

## Before implementation

Current source: backend/control/reviewer_bus.py uses gh issue comments, JSON
report_registry, one cycle lock, pending-response revalidation, then bounded
Codex exec and envelope validation. backend/app.py constructs the watcher at
startup. Persisted state reports polling and no pending continuation; this is
not proof of the in-memory source revision. No hot reload is evidenced, so
this delivery is source plus isolated tests, not a claim of live deployment.
Existing desktop apply_patch and GitHub read/write worked during the PoC;
this does not establish the separate sandboxed WC02 actor's write capability.
Unrelated runtime.yaml and history changes must be preserved.

Actors: Codex publishes an immutable request file and trigger comment; Reviewer
writes a response file on the fixed Review Bridge PR branch; Watcher reads via
existing gh authentication and validates, persists and dispatches; Codex applies
the full reply within recorded authority. Human retains genuine scope decisions.
No new service, credentials, model API or host write/reload mechanism is added.

Contract: explicit RESPONSE_TRANSPORT: github_file on a NEW report selects files.
Legacy comments and FILE_REVIEW_REQUEST PoCs are not silently migrated. Pin
REQUEST_COMMIT (40 hex), REQUEST_PATH=poc/file-review-requests/<REPORT_ID>.md and
RESPONSE_PATH=poc/file-review-responses/<REPORT_ID>.md. Safe IDs contain only
letters, digits, underscore and hyphen. Fetch the same-repository PR head SHA
on branch poc/review-loop-report-types-20260924, then read contents at that SHA;
request contents are read at REQUEST_COMMIT. No checkout or executable file is
loaded. Bound text size, reject non-file objects and validate Git blob hashes.

Compare notification/request control fields and response IN_REPLY_TO,
REQUEST_COMMIT, REQUEST_PATH, RESPONSE_KIND=GIT_FILE, unique RESULT and NEXT_ACTION.
Persist transport and request binding on first sight; changed bindings fail closed.
File mode never consumes a comment reply. Store response source, head, path and
blob separately from comment IDs. Re-fetch before retry; missing, changed or
mismatched pending content is invalidated with retained evidence and no execution.
Successful apply is deduplicated across restart by the existing report registry.
File confirmation IDs use their explicit file identity, not fabricated comment IDs.

Happy path: notify -> bind request -> read file at PR head -> exact validation ->
RESPONSE_RECEIVED -> APPLYING -> existing envelope handling -> APPLIED. An optional
ACK goes directly to ACKNOWLEDGED without Codex. One continuation runs per cycle.
Failure: absent response waits; network/permission/malformed data record a per-report
error; changed pending file is quarantined; wrong binding/result never starts Codex
or posts. CONTINUATION_FAILED is not APPLIED. Existing confirmation/HUMAN_REQUIRED
rules continue to apply. Errors on one file request need not block another request.
After uncertain process interruption exactly-once external effects are not claimed;
this change preserves existing continuation semantics, not a transactional executor.

Rollback: keep comment mode for new reports; retain file history and outstanding
bindings. Do not reinterpret a pending file report as a comment report. Deployment
requires an explicitly verified reload of the existing process; not performed by
this source/test change. Terminal evidence: focused fixture state transitions and
legacy regression, plus separately identified read-only real GitHub adapter probe.

## Implementation evidence

Implemented reader in backend/control/reviewer_files.py and integrated it with
ReviewerBusWatcher. Pending file identity is checked both against persisted request
state and freshly fetched contents. Invalidated pending state is quarantined;
file provenance never masquerades as a numeric comment ID. Existing registrations
retain their persisted mode; old PoCs are not silently promoted into execution
requests. A registry written by the old watcher without a transport field derives
the mode once from the explicit RESPONSE_TRANSPORT marker at upgrade; this allows
review requests published before deployment to be consumed after loading the reader.

Focused command: `python -m pytest tests/test_reviewer_files.py
tests/test_reviewer_bus.py tests/test_reviewer_confirmation.py -q -p no:cacheprovider`.
Result: 51 passed. Tests assert APPLIED once across restart, ACK with zero Codex
calls, field/hash/type/size failures with zero calls, missing reply waiting,
pending deletion/edit/mismatch quarantine, network error preserving pending,
unchanged failed pending retry, corrupted pending IDs not executing another reply,
independent comment handling, legacy PoC nonmigration and confirmation resolution.
One fixture initially treated a historical first-load request as active; adjusted
the scenario to register F1 before publishing C1, preserving the no-replay rule.

Read-only production reader probe fetched PoC003 at PR head
b9f2629cae7d6647779d67d87aaf522d5dcec259 and verified blob
e77ecb153e92e5f6d9883ccdcb698b08e2227661, size and UTF-8 body. This used only
GitHub GETs, not run_once, state writes or Codex. PoC003 predates opt-in and is
not eligible for automatic migration. Live Watcher reload/continuation remains
NOT_VERIFIED. No WC02 patch or host handoff implementation was performed.

## New request template

Commit the following control fields plus review evidence as an immutable request
file. The trigger carries the same fields and adds REQUEST_COMMIT with that SHA.
The Reviewer commits its reply on the existing PR branch; the Watcher reads it.

```
REPORT_ID: <new safe ID>
REPORT_TYPE: PROGRESS_UPDATE
RESPONSE_REQUIRED: yes
RESPONSE_TRANSPORT: github_file
REQUEST_PATH: poc/file-review-requests/<new safe ID>.md
RESPONSE_PATH: poc/file-review-responses/<new safe ID>.md
```

Echo REVIEWED_COMMIT and all confirmation/authority control fields identically
between the request and notification when present. The response format is
IN_REPLY_TO, RESULT, REQUEST_COMMIT, REQUEST_PATH, RESPONSE_KIND: GIT_FILE and
NEXT_ACTION; authority confirmations additionally echo existing binding fields.
Request creation stays with the publisher; the reader does not synthesize an
immutable request from a mutable comment. New continuations must not claim file
mode without first publishing such a request; their default remains comment mode.

## Live deployment and restart verification (2026-09-28)

Authority: human "おけ。進めてください。" after full matching CONTINUE to
FILE-WATCHER-REVIEW-20260928-001. Action class VALIDATION. The earlier
NOT_VERIFIED statement describes the implementation checkpoint, not current status.
Only the existing local app/Watcher was reloaded; no WC02 or host-write handoff.

Process evidence: old Uvicorn PID 19400 was checked, with no pending continuation,
then replaced at 19:15:17 JST by PID 13784. Local diagnostic launcher
state/file-watcher-live-launch.py checks the two source files against git show
544742769367d9d64fc1271ba0ebfb3671451af3 before importing the existing app unchanged.
It logged the loaded module/backend method fingerprint; no runtime monkeypatch.
Restart-deduplication check replaced PID 13784 with PID 7516 at 19:21:57 JST.
Both load logs report the same baseline and hashes:

| Evidence | SHA-256 |
| --- | --- |
| reviewer_bus.py, LF-normalized | 61a4a9498d47fcbf83c45f1866ebb02770acf97fe71863742cd110f5a509f600 |
| reviewer_files.py, LF-normalized | 1913eeed4740b6008c4c68f0710af3be6426906036f523ce8bd43dd11bf95757 |
| loaded _sync_file_responses code, marshal | cd603932d81b6c20658d894289d9369f1c97b9143e6090863b2151ea3ce138e7 |

Local raw logs: state/file-watcher-live-{1,2}.{stdout,stderr}.log (not committed).
Startup succeeded, binding 127.0.0.1:8000; poll_seconds=120.

Immutable Bridge provenance (all paths relative to HIPVG/AI-Control-Center-Review-Bridge):

| REPORT_ID | Request commit | Response observed head / blob |
| --- | --- | --- |
| FILE-WATCHER-REVIEW-20260928-001 | 9a0be015e7e3283b976ddd810d16f7826fd590d6 | f25976a60f01f6892acfcc25b133973da1419395 / 3a1919ad241691cb8a751cab91b021e9a8be4fae |
| FILE-WATCHER-LIVE-ACK-20260928-001 | d380f33062aa4a0035851af1d22b3ffb5ff71306 | 8eb3b90d2ffc14f75d181139568cc5a5efebef3e / 7300f4bd45361bf94a856678d5d603e4b2685d45 |

Exact request paths: poc/file-review-requests/<REPORT_ID>.md; response paths:
poc/file-review-responses/<REPORT_ID>.md for the two IDs above. Stored response
identity is git-file:<blob>:<response path>. Trigger comments 5867792668 and
5867964685 respectively. The second immutable request/notification was a live
test stimulus; foreground validation observed only local status afterwards,
not PR response polling. Reviewer created its six-field ACK (NEXT_ACTION: none).

Observed state transitions, not merely successful exit codes:

- Existing positive review: WAITING_RESPONSE -> APPLYING at 19:15:22 JST ->
  APPLIED at 2026-09-28T10:16:00.273770+00:00. Exactly one observed live
  continuation finished at 10:16:00.267787+00:00, exit 0, action NO_REPORT.
  Foreground ownership handoff prevented duplicate deployment by that continuation.
- New optional ACK: NOT_REQUIRED at 19:17:22 JST -> ACKNOWLEDGED at 19:19:22 JST;
  continuation timestamp stayed unchanged (zero additional starts for ACK).
- HTTP registry and persisted state were structurally identical before restart.
  After restart and poll 2026-09-28T10:22:00.813794+00:00, both states, identities,
  applied timestamp and last continuation timestamp remained unchanged. Structural
  equality and timezone-aware instant assertions passed; pending and last_error
  were null, running/available true. No state file was manually rewritten.

Two diagnostic comparisons initially failed on representation, not runtime state:
JSON property order and PowerShell's UTC-to-JST/decimal formatting. Full records
were inspected and comparison corrected to JSON structure and timezone-aware
instants; these checks passed. The first guard stopped before process shutdown.
Unchanged 51-test source evidence was reused, not rerun. Design contract unchanged.
ARTIFACT_QUALITY_CHECK: PASS for this bounded transport deployment: provenance,
state transitions, scope and replay check are traceable. This is not exactly-once
proof under arbitrary crashes, OS-login startup proof, G6 completion, or product E2E.
No Day/model/credential/paid actions, WC02 source edit or new host handoff occurred.
Next: completion review of this maintenance only; WC02 remains HUMAN_REQUIRED.
