"""Real persisted watcher cycles with simulated GitHub, no external effects."""
import base64
import hashlib
import json
import subprocess

import pytest

from test_reviewer_files import Bus, REQUEST, HEAD, reply
from backend.control.reviewer_files import field

NEW_COMMIT = "c" * 40


class RecoveryBus(Bus):
    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch)
        self.request += f"REVIEWED_COMMIT: {REQUEST}\nDECISION_ID: D1\n"
        self.comments[0]["body"] += f"REVIEWED_COMMIT: {REQUEST}\nDECISION_ID: D1\n"
        self.request += f"AUTHORITY_RECORD: https://github.com/HIPVG/AI-Control-Center/blob/{REQUEST}/docs/decision.md\n"
        self.files = {}
        self.puts, self.posts = 0, 0
        self.fail_put = False
        self.lost_post = False
        self.delivered_post = True

    def runner(self, argv, **kwargs):
        if "--method" in argv:
            method = argv[argv.index("--method") + 1]
            if method == "PUT":
                self.puts += 1
                assert not any(x.startswith("sha=") for x in argv)
                if self.fail_put:
                    return subprocess.CompletedProcess(argv, 1, "{}", "failure")
                path = next(x for x in argv if "/contents/" in x).split("/contents/")[1]
                assert path.startswith("poc/file-review-requests/TRANSPORT-REPAIR-")
                self.files[path] = base64.b64decode(next(x[8:] for x in argv if x.startswith("content="))).decode()
                self.head["head"]["sha"] = NEW_COMMIT
                return subprocess.CompletedProcess(argv, 0, json.dumps({"commit": {"sha": NEW_COMMIT}}), "")
            assert method == "POST"
            self.posts += 1
            body = next(x[5:] for x in argv if x.startswith("body="))
            if self.delivered_post:
                self.comments.append({"id": 50, "body": body})
            return subprocess.CompletedProcess(argv, 1 if self.lost_post else 0, '{"id":50}', "")
        endpoint = argv[2] if argv[0] == "gh" else ""
        if "/contents/" in endpoint and "TRANSPORT-REPAIR-" in endpoint:
            path = endpoint.split("/contents/")[1].split("?")[0]
            if path not in self.files:
                return subprocess.CompletedProcess(argv, 1, '{"status":"404"}', "")
            raw = self.files[path].encode()
            data = {"type": "file", "path": path, "size": len(raw), "encoding": "base64",
                    "content": base64.b64encode(raw).decode(),
                    "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()}
            return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        return super().runner(argv, **kwargs)

    def response(self):
        body = self.comments[-1]["body"]
        rid = field(body, "REPORT_ID")
        self.files[f"poc/file-review-responses/{rid}.md"] = reply(rid).replace(REQUEST, NEW_COMMIT)
        return rid


def test_rejection_repairs_delivery_then_applies_new_reply_once_across_restart(tmp_path, monkeypatch):
    b = RecoveryBus(tmp_path, monkeypatch)
    state = b.watcher().run_once()
    old = state["report_registry"]["F1"]
    assert old["transport_recovery"]["phase"] == "POSTING"
    assert old["file_error"] == "REQUEST_BINDING_MISMATCH_AUTHORITY_RECORD"
    assert not b.prompts and b.puts == b.posts == 1
    replacement = old["transport_recovery"]["replacement_report_id"]
    fixed = b.files[f"poc/file-review-requests/{replacement}.md"]
    assert field(fixed, "REVIEWED_COMMIT") == REQUEST
    assert field(fixed, "AUTHORITY_RECORD") == field(b.request, "AUTHORITY_RECORD")
    state = b.watcher().run_once()
    assert state["report_registry"]["F1"]["transport_recovery"]["phase"] == "WAITING_RESPONSE"
    assert not b.prompts
    b.response()
    state = b.watcher().run_once()
    assert state["report_registry"][replacement]["state"] == "APPLIED"
    assert state["report_registry"]["F1"]["transport_recovery"]["phase"] == "RESOLVED"
    assert "F1" not in state["processed_report_ids"]
    assert len(b.prompts) == 1 and f"REPORT_ID: {replacement}" in b.prompts[0]
    b.watcher().run_once()
    assert b.posts == 1 and b.puts == 1 and len(b.prompts) == 1


@pytest.mark.parametrize("delivered,phase", [(True, "WAITING_RESPONSE"), (False, "ESCALATED")])
def test_ambiguous_post_never_reposts_after_restart(tmp_path, monkeypatch, delivered, phase):
    b = RecoveryBus(tmp_path, monkeypatch)
    b.lost_post, b.delivered_post = True, delivered
    b.watcher().run_once()
    state = b.watcher().run_once()
    assert state["report_registry"]["F1"]["transport_recovery"]["phase"] == phase
    b.watcher().run_once()
    assert b.posts == 1 and not b.prompts


def test_two_failed_creates_escalate_without_unbounded_retries(tmp_path, monkeypatch):
    b = RecoveryBus(tmp_path, monkeypatch)
    b.fail_put = True
    for _ in range(4):
        state = b.watcher().run_once()
    rec = state["report_registry"]["F1"]["transport_recovery"]
    assert rec["phase"] == "ESCALATED" and rec["attempts"] == 2
    assert b.puts == 2 and b.posts == 0 and not b.prompts


def test_response_deadline_escalates_and_does_not_extend_on_poll(tmp_path, monkeypatch):
    b = RecoveryBus(tmp_path, monkeypatch)
    b.watcher().run_once()
    w = b.watcher()
    rec = dict(w.status()["report_registry"]["F1"]["transport_recovery"])
    rec["deadline_at"] = "2000-01-01T00:00:00+00:00"
    w._update_report("F1", transport_recovery=rec)
    b.response()  # Even a valid but late reply must not launch continuation.
    state = w.run_once()
    rec = state["report_registry"]["F1"]["transport_recovery"]
    assert rec["phase"] == "ESCALATED"
    assert rec["escalation_reason"] == "RECOVERY_DEADLINE_EXCEEDED"
    assert rec["deadline_at"] == "2000-01-01T00:00:00+00:00"
    assert not b.prompts


def test_changed_authority_is_not_guessed_or_repaired(tmp_path, monkeypatch):
    b = RecoveryBus(tmp_path, monkeypatch)
    b.comments[0]["body"] += "AUTHORITY_RECORD: other-value\n"
    state = b.watcher().run_once()
    rec = state["report_registry"]["F1"]["transport_recovery"]
    assert rec["phase"] == "ESCALATED"
    assert rec["escalation_reason"] == "AUTHORITY_RECORD_NOT_OMITTED"
    assert not b.puts and not b.posts and not b.prompts


def test_repair_request_cannot_start_recursive_repair_chain(tmp_path, monkeypatch):
    b = RecoveryBus(tmp_path, monkeypatch)
    b.comments[0]["body"] += "RECOVERY_OF: ANCESTOR\n"
    state = b.watcher().run_once()
    assert state["report_registry"]["F1"]["transport_recovery"]["phase"] == "ESCALATED"
    assert not b.puts and not b.posts and not b.prompts


def test_existing_foreground_replacement_is_followed_without_new_post(tmp_path, monkeypatch):
    b = RecoveryBus(tmp_path, monkeypatch)
    w = b.watcher()
    # Simulate publication by another actor before the first recovery poll.
    child = "TRANSPORT-REPAIR-existing"
    path = f"poc/file-review-requests/{child}.md"
    request = b.request.replace("F1", child) + "REPLACES_INVALID_REPORT_ID: F1\n"
    b.files[path] = request
    b.comments.append({"id": 50, "body": request + f"REQUEST_COMMIT: {REQUEST}\n"})
    w._set_state(report_registry={"F1": {"state": "WAITING_RESPONSE", "report_comment_id": 10,
                                       "response_transport": "github_file", "response_required": True}})
    state = w.run_once()
    rec = state["report_registry"]["F1"]["transport_recovery"]
    assert rec["replacement_report_id"] == child and rec["phase"] == "WAITING_RESPONSE"
    assert b.posts == b.puts == 0 and not b.prompts
