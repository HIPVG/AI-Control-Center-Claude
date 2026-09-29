import json
import subprocess
import pytest
from pathlib import Path

from backend.control.reviewer_bus import ReviewerBusWatcher


def _comment(comment_id: int, body: str) -> dict[str, object]:
    return {"id": comment_id, "body": body, "created_at": "2026-09-24T00:00:00Z"}


def _comments_output(comments: list[dict[str, object]]) -> str:
    return "\n".join(json.dumps(item) for item in comments)


def _envelope(report_id="R1-NEXT"):
    body = f"REPORT_ID: {report_id}\nREPORT_TYPE: PROGRESS_UPDATE\nCURRENT_ACTION: bounded acknowledgement"
    return json.dumps({"action": "POST_REPORT", "report_id": report_id, "report_type": "PROGRESS_UPDATE", "body": body})


def _watcher(tmp_path, runner, monkeypatch):
    watcher = ReviewerBusWatcher(tmp_path, codex_executable="codex", command_runner=runner)
    monkeypatch.setattr(watcher, "_resolve_executable", lambda value: value)
    monkeypatch.setattr(watcher, "_set_state", lambda **changes: watcher._state.update(changes))
    return watcher


def test_matching_response_posts_envelope_and_sets_new_outstanding(tmp_path: Path, monkeypatch):
    comments = [_comment(10, "REPORT_ID: R1\nREPORT_TYPE: PROGRESS_UPDATE"), _comment(11, "IN_REPLY_TO: R1\nRESULT: CONTINUE")]
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "gh" and "--method" not in argv:
            return subprocess.CompletedProcess(argv, 0, _comments_output(comments), "")
        if argv[0] == "codex":
            assert "Do not access GitHub" in argv[-1]
            return subprocess.CompletedProcess(argv, 0, _envelope(), "")
        assert "--method" in argv
        return subprocess.CompletedProcess(argv, 0, json.dumps({"id": 12}), "")

    result = _watcher(tmp_path, runner, monkeypatch).run_once()

    assert result["last_applied_response_comment_id"] == 11
    assert result["outstanding_report_id"] == "R1-NEXT"
    assert result["last_delivery_comment_id"] == 12
    assert len([call for call in calls if call[0] == "codex"]) == 1
    assert len([call for call in calls if "--method" in call]) == 1


def test_exit_zero_without_payload_is_not_delivery_success(tmp_path: Path, monkeypatch):
    comments = [_comment(20, "REPORT_ID: R2\nREPORT_TYPE: PROGRESS_UPDATE"), _comment(21, "IN_REPLY_TO: R2\nRESULT: CONTINUE")]

    def runner(argv, **kwargs):
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, _comments_output(comments), "")
        return subprocess.CompletedProcess(argv, 0, "not an envelope", "")

    result = _watcher(tmp_path, runner, monkeypatch).run_once()

    assert result["last_codex_exit_code"] == 0
    assert result["last_error"] == "CODEX_CONTINUATION_OUTPUT_INVALID"
    assert result.get("last_applied_response_comment_id") is None
    assert result.get("last_delivery_comment_id") is None


def test_current_mismatch_is_recorded_without_post_or_continuation(tmp_path: Path, monkeypatch):
    comments = [_comment(30, "REPORT_ID: R3\nREPORT_TYPE: PROGRESS_UPDATE"), _comment(31, "IN_REPLY_TO: OTHER\nRESULT: CONTINUE")]
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, _comments_output(comments), "")

    watcher = _watcher(tmp_path, runner, monkeypatch)
    result = watcher.run_once()

    assert result["last_error"] == "REVIEW_RESPONSE_CORRELATION_MISMATCH"
    assert result["outstanding_report_id"] == "R3"
    assert result["last_invalidated_response"] == {
        "response_comment_id": 31,
        "pending_response_report_id": None,
        "expected_report_id": "R3",
        "actual_in_reply_to": "OTHER",
        "reason": "UNMATCHED_RESPONSE_IN_REPLY_TO_MISMATCH",
        "invalidated_at": result["last_invalidated_response"]["invalidated_at"],
    }
    assert all(call[0] != "codex" for call in calls)
    assert all("--method" not in call for call in calls)


def test_known_old_response_is_silently_ignored(tmp_path: Path, monkeypatch):
    comments = [_comment(40, "REPORT_ID: R4\nREPORT_TYPE: PROGRESS_UPDATE"), _comment(41, "IN_REPLY_TO: OLD\nRESULT: CONTINUE")]
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, _comments_output(comments), "")

    watcher = _watcher(tmp_path, runner, monkeypatch)
    watcher._state["processed_report_ids"] = ["OLD"]
    watcher.run_once()

    assert all("--method" not in call and call[0] != "codex" for call in calls)


def test_failed_continuation_keeps_response_pending_for_retry(tmp_path: Path, monkeypatch):
    comments = [_comment(50, "REPORT_ID: R5\nREPORT_TYPE: PROGRESS_UPDATE"), _comment(51, "IN_REPLY_TO: R5\nRESULT: CONTINUE")]
    attempts = 0

    def runner(argv, **kwargs):
        nonlocal attempts
        if argv[0] == "gh" and "--method" not in argv:
            return subprocess.CompletedProcess(argv, 0, _comments_output(comments), "")
        if argv[0] == "codex":
            attempts += 1
            return subprocess.CompletedProcess(argv, 1 if attempts == 1 else 0, _envelope(), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"id": 52}), "")

    watcher = _watcher(tmp_path, runner, monkeypatch)
    failed = watcher.run_once()
    succeeded = watcher.run_once()

    assert failed["last_error"] == "CODEX_CONTINUATION_FAILED"
    assert failed["pending_response_comment_id"] == 51
    assert succeeded["last_applied_response_comment_id"] == 51
    assert attempts == 2


def test_stale_pending_response_is_invalidated_without_continuation_or_post(tmp_path: Path, monkeypatch):
    comments = [
        _comment(60, "REPORT_ID: G5-ACC-REVIEW-20260928-004\nREPORT_TYPE: PROGRESS_UPDATE"),
        _comment(61, "IN_REPLY_TO: G5-ACC-REVIEW-20260928-003\nRESULT: REJECT"),
        _comment(62, "REPORT_ID: G5-ACC-WATCHER-CORRELATION-20260928-AAF1EDD\nREPORT_TYPE: PROGRESS_UPDATE"),
    ]
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, _comments_output(comments), "")

    watcher = _watcher(tmp_path, runner, monkeypatch)
    watcher._state.update(
        pending_response_comment_id=61,
        pending_response_body=comments[1]["body"],
        pending_response_report_id="G5-ACC-REVIEW-20260928-004",
        last_report_id="G5-ACC-REVIEW-20260928-004",
    )

    result = watcher.run_once()

    assert result["pending_response_comment_id"] is None
    assert result["pending_response_body"] is None
    assert result["pending_response_report_id"] is None
    assert result["outstanding_report_id"] == "G5-ACC-REVIEW-20260928-004"
    assert result["outstanding_report_comment_id"] == 60
    assert result["last_error"] == "REVIEW_RESPONSE_CORRELATION_MISMATCH"
    assert result["last_invalidated_response"] == {
        "response_comment_id": 61,
        "pending_response_report_id": "G5-ACC-REVIEW-20260928-004",
        "expected_report_id": "G5-ACC-REVIEW-20260928-004",
        "actual_in_reply_to": "G5-ACC-REVIEW-20260928-003",
        "reason": "PENDING_RESPONSE_IN_REPLY_TO_MISMATCH",
        "invalidated_at": result["last_invalidated_response"]["invalidated_at"],
    }
    assert result.get("last_applied_response_comment_id") is None
    assert all(call[0] != "codex" for call in calls)
    assert all("--method" not in call for call in calls)


def test_pending_state_report_id_mismatch_is_traceable_and_not_continued(tmp_path: Path, monkeypatch):
    comments = [_comment(65, "REPORT_ID: R6\nREPORT_TYPE: PROGRESS_UPDATE"), _comment(66, "IN_REPLY_TO: R6\nRESULT: CONTINUE")]
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, _comments_output(comments), "")

    watcher = _watcher(tmp_path, runner, monkeypatch)
    watcher._state.update(
        pending_response_comment_id=66,
        pending_response_body=comments[1]["body"],
        pending_response_report_id="R6-OLD",
        outstanding_report_id="R6",
    )

    result = watcher.run_once()

    assert result["pending_response_comment_id"] is None
    assert result["outstanding_report_id"] == "R6"
    assert result["last_error"] == "REVIEW_RESPONSE_CORRELATION_MISMATCH"
    assert result["last_invalidated_response"]["response_comment_id"] == 66
    assert result["last_invalidated_response"]["pending_response_report_id"] == "R6-OLD"
    assert result["last_invalidated_response"]["expected_report_id"] == "R6"
    assert result["last_invalidated_response"]["actual_in_reply_to"] == "R6"
    assert result["last_invalidated_response"]["reason"] == "PENDING_RESPONSE_REPORT_ID_MISMATCH"
    assert all(call[0] != "codex" for call in calls)
    assert all("--method" not in call for call in calls)


def test_exactly_matching_pending_response_continues_once(tmp_path: Path, monkeypatch):
    comments = [_comment(70, "REPORT_ID: R7\nREPORT_TYPE: PROGRESS_UPDATE"), _comment(71, "IN_REPLY_TO: R7\nRESULT: CONTINUE")]
    attempts = 0

    def runner(argv, **kwargs):
        nonlocal attempts
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, _comments_output(comments), "")
        attempts += 1
        return subprocess.CompletedProcess(argv, 0, json.dumps({"action": "NO_REPORT"}), "")

    watcher = _watcher(tmp_path, runner, monkeypatch)
    watcher._state.update(
        pending_response_comment_id=71,
        pending_response_body=comments[1]["body"],
        pending_response_report_id="R7",
        outstanding_report_id="R7",
    )

    first = watcher.run_once()
    second = watcher.run_once()

    assert first["last_applied_response_comment_id"] == 71
    assert first["pending_response_comment_id"] is None
    assert first["outstanding_report_id"] is None
    assert second["last_applied_response_comment_id"] == 71
    assert attempts == 1


def test_loop_keeps_cadence_after_long_continuation(tmp_path: Path, monkeypatch):
    watcher = ReviewerBusWatcher(tmp_path, poll_seconds=120)
    timeline = iter([0.0, 150.0])
    monkeypatch.setattr("backend.control.reviewer_bus.monotonic", lambda: next(timeline))

    class StopOnce:
        def __init__(self):
            self.checks, self.waits = 0, []

        def is_set(self):
            self.checks += 1
            return self.checks > 1

        def wait(self, timeout):
            self.waits.append(timeout)

    watcher._stop = StopOnce()
    watcher.run_once = lambda: {}
    watcher._loop()
    assert watcher._stop.waits == [0.0]


def test_offline_binding_recovery_survives_restart_and_applies_only_target_once(tmp_path):
    comments = [_comment(10, "REPORT_ID: TARGET\nREPORT_TYPE: PROGRESS_UPDATE"),
                _comment(20, "REPORT_ID: ANCILLARY\nREPORT_TYPE: PROGRESS_UPDATE"),
                _comment(30, "IN_REPLY_TO: TARGET\nRESULT: HUMAN_REQUIRED"),
                _comment(31, "IN_REPLY_TO: ANCILLARY\nRESULT: CONTINUE")]
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "gh":
            assert "--method" not in argv
            return subprocess.CompletedProcess(argv, 0, _comments_output(comments), "")
        assert "REPORT_ID: TARGET" in argv[-1]
        assert "Receipt only; no product work." in argv[-1]
        return subprocess.CompletedProcess(argv, 0, '{"action":"HUMAN_REQUIRED"}', "")

    watcher = ReviewerBusWatcher(tmp_path, command_runner=runner)
    old_error = {"response_comment_id": 3, "actual_in_reply_to": "OLD"}
    watcher._set_state(outstanding_report_id="ANCILLARY", outstanding_report_comment_id=20,
                       last_error="REVIEW_RESPONSE_CORRELATION_MISMATCH", last_invalidated_response=old_error)
    watcher.recover_outstanding_report(comments, expected_current="ANCILLARY", target_report_id="TARGET",
        target_comment_id=10, authority="Explicit human repair request", continuation_scope="Receipt only; no product work.")
    restarted = ReviewerBusWatcher(tmp_path, command_runner=runner)
    restarted._resolve_executable = lambda value: value
    assert restarted.status()["last_binding_recovery"]["previous_invalidated_response"] == old_error
    assert restarted.status()["outstanding_report_id"] == "TARGET"
    assert restarted.status().get("last_applied_response_comment_id") is None
    first = restarted.run_once()
    second = restarted.run_once()
    assert first["last_applied_response_comment_id"] == 30
    assert first["last_continuation_action"] == "HUMAN_REQUIRED"
    assert second["outstanding_report_id"] is None
    assert second["processed_report_ids"] == ["TARGET"]
    assert second["non_controlling_report_ids"] == ["ANCILLARY"]
    assert len([c for c in calls if c[0] == "codex"]) == 1
    assert len(second["binding_recovery_history"]) == 1


@pytest.mark.parametrize("current,target_comment,processed,error", [
    ("CHANGED", 10, [], "RECOVERY_CURRENT_TARGET_CHANGED"),
    ("ANCILLARY", 999, [], "RECOVERY_TARGET_NOT_CONFIRMED"),
    ("ANCILLARY", 10, ["TARGET"], "RECOVERY_TARGET_ALREADY_APPLIED"),
])
def test_binding_recovery_refuses_changed_missing_or_applied_target(tmp_path, monkeypatch, current, target_comment, processed, error):
    watcher = _watcher(tmp_path, lambda *a, **kw: pytest.fail("No external effects allowed"), monkeypatch)
    watcher._state.update(outstanding_report_id=current, processed_report_ids=processed)
    before = watcher.status()
    comments = [_comment(10, "REPORT_ID: TARGET\nREPORT_TYPE: PROGRESS_UPDATE"),
                _comment(20, "REPORT_ID: ANCILLARY\nREPORT_TYPE: PROGRESS_UPDATE")]
    with pytest.raises(ValueError, match=error):
        watcher.recover_outstanding_report(comments, expected_current="ANCILLARY", target_report_id="TARGET",
            target_comment_id=target_comment, authority="human", continuation_scope="receipt only")
    assert watcher.status() == before


def test_registry_keeps_continuous_requests_handles_out_of_order_and_no_reply_ack(tmp_path, monkeypatch):
    comments = [_comment(10, "REPORT_ID: 0001\nREPORT_TYPE: PROGRESS_UPDATE")]
    prompts = []
    def runner(argv, **kwargs):
        if argv[0] == "gh":
            assert "--method" not in argv
            return subprocess.CompletedProcess(argv, 0, _comments_output(comments), "")
        prompts.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, '{"action":"NO_REPORT"}', "")
    watcher = _watcher(tmp_path, runner, monkeypatch)
    watcher.run_once()
    comments.extend([_comment(20, "REPORT_ID: 0002\nREPORT_TYPE: PROGRESS_UPDATE\nRESPONSE_REQUIRED: no"),
                     _comment(30, "REPORT_ID: 0003\nREPORT_TYPE: PROGRESS_UPDATE")])
    watcher.run_once()
    assert set(watcher.status()["report_registry"]) == {"0001", "0002", "0003"}
    assert watcher.status()["report_registry"]["0002"]["state"] == "NOT_REQUIRED"
    comments.extend([_comment(40, "IN_REPLY_TO: 0003\nRESULT: CONTINUE"),
                     _comment(41, "IN_REPLY_TO: 0002\nRESULT: ACKNOWLEDGED")])
    third = watcher.run_once()
    assert third["report_registry"]["0003"]["state"] == "APPLIED"
    assert third["report_registry"]["0002"]["state"] == "ACKNOWLEDGED"
    assert [item["report_id"] for item in watcher._work_in_progress()] == ["0001"]
    # A delayed listing of an older reply ID must still be applied by report identity.
    comments.append(_comment(35, "IN_REPLY_TO: 0001\nRESULT: CONTINUE"))
    watcher.run_once()
    watcher.run_once()
    assert len(prompts) == 2
    assert "REPORT_ID: 0003" in prompts[0] and "REPORT_ID: 0001" in prompts[1]
    assert watcher._work_in_progress() == []
    assert set(watcher.status()["report_registry"]) == {"0001", "0002", "0003"}


def test_reply_with_multiple_targets_does_not_launch_continuation(tmp_path, monkeypatch):
    comments = [_comment(10, "REPORT_ID: R1\nREPORT_TYPE: PROGRESS_UPDATE"),
                _comment(11, "IN_REPLY_TO: R1\nIN_REPLY_TO: R2\nRESULT: CONTINUE")]
    def runner(argv, **kwargs):
        assert argv[0] == "gh" and "--method" not in argv
        return subprocess.CompletedProcess(argv, 0, _comments_output(comments), "")
    result = _watcher(tmp_path, runner, monkeypatch).run_once()
    assert result.get("last_applied_response_comment_id") is None
