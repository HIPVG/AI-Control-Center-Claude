# G6 WC-02 bounded limits repair

- Action class: `IMPLEMENTATION`, followed by `VALIDATION`
- Human continuation: 「じゃ、新体制で続行しましょう。WC02の追加試験からだっけ？」
- Existing authority: `AUTH-G6-WC02-REPAIR-20260928-001`
- Scope: `backend/control/day_git.py` and `tests/test_day_admission.py`
- Cost: zero

## Implemented change

`day_admission()` now rejects a `RunIntent` before Git inspection when
`requested_limits.active_work_seconds > 1800` or
`requested_limits.max_attempts > 2`. The result is fail-closed:
`BLOCKED`, `HUMAN_ACTION_REQUIRED`, and
`LIMITS_EXCEED_APPROVED_BOUND`. The focused fixture checks both boundary
violations and verifies that the repository content is not changed.

## Fresh-process fixture attempt

Exactly one newly authorized command was run:

```text
python -B -m pytest tests/test_day_admission.py -q --basetemp .pytest-wc02-bounded-repair-attempt1
```

Observed result: collection stopped with `NameError: name 'pytest' is not
defined` at the new `@pytest.mark.parametrize` declaration. No WC-02 assertion
executed. This is classified as a test-harness defect, not evidence that the
admission implementation passed or failed.

The missing import was then added. The command was not rerun because the direct
authority allowed exactly one new focused fixture attempt. WC-02 therefore
remained unverified at that checkpoint.

## Additional validation authorized by the human

The human then explicitly instructed 「許可します。実行して下さい。」. One
additional fresh-process focused command was run:

```text
python -B -m pytest tests/test_day_admission.py -q --basetemp .pytest-wc02-bounded-repair-attempt2
8 passed in 4.06s
```

Both new over-limit cases and the existing admission cases passed. The two new
cases confirm that rejection occurs before Git inspection and does not change
the fixture repository. This verifies the bounded repair at deterministic
fixture level; it does not prove an actual Day, UI Go, effective production
permission, or product acceptance.

No Day selection/Go, model invocation, service operation, credential change,
external reviewer delivery, paid work, destructive Git action, WC-03, G7, or G8
was performed.
