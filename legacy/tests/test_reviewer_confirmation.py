"""Human wait -> new confirmation -> review -> one continuation, on real JSON state."""
import json
import subprocess

import pytest

from backend.control.reviewer_bus import ReviewerBusWatcher


COMMIT = "a" * 40
BINDING = ("CONFIRMS_REPORT_ID: R1\nCONFIRMS_RESPONSE_ID: 11\n"
           f"DECISION_ID: D1\nREVIEWED_COMMIT: {COMMIT}\n")


def scenario(tmp_path, monkeypatch, *, reply=None, code=0, action="NO_REPORT"):
    comments = [
        {"id": 10, "body": f"REPORT_ID: R1\nREPORT_TYPE: PROGRESS_UPDATE\nREVIEWED_COMMIT: {COMMIT}"},
        {"id": 11, "body": "IN_REPLY_TO: R1\nRESULT: HUMAN_REQUIRED"},
        {"id": 20, "body": "REPORT_ID: R2\nREPORT_TYPE: PROGRESS_UPDATE\n" + BINDING +
         "AUTHORITY_RECORD: https://example.test/fixed-commit/decision.md"},
    ]
    calls = []
    def runner(argv, **kwargs):
        if argv[0] == "gh":
            assert "--method" not in argv, "Fixture must not post"
            return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(c) for c in comments), "")
        calls.append(argv[-1])
        return subprocess.CompletedProcess(argv, code, json.dumps({"action": action}), "")
    def build():
        watcher = ReviewerBusWatcher(tmp_path, command_runner=runner)
        monkeypatch.setattr(watcher, "_resolve_executable", lambda value: value)
        return watcher
    watcher = build()
    watcher._set_state(processed_report_ids=["R1"], report_registry={
        "R1": {"state": "HUMAN_REQUIRED", "report_comment_id": 10,
               "response_comment_id": 11, "reviewed_commit": COMMIT, "response_required": True}})
    watcher.run_once()
    assert watcher.status()["report_registry"]["R1"]["state"] == "HUMAN_REQUIRED"
    assert not calls  # publication alone does not resolve or run anything
    comments.append({"id": 21, "body": reply or
        "IN_REPLY_TO: R2\nRESULT: CONTINUE\nNEXT_ACTION: Record confirmed authority only\n" + BINDING})
    return build(), build, calls, comments


def test_confirmation_resolves_wait_once_across_restart_preserves_old_reply(tmp_path, monkeypatch):
    watcher, build, calls, comments = scenario(tmp_path, monkeypatch)
    watcher.run_once()
    old = watcher.status()["report_registry"]["R1"]
    assert old["state"] == "RESOLVED_BY_CONFIRMATION"
    assert old["response_comment_id"] == 11
    assert old["resolution_response_comment_id"] == 21
    assert old["resolved_by_report_id"] == "R2"
    assert old["resolution_decision_id"] == "D1"
    assert len(calls) == 1 and "AUTHORITY_RECORD:" in calls[0]
    restarted = build()
    restarted.run_once()
    assert len(calls) == 1 and restarted._work_in_progress() == []
    # Another confirmation for the same resolved decision is not executed.
    comments += [{"id": 30, "body": comments[2]["body"].replace("REPORT_ID: R2", "REPORT_ID: R3")},
                 {"id": 31, "body": comments[3]["body"].replace("IN_REPLY_TO: R2", "IN_REPLY_TO: R3")}]
    restarted.run_once()
    assert len(calls) == 1
    assert restarted.status()["report_registry"]["R3"]["confirmation_error"] == "CONFIRMATION_TARGET_NOT_WAITING"


@pytest.mark.parametrize("field,replacement", [
    ("CONFIRMS_REPORT_ID: R1", "CONFIRMS_REPORT_ID: OTHER"),
    ("CONFIRMS_RESPONSE_ID: 11", "CONFIRMS_RESPONSE_ID: 12"),
    ("DECISION_ID: D1", "DECISION_ID: D2"),
    (f"REVIEWED_COMMIT: {COMMIT}", "REVIEWED_COMMIT: " + "b" * 40),
])
def test_mismatched_confirmation_never_starts_continuation(tmp_path, monkeypatch, field, replacement):
    reply = "IN_REPLY_TO: R2\nRESULT: CONTINUE\nNEXT_ACTION: Record\n" + BINDING.replace(field, replacement)
    watcher, _, calls, _ = scenario(tmp_path, monkeypatch, reply=reply)
    watcher.run_once()
    assert not calls
    assert watcher.status()["report_registry"]["R1"]["state"] == "HUMAN_REQUIRED"
    assert watcher.status()["report_registry"]["R2"]["confirmation_error"] == "CONFIRMATION_REPLY_BINDING_MISMATCH"


@pytest.mark.parametrize("result,code,action", [("REJECT", 0, "NO_REPORT"),
    ("HUMAN_REQUIRED", 0, "HUMAN_REQUIRED"), ("CONTINUE", 1, "NO_REPORT"),
    ("CONTINUE", 0, "HUMAN_REQUIRED")])
def test_negative_or_failed_continuation_keeps_old_wait(tmp_path, monkeypatch, result, code, action):
    reply = f"IN_REPLY_TO: R2\nRESULT: {result}\nNEXT_ACTION: bounded handling\n" + BINDING
    watcher, _, calls, _ = scenario(tmp_path, monkeypatch, reply=reply, code=code, action=action)
    watcher.run_once()
    assert len(calls) == 1
    assert watcher.status()["report_registry"]["R1"]["state"] == "HUMAN_REQUIRED"


def test_confirmation_for_changed_commit_is_rejected_even_when_reply_echoes_it(tmp_path, monkeypatch):
    watcher, _, calls, comments = scenario(tmp_path, monkeypatch)
    comments[2]["body"] = comments[2]["body"].replace(COMMIT, "b" * 40)
    comments[2]["body"] = comments[2]["body"].replace("REPORT_ID: R2", "REPORT_ID: R3")
    comments[3]["body"] = comments[3]["body"].replace(COMMIT, "b" * 40).replace("IN_REPLY_TO: R2", "IN_REPLY_TO: R3")
    watcher.run_once()
    assert not calls
    assert watcher.status()["report_registry"]["R3"]["confirmation_error"] == "CONFIRMATION_COMMIT_MISMATCH"
