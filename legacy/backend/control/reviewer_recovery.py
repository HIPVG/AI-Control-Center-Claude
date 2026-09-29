"""Bounded transport repair; never treats a rejected response as approval."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone

from backend.control.reviewer_files import (
    BRANCH, FIELDS, FileReplyError, GitHubFileReader, binding, field,
    validate_trigger_binding,
)


class TransportRecovery:
    """Runs inside the watcher's cycle lock. Its only write is a new request/trigger."""

    def __init__(self, watcher, repo, pr):
        self.w = watcher
        self.reader = GitHubFileReader(watcher, repo, pr)
        self.repo = repo

    def save(self, rid, record, **changes):
        record = dict(record, **changes)
        self.w._update_report(rid, transport_recovery=record)
        return record

    def escalate(self, rid, record, reason):
        self.save(rid, record, phase="ESCALATED", escalation_reason=reason,
                  next_action="配送担当: 保存された相関・配送証拠を確認し限定修復。新権限が必要な場合だけ人間判断。")

    def blocks_continuation(self, rid):
        for entry in self.w._state.get("report_registry", {}).values():
            record = entry.get("transport_recovery", {})
            if record.get("replacement_report_id") == rid and record.get("phase") != "RESOLVED":
                if record.get("phase") == "ESCALATED" or datetime.now(timezone.utc) >= datetime.fromisoformat(record["deadline_at"]):
                    return True
        return False

    def tick(self, comments):
        now = datetime.now(timezone.utc)
        registry = self.w._state.get("report_registry", {})
        for rid, entry in list(registry.items()):
            record = entry.get("transport_recovery")
            if not record or record["phase"] in {"RESOLVED", "ESCALATED"}:
                continue
            child = record.get("replacement_report_id")
            target = registry.get(child, {})
            if target.get("state") in {"APPLIED", "ACKNOWLEDGED", "RESOLVED_BY_CONFIRMATION"}:
                self.save(rid, record, phase="RESOLVED", resolved_at=now.isoformat(),
                          next_action="代替依頼の適用確認済み。旧拒否応答は未適用のまま保存。")
                continue
            if child and (target.get("file_error") or target.get("state") in {
                    "HUMAN_REQUIRED", "CONTINUATION_FAILED", "CONFIRMATION_BLOCKED"}):
                self.escalate(rid, record, "REPLACEMENT_FAILED_OR_REQUIRES_DECISION")
                continue
            if now >= datetime.fromisoformat(record["deadline_at"]):
                self.escalate(rid, record, "RECOVERY_DEADLINE_EXCEEDED")
                continue
            if comments is None:  # Transport unavailable: deadlines still advance.
                continue
            if record["phase"] in {"WAITING_RESPONSE", "RESPONSE_RECEIVED"}:
                if target.get("state") in {"RESPONSE_RECEIVED", "APPLYING"}:
                    self.save(rid, record, phase="RESPONSE_RECEIVED")
                continue
            try:
                # A foreground repair may already have published a replacement.
                replacements = [c for c in comments if field(c["body"], "REPLACES_INVALID_REPORT_ID") == rid]
                if replacements and not child:
                    if len(replacements) != 1:
                        raise FileReplyError("AMBIGUOUS_REPLACEMENT")
                    pin = binding(replacements[0]["body"])
                    request, _ = self.reader.content(pin["REQUEST_PATH"], pin["REQUEST_COMMIT"])
                    validate_trigger_binding(request, replacements[0]["body"])
                    self.save(rid, record, phase="WAITING_RESPONSE", replacement_report_id=pin["REPORT_ID"],
                              replacement_comment_id=replacements[0]["id"],
                              next_action="Watcher: 代替依頼の一致応答と適用結果を追跡。")
                    continue
                if record["phase"] == "POSTING":
                    # A crash/timeout after POST has an unknown outcome: read back,
                    # never issue another POST blindly.
                    matches = [c for c in comments if field(c["body"], "REPORT_ID") == child]
                    if len(matches) == 1 and binding(matches[0]["body"]) == binding(record["trigger"]):
                        self.save(rid, record, phase="WAITING_RESPONSE", replacement_comment_id=matches[0]["id"])
                    else:
                        self.escalate(rid, record, "POST_OUTCOME_UNKNOWN_OR_MISMATCH")
                    continue
                if record.get("attempts", 0) >= 2:
                    self.escalate(rid, record, "RECOVERY_ATTEMPTS_EXHAUSTED")
                    continue
                if record["reason"] != "REQUEST_BINDING_MISMATCH_AUTHORITY_RECORD":
                    raise FileReplyError("NO_DETERMINISTIC_REPAIR")
                reports = [c for c in comments if field(c["body"], "REPORT_ID") == rid]
                if len(reports) != 1 or field(reports[0]["body"], "RECOVERY_OF"):
                    raise FileReplyError("DUPLICATE_OR_RECURSIVE_RECOVERY")
                pin = binding(reports[0]["body"])
                request, _ = self.reader.content(pin["REQUEST_PATH"], pin["REQUEST_COMMIT"])
                if field(request, "RECOVERY_OF"):
                    raise FileReplyError("DUPLICATE_OR_RECURSIVE_RECOVERY")
                # Only an omitted trigger field can be reconstructed; changed values
                # and unknown baselines require an explicit diagnostic decision.
                if pin["AUTHORITY_RECORD"] is not None:
                    raise FileReplyError("AUTHORITY_RECORD_NOT_OMITTED")
                authority = field(request, "AUTHORITY_RECORD") or ""
                if not re.fullmatch(r"https://github.com/HIPVG/AI-Control-Center/blob/[0-9a-f]{40}/docs/[A-Za-z0-9_./-]+\.md", authority):
                    raise FileReplyError("AUTHORITY_RECORD_UNSUPPORTED")
                for key in FIELDS:
                    if key not in {"AUTHORITY_RECORD", "REQUEST_COMMIT"} and field(request, key) != pin[key]:
                        raise FileReplyError("MULTIPLE_BINDING_ERRORS")
                if not re.fullmatch(r"[0-9a-f]{40}", pin["REVIEWED_COMMIT"] or ""):
                    raise FileReplyError("REVIEWED_COMMIT_MISSING")
                child = "TRANSPORT-REPAIR-" + hashlib.sha256((rid + pin["REQUEST_COMMIT"]).encode()).hexdigest()[:24]
                body = request
                for key, value in {"REPORT_ID": child,
                                   "REQUEST_PATH": f"poc/file-review-requests/{child}.md",
                                   "RESPONSE_PATH": f"poc/file-review-responses/{child}.md"}.items():
                    body = re.sub(r"(?m)^" + key + r":[^\r\n]*", key + ": " + value, body)
                body = re.sub(r"(?m)^REQUEST_COMMIT:[^\r\n]*\r?\n?", "", body)
                body += f"\nRECOVERY_OF: {rid}\nREPLACES_INVALID_REPORT_ID: {rid}\n"
                path = f"poc/file-review-requests/{child}.md"
                record = self.save(rid, record, phase="PREPARING", replacement_report_id=child,
                                   attempts=record.get("attempts", 0) + 1)
                head = self.reader.head()  # validates exact repository and PR branch
                existing = self.reader.content(path, head, missing_ok=True)
                if existing is not None:
                    if existing[0] != body:
                        raise FileReplyError("REPLACEMENT_PATH_OCCUPIED")
                    commit = head
                else:
                    commit = self.create_request(path, body)
                stored, _ = self.reader.content(path, commit)
                if stored != body:
                    raise FileReplyError("REPLACEMENT_READBACK_MISMATCH")
                trigger = body + f"REQUEST_COMMIT: {commit}\n"
                validate_trigger_binding(stored, trigger)
                record = self.save(rid, record, phase="POSTING", trigger=trigger, request_commit=commit,
                                   next_action="Watcher: PR通知を読戻してから応答・適用を追跡。")
                self.w._post_comment(trigger)
                # The next normal fetch confirms delivery even if POST timed out.
                return  # one bounded delivery per poll; no Codex work in this path
            except (FileReplyError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
                if str(exc) == "GITHUB_FILE_FETCH_FAILED" and record.get("attempts", 0) < 2:
                    continue  # bounded by the original deadline; no repeat POST
                self.escalate(rid, record, str(exc))

    def create_request(self, path, body):
        argv = [self.w._resolve_executable(self.w.gh_executable) or self.w.gh_executable,
                "api", "--method", "PUT", f"repos/{self.repo}/contents/{path}",
                "-f", "message=repair reviewer transport binding", "-f", f"branch={BRANCH}",
                "-f", "content=" + base64.b64encode(body.encode()).decode()]
        # No sha: create-only. A retry reads the exact path before trying again.
        result = self.w.command_runner(argv, cwd=self.w.project_root, capture_output=True,
            text=True, encoding="utf-8", errors="strict", timeout=30, check=False, shell=False)
        if result.returncode:
            raise FileReplyError("GITHUB_FILE_FETCH_FAILED")
        try:
            commit = json.loads(result.stdout)["commit"]["sha"]
        except (ValueError, KeyError, TypeError):
            raise FileReplyError("GITHUB_FILE_FETCH_FAILED") from None
        if not re.fullmatch(r"[0-9a-f]{40}", str(commit)):
            raise FileReplyError("GITHUB_FILE_FETCH_FAILED")
        return commit

    def reject(self, rid, reason):
        entry = self.w._state["report_registry"][rid]
        if entry.get("transport_recovery"):
            return
        now = datetime.now(timezone.utc)
        self.save(rid, {}, phase="DETECTED", owner="deterministic_transport_worker",
                  reason=reason, detected_at=now.isoformat(),
                  deadline_at=(now + timedelta(minutes=10)).isoformat(), attempts=0,
                  report_comment_id=entry.get("report_comment_id"), binding=entry.get("file_binding"),
                  next_action="配送担当: 固定依頼を照合し限定修復。復元不能なら要対応として通知。")
