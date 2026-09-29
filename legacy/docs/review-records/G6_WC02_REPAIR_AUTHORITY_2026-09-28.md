# G6 WC-02 bounded repair authority

- Decision ID: `AUTH-G6-WC02-REPAIR-20260928-001`
- Decision maker: 広瀬剛
- Source: `RECORDED_DIRECT_CONVERSATION`, this Codex chat
- Exact human instruction: 「１．で進めてください。」
- Context: the immediately preceding two choices were (1) a named limited
  ratification of the actual edit route, bounds repair, and one new focused
  fixture attempt, or (2) leaving WC-02 unaccepted and stopped.

## Bounded authorization

The authorized editor is the existing foreground Codex actor operating in
`C:\AI-Control-Center`, using `apply_patch` for reviewed source/test changes and
explicit-path Git commit/push through the existing host-approved repository
workflow. This does not authorize the Watcher child, `codex exec` child, a new
writer, a service reload, or a new authentication mechanism.

Authorize only the following WC-02 repair:

1. reject a `RunIntent` whose `requested_limits.active_work_seconds` exceeds
   1800, or whose `requested_limits.max_attempts` exceeds 2, with a concrete
   fail-closed admission blocker;
2. add only the focused boundary assertions needed for those two rejects; and
3. run one fresh-process focused fixture command once after the patch.

The work window is the normal 30 minutes of ACTIVE_WORK, with exactly one new
fixture attempt for this repair and zero cost. The prior WC-02 fixture tally
remains historical evidence and is not relabelled or reused as the new attempt.

No Day selection/Go, model invocation, service operation, credential change,
external product operation, paid work, destructive Git action, WC-03, or wider
policy/config/UI change is authorized. Stop after the one focused fixture result
and deliver the fixed commit for review.

## Standard and design consistency check

- Standard checked: `HIPVG/ai_work_operating_standard@13065155999b799fdd2766630696d523fc53beaf` (2026-09-28). Section 5.0 routes a defect through observed failure/cause hypothesis, a minimum repair plan, G6 implementation, G7 verification, and G8 acceptance. Section 2 requires deterministic limit checks and returns the affected result to unverified on correction.
- Observed failure: the fixed WC-02 implementation commit `12176209ffe27c6ae5cf18c85865fb238c801986` checks requested limits for positivity but does not reject values over 1800 seconds or two attempts. The Reviewer response file for `G6-ACC-WC02-IMPLEMENTATION-20260928-001` identifies this gap; its 16 focused passes do not cover these two upper-bound rejects, and the second test outcome remains unobserved.
- Design mapping: G4 v2 section 3 assigns deterministic time/attempt/cost preflight to the machine checker; section 6 requires the server-owned limit check to stop new action on an exceeded bound. G5 v2 section 3.1 fixes 30 active minutes, two target validation attempts, and zero cost; WC-02 requires deterministic time/attempt/cost preflight and forbids overlimit admission.
- Judgment: this is a failure to implement the existing G4/G5 contract, not a new functional contract or state transition. Do not revise those design baselines solely to restate the defect. This record and CURRENT_WORK carry the authorized correction and its traceability. Keep WC-02 unverified until the bounded fixture and matching review; neither the historical 16 passes nor this human decision establishes G7/G8 acceptance.
- Provenance limit: this chat records the human's limited ratification of the named foreground route. It is an operational authority record, not independent forensic proof of which process wrote every byte of the earlier commit. If that historical actor identity remains decision-critical, preserve it as unknown rather than inventing evidence.
