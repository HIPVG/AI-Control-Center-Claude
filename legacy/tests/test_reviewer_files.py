"""File transport state transitions with persisted state, no live effects."""
import base64
import hashlib
import json
import subprocess

import pytest

from backend.control.reviewer_bus import ReviewerBusWatcher, REVIEW_REPO
from backend.control.reviewer_files import BRANCH, FileReplyError, validate_trigger_binding

REQUEST = "a" * 40
HEAD = "b" * 40


def packet(rid="F1", required="yes"):
    return (f"REPORT_ID: {rid}\nREPORT_TYPE: PROGRESS_UPDATE\n"
            f"RESPONSE_REQUIRED: {required}\nRESPONSE_TRANSPORT: github_file\n"
            f"REQUEST_PATH: poc/file-review-requests/{rid}.md\n"
            f"RESPONSE_PATH: poc/file-review-responses/{rid}.md\n")


def reply(rid="F1", result="CONTINUE"):
    return (f"IN_REPLY_TO: {rid}\nRESULT: {result}\nREQUEST_COMMIT: {REQUEST}\n"
            f"REQUEST_PATH: poc/file-review-requests/{rid}.md\n"
            "RESPONSE_KIND: GIT_FILE\nNEXT_ACTION: Record only\n")


def test_trigger_preflight_requires_authority_record_to_match_request():
    request = (packet() + "REVIEWED_COMMIT: " + REQUEST + "\n"
               "DECISION_ID: D1\nAUTHORITY_RECORD: fixed-record\n")
    trigger = request + f"REQUEST_COMMIT: {REQUEST}\n"
    assert validate_trigger_binding(request, trigger)["AUTHORITY_RECORD"] == "fixed-record"
    with pytest.raises(FileReplyError, match="REQUEST_BINDING_MISMATCH_AUTHORITY_RECORD"):
        validate_trigger_binding(request, trigger.replace("AUTHORITY_RECORD: fixed-record\n", ""))


class Bus:
    def __init__(self, tmp_path, monkeypatch, required="yes"):
        self.root, self.monkeypatch = tmp_path, monkeypatch
        self.request = packet(required=required)
        self.comments = [{"id": 10, "body": self.request + f"REQUEST_COMMIT: {REQUEST}\n"}]
        self.body = reply(result="ACKNOWLEDGED" if required == "no" else "CONTINUE")
        self.calls, self.prompts = [], []
        self.code, self.action, self.transport_failure = 0, "NO_REPORT", False
        self.head = {"head": {"sha": HEAD, "ref": BRANCH, "repo": {"full_name": REVIEW_REPO}}}
        self.content_override = {}

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        assert "--method" not in argv, "No posting in these fixtures"
        if argv[0] == "codex":
            self.prompts.append(argv[-1])
            return subprocess.CompletedProcess(argv, self.code, json.dumps({"action": self.action}), "")
        endpoint = argv[2]
        if "/comments?" in endpoint:
            return subprocess.CompletedProcess(argv, 0, "\n".join(json.dumps(c) for c in self.comments), "")
        if endpoint.endswith("pulls/1"):
            data = self.head
        else:
            is_response = "/file-review-responses/" in endpoint
            assert endpoint.endswith("?ref=" + (HEAD if is_response else REQUEST))
            if is_response and self.transport_failure:
                return subprocess.CompletedProcess(argv, 1, '{"status":"403"}', "denied")
            body = self.body if is_response else self.request
            if body is None:
                return subprocess.CompletedProcess(argv, 1, '{"status":"404"}', "missing")
            raw = body.encode()
            data = {"type": "file", "path": endpoint.split("/contents/")[1].split("?")[0],
                    "size": len(raw), "encoding": "base64", "content": base64.b64encode(raw).decode(),
                    "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()}
            if is_response:
                data.update(self.content_override)
        return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")

    def watcher(self):
        w = ReviewerBusWatcher(self.root, command_runner=self.runner)
        self.monkeypatch.setattr(w, "_resolve_executable", lambda name: name)
        return w


def test_valid_file_applies_once_after_restart_and_retains_provenance(tmp_path, monkeypatch):
    b = Bus(tmp_path, monkeypatch)
    state = b.watcher().run_once()
    e = state["report_registry"]["F1"]
    assert e["state"] == "APPLIED" and state["pending_response_file"] is None
    assert e["response_comment_id"] is None
    assert e["response_file"]["head_commit"] == HEAD
    assert e["response_file"]["request_commit"] == REQUEST
    assert e["response_file_id"].startswith("git-file:")
    b.watcher().run_once()
    assert len(b.prompts) == 1 and reply() in b.prompts[0]


def test_ack_is_persisted_without_any_continuation(tmp_path, monkeypatch):
    b = Bus(tmp_path, monkeypatch, "no")
    for _ in range(2):
        state = b.watcher().run_once()
        assert state["report_registry"]["F1"]["state"] == "ACKNOWLEDGED"
    assert not b.prompts


@pytest.mark.parametrize("change", ["target", "commit", "path", "kind", "duplicate", "result", "next"])
def test_bad_response_never_runs_and_records_reason(tmp_path, monkeypatch, change):
    b = Bus(tmp_path, monkeypatch)
    mutations = {
        "target": lambda s: s.replace("IN_REPLY_TO: F1", "IN_REPLY_TO: OTHER"),
        "commit": lambda s: s.replace(REQUEST, "c" * 40),
        "path": lambda s: s.replace("requests/F1.md", "requests/OTHER.md"),
        "kind": lambda s: s.replace("GIT_FILE", "COMMENT"),
        "duplicate": lambda s: s + "IN_REPLY_TO: F1\n",
        "result": lambda s: s.replace("CONTINUE", "ACKNOWLEDGED"),
        "next": lambda s: s.replace("NEXT_ACTION: Record only\n", ""),
    }
    b.body = mutations[change](b.body)
    state = b.watcher().run_once()
    assert not b.prompts
    assert state["report_registry"]["F1"]["file_error"]
    assert state["last_file_response_error"]["report_id"] == "F1"
    assert "F1" not in state.get("processed_report_ids", [])


def test_absent_file_waits_and_comment_cannot_substitute(tmp_path, monkeypatch):
    b = Bus(tmp_path, monkeypatch)
    b.body = None
    b.comments.append({"id": 20, "body": "IN_REPLY_TO: F1\nRESULT: CONTINUE"})
    state = b.watcher().run_once()
    assert state["report_registry"]["F1"]["state"] == "WAITING_RESPONSE"
    assert not b.prompts
    b.body = reply()
    assert b.watcher().run_once()["report_registry"]["F1"]["state"] == "APPLIED"


@pytest.mark.parametrize("change", ["deleted", "edited", "mismatched"])
def test_failed_pending_file_revalidated_and_quarantined_without_retry(tmp_path, monkeypatch, change):
    b = Bus(tmp_path, monkeypatch)
    b.code = 1
    first = b.watcher().run_once()
    assert first["report_registry"]["F1"]["state"] == "CONTINUATION_FAILED"
    original = first["pending_response_file"]
    b.body = {"deleted": None, "edited": reply() + "Extra note\n",
              "mismatched": reply("OTHER")}[change]
    state = b.watcher().run_once()
    e = state["report_registry"]["F1"]
    assert e["state"] == "FILE_RESPONSE_INVALIDATED"
    assert e["invalidated_file_response"]["pending_file"] == original
    assert state["pending_response_file"] is None
    b.watcher().run_once()
    assert len(b.prompts) == 1


def test_unchanged_pending_file_retries_then_applies_once(tmp_path, monkeypatch):
    b = Bus(tmp_path, monkeypatch)
    b.code = 1
    b.watcher().run_once()
    b.code = 0
    assert b.watcher().run_once()["report_registry"]["F1"]["state"] == "APPLIED"
    b.watcher().run_once()
    assert len(b.prompts) == 2


def test_network_failure_preserves_pending_without_execution(tmp_path, monkeypatch):
    b = Bus(tmp_path, monkeypatch)
    b.code = 1
    original = b.watcher().run_once()["pending_response_file"]
    b.transport_failure = True
    state = b.watcher().run_once()
    assert state["pending_response_file"] == original
    assert state["last_error"] == "GITHUB_FILE_FETCH_FAILED"
    assert len(b.prompts) == 1


@pytest.mark.parametrize("override", [{"type": "symlink"}, {"sha": "0" * 40},
                                     {"size": 65537}, {"content": "!bad!"}])
def test_untrusted_content_rejected(tmp_path, monkeypatch, override):
    b = Bus(tmp_path, monkeypatch)
    b.content_override = override
    assert b.watcher().run_once()["report_registry"]["F1"]["file_error"]
    assert not b.prompts


def test_changed_request_binding_cannot_replace_pinned_request(tmp_path, monkeypatch):
    b = Bus(tmp_path, monkeypatch)
    b.body = None
    b.watcher().run_once()
    b.comments[0]["body"] = b.comments[0]["body"].replace(REQUEST, "c" * 40)
    b.body = reply()
    state = b.watcher().run_once()
    assert state["report_registry"]["F1"]["file_error"] == "FILE_REPORT_BINDING_CHANGED"
    assert not b.prompts


def test_wrong_head_repo_and_traversal_rejected(tmp_path, monkeypatch):
    b = Bus(tmp_path, monkeypatch)
    b.head["head"]["repo"]["full_name"] = "other/repo"
    assert b.watcher().run_once()["last_error"] == "GITHUB_FILE_HEAD_INVALID"
    # Quarantined requests retain their first rejection. Test traversal on a
    # separate request/store instead of mutating that rejected report in place.
    b = Bus(tmp_path / "traversal", monkeypatch)
    b.comments[0]["body"] = b.comments[0]["body"].replace("requests/F1.md", "requests/../F1.md")
    assert b.watcher().run_once()["report_registry"]["F1"]["file_error"] == "INVALID_REQUEST_PATH"
    assert not b.prompts


def test_pending_state_wrong_report_cannot_launch_other_comment_response(tmp_path, monkeypatch):
    b = Bus(tmp_path, monkeypatch)
    b.code = 1
    b.watcher().run_once()
    b.comments += [{"id": 30, "body": "REPORT_ID: C1\nREPORT_TYPE: PROGRESS_UPDATE"},
                   {"id": 31, "body": "IN_REPLY_TO: C1\nRESULT: CONTINUE"}]
    w = b.watcher()
    w._set_state(pending_response_report_id="C1", outstanding_report_id="C1")
    state = w.run_once()
    assert len(b.prompts) == 1
    assert state["last_error"] == "PENDING_FILE_STATE_MISMATCH"
    assert state["report_registry"]["F1"]["state"] == "FILE_RESPONSE_INVALIDATED"
    assert state["report_registry"]["C1"]["state"] == "RESPONSE_RECEIVED"


def test_bad_file_does_not_block_separate_comment_report(tmp_path, monkeypatch):
    b = Bus(tmp_path, monkeypatch)
    b.body = reply("WRONG")
    b.watcher().run_once()  # F1 is already registered before the later request.
    b.comments += [{"id": 30, "body": "REPORT_ID: C1\nREPORT_TYPE: PROGRESS_UPDATE"},
                   {"id": 31, "body": "IN_REPLY_TO: C1\nRESULT: CONTINUE"}]
    state = b.watcher().run_once()
    assert state["report_registry"]["F1"]["file_error"]
    assert state["report_registry"]["C1"]["state"] == "APPLIED"
    assert len(b.prompts) == 1 and "REPORT_ID: C1" in b.prompts[0]


def test_no_opt_in_keeps_legacy_poc_informational(tmp_path, monkeypatch):
    b = Bus(tmp_path, monkeypatch, "no")
    b.comments[0]["body"] = b.comments[0]["body"].replace(
        "RESPONSE_TRANSPORT: github_file", "FILE_REVIEW_REQUEST: yes")
    state = b.watcher().run_once()
    assert state["report_registry"]["F1"]["state"] == "NOT_REQUIRED"
    assert len(b.calls) == 1 and not b.prompts


def test_upgrade_reads_explicit_file_report_registered_by_older_watcher(tmp_path, monkeypatch):
    b = Bus(tmp_path, monkeypatch)
    w = b.watcher()
    w._set_state(report_registry={"F1": {"report_comment_id": 10,
        "state": "WAITING_RESPONSE", "response_required": True}})
    assert w.run_once()["report_registry"]["F1"]["state"] == "APPLIED"
    b.watcher().run_once()
    assert len(b.prompts) == 1


def test_file_confirmation_resolves_old_wait_only_after_success(tmp_path, monkeypatch):
    b = Bus(tmp_path, monkeypatch)
    fields = (f"REVIEWED_COMMIT: {REQUEST}\nCONFIRMS_REPORT_ID: OLD\n"
              "CONFIRMS_RESPONSE_ID: 5\nDECISION_ID: D1\nAUTHORITY_RECORD: fixed-record\n")
    b.request += fields
    b.comments[0]["body"] += fields
    b.body += fields
    w = b.watcher()
    w._set_state(processed_report_ids=["OLD"], report_registry={"OLD": {
        "state": "HUMAN_REQUIRED", "response_comment_id": 5, "reviewed_commit": REQUEST}})
    state = w.run_once()
    assert state["report_registry"]["OLD"]["state"] == "RESOLVED_BY_CONFIRMATION"
    assert state["report_registry"]["OLD"]["response_comment_id"] == 5
    assert state["report_registry"]["OLD"]["resolution_response_file_id"].startswith("git-file:")
    b.watcher().run_once()
    assert len(b.prompts) == 1
