"""Deterministic GitHub reviewer-bus watcher and report transport."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Callable

from backend.control.reviewer_files import FileReplyError, GitHubFileReader, binding
from backend.control.reviewer_recovery import TransportRecovery

REVIEW_REPO = "HIPVG/AI-Control-Center-Review-Bridge"
REVIEW_PR = 1
POLL_SECONDS = 120
REPORT_ID_RE = re.compile(r"(?m)^REPORT_ID:\s*([^\s]+)\s*$")
REPORT_TYPE_RE = re.compile(r"(?m)^REPORT_TYPE:\s*([^\s]+)\s*$")
IN_REPLY_TO_RE = re.compile(r"(?m)^IN_REPLY_TO:\s*([^\s]+)\s*$")
REPORT_TYPES = {"PROGRESS_UPDATE", "DECISION_REQUEST", "COMPLETION_REPORT"}
POST_ACTION = "POST_REPORT"
CONFIRMATION_FIELDS = ("CONFIRMS_REPORT_ID", "CONFIRMS_RESPONSE_ID", "DECISION_ID", "REVIEWED_COMMIT")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReviewerBusWatcher:
    """Own reviewer response polling, Codex continuation, and PR publication."""

    def __init__(self, project_root: Path, codex_executable: str = "codex", *, gh_executable: str = "gh", poll_seconds: int = POLL_SECONDS, command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
        self.project_root = project_root.resolve()
        self.codex_executable = codex_executable
        self.gh_executable = gh_executable
        self.poll_seconds = max(10, int(poll_seconds))
        self.command_runner = command_runner
        self.state_path = self.project_root / "state" / "reviewer-bus-watcher.json"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._cycle_lock = threading.Lock()
        self._state = self._load_state()

    def available(self) -> bool:
        return self._resolve_executable(self.codex_executable) is not None and self._resolve_executable(self.gh_executable) is not None

    def start(self) -> bool:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return False
        if os.environ.get("AI_CONTROL_CENTER_DISABLE_REVIEWER_BUS") == "1":
            self._set_state(running=False, available=False, last_error="DISABLED_BY_ENV")
            return False
        if not self.available():
            self._set_state(running=False, available=False, last_error="REVIEWER_BUS_PREREQUISITE_MISSING")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="reviewer-bus-watcher", daemon=True)
        self._thread.start()
        self._set_state(running=True, available=True, last_error=None)
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._set_state(running=False)

    def status(self) -> dict[str, object]:
        with self._lock:
            return dict(self._state)

    def run_once(self) -> dict[str, object]:
        with self._cycle_lock:
            self._cycle_comments = None
            self._run_once()
            TransportRecovery(self, REVIEW_REPO, REVIEW_PR).tick(self._cycle_comments)
            return self.status()

    def _run_once(self) -> dict[str, object]:
        comments, error = self._fetch_comments()
        self._set_state(last_poll_at=_utc_now())
        if error:
            self._set_state(last_error=error)
            return self.status()

        self._cycle_comments = comments
        self._sync_report_registry(comments)
        if not self._validate_pending_file_state():
            return self.status()
        file_responses = self._sync_file_responses(comments)
        report = self._current_report(comments)
        if report is None:
            if not any(e.get("file_error") for e in self._state.get("report_registry", {}).values()):
                self._set_state(last_error=None)
            return self.status()
        report_id = self._extract(REPORT_ID_RE, str(report.get("body", "")))
        if not report_id:
            self._set_state(last_error="REPORT_ID_MISSING")
            return self.status()

        entry = self._state.get("report_registry", {}).get(report_id, {})
        if entry.get("response_transport") == "github_file":
            response, pending_invalidated = file_responses.get(report_id), True
        else:
            response, pending_invalidated = self._pending_or_matching_response(comments, report, report_id)
        if response is None:
            if not pending_invalidated:
                self._record_unmatched_response(comments, report, report_id)
            return self.status()

        response_id = response["id"]
        response_body = str(response.get("body", ""))
        confirmation_error = self._confirmation_error(report_id, response_body)
        if confirmation_error:
            self._update_report(report_id, state="CONFIRMATION_BLOCKED", confirmation_error=confirmation_error,
                                rejected_response_comment_id=response_id)
            self._set_state(last_error=confirmation_error)
            return self.status()
        comment_id = response_id if isinstance(response_id, int) else None
        file_source = {k: v for k, v in response.items() if k != "body"} if comment_id is None else None
        self._set_state(last_report_id=report_id, last_response_comment_id=comment_id, pending_response_comment_id=comment_id, pending_response_file=file_source, pending_response_body=response_body, pending_response_report_id=report_id, outstanding_report_id=report_id, outstanding_report_comment_id=int(report["id"]), last_error=None)
        prompt = self._resume_prompt(report_id, response_body)
        prompt += "\nORIGINAL_REPORT:\n" + str(report["body"])
        prompt += "\nREVIEW_WORK_IN_PROGRESS:\n" + json.dumps(self._work_in_progress(), ensure_ascii=False)
        recovery = self._state.get("last_binding_recovery", {})
        if recovery.get("target_report_id") == report_id:
            prompt += "\nCURRENT HUMAN AUTHORIZED RECOVERY SCOPE:\n" + str(recovery["continuation_scope"])
        self._update_report(report_id, state="APPLYING", response_comment_id=comment_id,
                            response_file=file_source, response_file_id=response_id if file_source else None)
        completed = self._continue_codex(prompt)
        self._set_state(last_continuation_at=_utc_now(), last_codex_exit_code=completed.returncode)
        if completed.returncode != 0:
            self._update_report(report_id, state="CONTINUATION_FAILED")
            self._set_state(last_error="CODEX_CONTINUATION_FAILED")
            return self.status()

        envelope = self._extract_envelope(completed.stdout)
        if envelope is None:
            self._update_report(report_id, state="CONTINUATION_FAILED")
            self._set_state(last_error="CODEX_CONTINUATION_OUTPUT_INVALID")
            return self.status()
        self._set_state(last_continuation_action=envelope.get("action"))
        if envelope.get("action") != POST_ACTION:
            if envelope.get("action") in {"NO_REPORT", "HUMAN_REQUIRED"}:
                self._mark_response_applied(response_id, report_id)
                return self.status()
            self._set_state(last_error="CODEX_CONTINUATION_OUTPUT_INVALID")
            return self.status()

        report_body = self._validated_report_body(envelope)
        if report_body is None:
            self._set_state(last_error="CODEX_CONTINUATION_OUTPUT_INVALID")
            return self.status()
        pending_list = [entry for entry in self._work_in_progress() if entry["report_id"] != report_id]
        pending_list.append({"report_id": str(envelope["report_id"]), "state": "WAITING_RESPONSE",
                             "response_required": not bool(re.search(r"(?mi)^RESPONSE_REQUIRED:\s*no\s*$", report_body))})
        report_body = re.sub(r"(?m)^REVIEW_WORK_IN_PROGRESS:.*\n?", "", report_body)
        report_body += "\nREVIEW_WORK_IN_PROGRESS: " + json.dumps(pending_list, ensure_ascii=False)
        posted_id = self._post_comment(report_body)
        if posted_id is None:
            self._set_state(last_error="GITHUB_REPORT_DELIVERY_FAILED")
            return self.status()

        new_report_id = str(envelope["report_id"])
        self._update_report(new_report_id, state="WAITING_RESPONSE", report_comment_id=posted_id,
                            report_type=envelope["report_type"], predecessor_report_id=report_id,
                            response_transport="github_file" if self._single_field(report_body, "RESPONSE_TRANSPORT") == "github_file" else "comment")
        self._mark_response_applied(response_id, report_id, outstanding_report_id=new_report_id, outstanding_report_comment_id=posted_id, last_report_id=new_report_id, last_delivery_comment_id=posted_id, last_delivery_at=_utc_now())
        return self.status()

    def _loop(self) -> None:
        while not self._stop.is_set():
            cycle_started = monotonic()
            self.run_once()
            self._stop.wait(max(0.0, self.poll_seconds - (monotonic() - cycle_started)))

    def _fetch_comments(self) -> tuple[list[dict[str, object]], str | None]:
        gh = self._resolve_executable(self.gh_executable) or self.gh_executable
        argv = [gh, "api", f"repos/{REVIEW_REPO}/issues/{REVIEW_PR}/comments?per_page=100", "--paginate", "--jq", ".[] | {id: .id, body: .body, created_at: .created_at}"]
        try:
            completed = self.command_runner(argv, cwd=self.project_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            return [], "GITHUB_COMMENT_FETCH_FAILED"
        if completed.returncode != 0:
            return [], "GITHUB_COMMENT_FETCH_FAILED"
        comments: list[dict[str, object]] = []
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                return [], "GITHUB_COMMENT_OUTPUT_INVALID"
            if isinstance(value, dict) and isinstance(value.get("id"), int) and isinstance(value.get("body"), str):
                comments.append(value)
        comments.sort(key=lambda item: int(item["id"]))
        return comments, None

    def _current_report(self, comments: list[dict[str, object]]) -> dict[str, object] | None:
        excluded = set(self._state.get("non_controlling_report_ids", []))
        reports = [item for item in comments if self._extract(REPORT_ID_RE, str(item.get("body", ""))) not in excluded and self._extract(REPORT_ID_RE, str(item.get("body", ""))) and self._extract(REPORT_TYPE_RE, str(item.get("body", "")))]
        if not reports:
            return None
        expected = self._state.get("outstanding_report_id")
        if not isinstance(expected, str):
            expected = self._state.get("pending_response_report_id")
        registry = self._state.get("report_registry")
        if registry is not None:
            recovery = TransportRecovery(self, REVIEW_REPO, REVIEW_PR)
            reports = [item for item in reports if not recovery.blocks_continuation(self._extract(REPORT_ID_RE, str(item["body"])))]
            if self._state.get("pending_response_comment_id") or self._state.get("pending_response_file"):
                return next((item for item in reversed(reports) if self._extract(REPORT_ID_RE, str(item["body"])) == expected), None)
            eligible = [item for item in reports if not registry.get(self._extract(REPORT_ID_RE, str(item["body"])), {}).get("transport_recovery") and registry.get(self._extract(REPORT_ID_RE, str(item["body"])), {}).get("state")
                        in {"QUEUED", "WAITING_RESPONSE", "RESPONSE_RECEIVED", "CONTINUATION_FAILED"}]
            ready = [item for item in eligible if registry[self._extract(REPORT_ID_RE, str(item["body"]))]["state"] == "RESPONSE_RECEIVED"]
            candidates = ready or eligible
            return min(candidates, key=lambda item: int(item["id"])) if candidates else None
        if isinstance(expected, str):
            return next((item for item in reversed(reports) if self._extract(REPORT_ID_RE, str(item["body"])) == expected), None)
        latest = max(reports, key=lambda item: int(item["id"]))
        if self._extract(REPORT_ID_RE, str(latest["body"])) in self._state.get("processed_report_ids", []):
            return None
        return latest

    def _sync_report_registry(self, comments: list[dict[str, object]]) -> None:
        """Keep every request; a new comment must never displace existing work."""
        reports = [item for item in comments if self._extract(REPORT_ID_RE, str(item["body"]))
                   and self._extract(REPORT_TYPE_RE, str(item["body"])) in REPORT_TYPES]
        if not reports:
            return
        registry = dict(self._state.get("report_registry", {}))
        first_load = "report_registry" not in self._state
        anchor = self._state.get("outstanding_report_id") or self._state.get("pending_response_report_id")
        if not anchor:
            anchor = self._extract(REPORT_ID_RE, str(max(reports, key=lambda item: int(item["id"]))["body"]))
        anchor_comment = next((int(item["id"]) for item in reports if self._extract(REPORT_ID_RE, str(item["body"])) == anchor), 0)
        for report in reports:
            rid = self._extract(REPORT_ID_RE, str(report["body"]))
            entry = dict(registry.get(rid, {}))
            if not entry:
                state = "HISTORICAL_NOT_REPLAYED" if first_load and int(report["id"]) < anchor_comment else "QUEUED"
                if rid == anchor:
                    state = "WAITING_RESPONSE"
                reply_required = not re.search(r"(?mi)^RESPONSE_REQUIRED:\s*no\s*$", str(report["body"]))
                entry = {"report_comment_id": int(report["id"]), "report_type": self._extract(REPORT_TYPE_RE, str(report["body"])), "state": state, "response_required": reply_required,
                         "response_transport": "github_file" if self._single_field(str(report["body"]), "RESPONSE_TRANSPORT") == "github_file" else "comment"}
                for field in ("REVIEWED_COMMIT", "BASELINE_ID"):
                    value = self._extract(re.compile(r"(?m)^" + field + r":\s*([^\s]+)\s*$"), str(report["body"]))
                    if value:
                        entry[field.lower()] = value
            entry.setdefault("response_required", not bool(re.search(r"(?mi)^RESPONSE_REQUIRED:\s*no\s*$", str(report["body"]))))
            # Older watcher versions did not persist transport. Honor an explicit
            # file contract on first upgraded read; never infer it from PoC flags.
            entry.setdefault("response_transport", "github_file" if self._single_field(str(report["body"]), "RESPONSE_TRANSPORT") == "github_file" else "comment")
            if rid in self._state.get("non_controlling_report_ids", []):
                entry["state"] = "NON_CONTROLLING"
            elif rid in self._state.get("processed_report_ids", []) and entry["state"] not in {"HUMAN_REQUIRED", "RESOLVED_BY_CONFIRMATION"}:
                entry["state"] = "HUMAN_REQUIRED" if rid == self._state.get("last_report_id") and self._state.get("last_continuation_action") == "HUMAN_REQUIRED" else "APPLIED"
            elif entry.get("response_required") is False and entry["state"] != "HISTORICAL_NOT_REPLAYED" and entry["response_transport"] == "comment":
                acknowledgements = [item for item in comments if int(item["id"]) > entry["report_comment_id"]
                                    and self._response_target(str(item["body"])) == rid
                                    and re.search(r"(?m)^RESULT:\s*ACKNOWLEDGED\s*$", str(item["body"]))]
                entry["state"] = "ACKNOWLEDGED" if acknowledgements else "NOT_REQUIRED"
                if acknowledgements:
                    entry["response_comment_id"] = int(min(acknowledgements, key=lambda item: int(item["id"]))["id"])
            elif entry["state"] in {"QUEUED", "WAITING_RESPONSE", "RESPONSE_RECEIVED"} and entry["response_transport"] == "comment":
                replies = [item for item in comments if int(item["id"]) > entry["report_comment_id"]
                           and self._response_target(str(item["body"])) == rid]
                if replies:
                    entry.update(state="RESPONSE_RECEIVED", response_comment_id=int(min(replies, key=lambda item: int(item["id"]))["id"]))
            registry[rid] = entry
            if re.search(r"(?m)^CONFIRMS_REPORT_ID:", str(report["body"])):
                entry["confirmation_requested"] = True
                if not self._single_field(str(report["body"]), "CONFIRMS_REPORT_ID"):
                    entry["confirmation_metadata_invalid"] = True
            for field in (*CONFIRMATION_FIELDS, "AUTHORITY_RECORD"):
                value = self._single_field(str(report["body"]), field)
                if value:
                    entry.setdefault(field.lower(), value)
        self._set_state(report_registry=registry)

    def _validate_pending_file_state(self):
        pending = self._state.get("pending_response_file")
        if not pending:
            return True
        rid = self._state.get("pending_response_report_id")
        entry = self._state.get("report_registry", {}).get(rid, {})
        if (isinstance(pending, dict) and entry.get("response_transport") == "github_file"
                and entry.get("file_binding", {}).get("RESPONSE_PATH") == pending.get("path")
                and self._state.get("outstanding_report_id") == rid
                and rid not in self._state.get("processed_report_ids", [])
                and not self._state.get("pending_response_comment_id")):
            return True
        evidence = {"reason": "PENDING_FILE_STATE_MISMATCH", "pending_file": pending,
                    "report_id": rid, "outstanding_report_id": self._state.get("outstanding_report_id"),
                    "observed_at": _utc_now()}
        self._set_state(last_file_response_error=evidence, last_error="PENDING_FILE_STATE_MISMATCH",
                        pending_response_file=None, pending_response_body=None,
                        pending_response_report_id=None, pending_response_comment_id=None)
        # Quarantine the source identified by the stored file, not an unrelated report ID.
        for key, value in list(self._state.get("report_registry", {}).items()):
            if value.get("response_file") == pending and value.get("response_transport") == "github_file":
                self._update_report(key, state="FILE_RESPONSE_INVALIDATED",
                                    invalidated_file_response=evidence, file_error="PENDING_FILE_STATE_MISMATCH")
                TransportRecovery(self, REVIEW_REPO, REVIEW_PR).reject(key, "PENDING_FILE_STATE_MISMATCH")
        return False

    def _sync_file_responses(self, comments):
        """Validate each opted-in report independently; never manufacture comment IDs."""
        reader = GitHubFileReader(self, REVIEW_REPO, REVIEW_PR)
        responses, head = {}, None
        terminal = {"APPLIED", "ACKNOWLEDGED", "HUMAN_REQUIRED", "RESOLVED_BY_CONFIRMATION",
                    "NON_CONTROLLING", "HISTORICAL_NOT_REPLAYED", "FILE_RESPONSE_INVALIDATED"}
        for rid, entry in list(self._state.get("report_registry", {}).items()):
            if entry.get("response_transport") != "github_file" or entry["state"] in terminal or entry.get("transport_recovery"):
                continue
            pending = self._state.get("pending_response_file")
            pending_for_report = pending and self._state.get("pending_response_report_id") == rid
            try:
                reports = [c for c in comments if self._extract(REPORT_ID_RE, str(c["body"])) == rid]
                if not reports:
                    raise FileReplyError("FILE_REPORT_MISSING")
                pin = binding(str(reports[0]["body"]))
                if any(binding(str(c["body"])) != pin for c in reports[1:]):
                    raise FileReplyError("FILE_REPORT_BINDING_CHANGED")
                if entry.get("file_binding") and entry["file_binding"] != pin:
                    raise FileReplyError("FILE_REPORT_BINDING_CHANGED")
                self._update_report(rid, file_binding=pin)
                if head is None:
                    head = reader.head()
                response = reader.response(pin, head)
                if pending_for_report:
                    if (response is None or pending.get("id") != response["id"] or
                            self._state.get("outstanding_report_id") != rid or
                            rid in self._state.get("processed_report_ids", [])):
                        raise FileReplyError("PENDING_FILE_CHANGED_OR_MISSING")
                if response is None:
                    self._update_report(rid, state="WAITING_RESPONSE" if entry["response_required"] else "NOT_REQUIRED", file_error=None)
                    continue
                provenance = {k: v for k, v in response.items() if k != "body"}
                if not entry["response_required"]:
                    self._update_report(rid, state="ACKNOWLEDGED", response_file=provenance,
                                        response_file_id=response["id"], file_error=None)
                else:
                    self._update_report(rid, state="RESPONSE_RECEIVED", file_error=None)
                    responses[rid] = response
            except FileReplyError as exc:
                reason = str(exc)
                evidence = {"report_id": rid, "reason": reason, "observed_at": _utc_now(),
                            "pending_file": pending if pending_for_report else None}
                # Network errors preserve the pending item for a later read, not an execution.
                if pending_for_report and reason != "GITHUB_FILE_FETCH_FAILED":
                    self._update_report(rid, state="FILE_RESPONSE_INVALIDATED", file_error=reason,
                                        invalidated_file_response=evidence)
                    self._set_state(pending_response_file=None, pending_response_body=None,
                                    pending_response_report_id=None)
                else:
                    self._update_report(rid, file_error=reason)
                self._set_state(last_error=reason, last_file_response_error=evidence)
                if reason != "GITHUB_FILE_FETCH_FAILED":
                    TransportRecovery(self, REVIEW_REPO, REVIEW_PR).reject(rid, reason)
        return responses

    @staticmethod
    def _single_field(body: str, name: str) -> str | None:
        values = re.findall(r"(?m)^" + re.escape(name) + r":[ \t]*([^\r\n]+)$", body)
        return values[0].strip() if len(values) == 1 else None

    def _confirmation_error(self, report_id: str, response_body: str) -> str | None:
        registry = self._state.get("report_registry", {})
        entry = registry.get(report_id, {})
        target_id = entry.get("confirms_report_id")
        if entry.get("confirmation_metadata_invalid") or (entry.get("confirmation_requested") and not target_id):
            return "CONFIRMATION_REPORT_BINDING_INVALID"
        if not target_id:
            return None
        target = registry.get(target_id, {})
        if not entry.get("authority_record") or not entry.get("decision_id"):
            return "CONFIRMATION_AUTHORITY_RECORD_MISSING"
        # The old reply was handled, but the authority question remained open.
        if target_id == report_id or target.get("state") != "HUMAN_REQUIRED":
            return "CONFIRMATION_TARGET_NOT_WAITING"
        if not target.get("reviewed_commit") or entry.get("reviewed_commit") != target.get("reviewed_commit"):
            return "CONFIRMATION_COMMIT_MISMATCH"
        if str(target.get("response_file_id") or target.get("response_comment_id")) != entry.get("confirms_response_id"):
            return "CONFIRMATION_PRIOR_RESPONSE_MISMATCH"
        for field in CONFIRMATION_FIELDS:
            if not entry.get(field.lower()) or self._single_field(response_body, field) != entry[field.lower()]:
                return "CONFIRMATION_REPLY_BINDING_MISMATCH"
        result = self._single_field(response_body, "RESULT")
        if result not in {"CONTINUE", "DECISION", "ACCEPT_COMPLETE", "REJECT", "HUMAN_REQUIRED"}:
            return "CONFIRMATION_RESULT_INVALID"
        if not self._single_field(response_body, "NEXT_ACTION"):
            return "CONFIRMATION_NEXT_ACTION_MISSING"
        return None

    def _resolve_confirmed_wait(self, report_id: str, response_id: int | str) -> None:
        entry = self._state.get("report_registry", {}).get(report_id, {})
        target_id = entry.get("confirms_report_id")
        if not target_id or self._state.get("last_continuation_action") == "HUMAN_REQUIRED":
            return
        body = str(self._state.get("pending_response_body", ""))
        if self._single_field(body, "RESULT") not in {"CONTINUE", "DECISION", "ACCEPT_COMPLETE"}:
            return
        if self._confirmation_error(report_id, body):
            return
        # Never replace the original response ID or delete its history.
        self._update_report(target_id, state="RESOLVED_BY_CONFIRMATION",
                            previous_state="HUMAN_REQUIRED", resolved_by_report_id=report_id,
                            resolution_response_comment_id=response_id if isinstance(response_id, int) else None,
                            resolution_response_file_id=response_id if isinstance(response_id, str) else None,
                            resolution_decision_id=entry["decision_id"], resolved_at=_utc_now())

    @staticmethod
    def _response_target(body: str) -> str | None:
        targets = IN_REPLY_TO_RE.findall(body)
        return targets[0].strip() if len(targets) == 1 else None

    def _update_report(self, report_id: str, **changes: object) -> None:
        registry = dict(self._state.get("report_registry", {}))
        registry[report_id] = {**registry.get(report_id, {}), **changes, "updated_at": _utc_now()}
        self._set_state(report_registry=registry)

    def _work_in_progress(self) -> list[dict[str, object]]:
        return [{"report_id": rid, **entry} for rid, entry in self._state.get("report_registry", {}).items()
                if entry.get("state") not in {"APPLIED", "ACKNOWLEDGED", "NON_CONTROLLING", "HISTORICAL_NOT_REPLAYED", "RESOLVED_BY_CONFIRMATION"}]

    def recover_outstanding_report(self, comments: list[dict[str, object]], *, expected_current: str, target_report_id: str, target_comment_id: int, authority: str, continuation_scope: str) -> None:
        """Explicit offline repair; never infer the target from a mismatched reply."""
        if self._thread and self._thread.is_alive():
            raise ValueError("WATCHER_MUST_BE_STOPPED")
        if self._state.get("outstanding_report_id") != expected_current or expected_current == target_report_id:
            raise ValueError("RECOVERY_CURRENT_TARGET_CHANGED")
        if self._state.get("pending_response_comment_id"):
            raise ValueError("RECOVERY_PENDING_RESPONSE_MUST_BE_INVALIDATED_FIRST")
        if not authority.strip() or not continuation_scope.strip():
            raise ValueError("RECOVERY_AUTHORITY_AND_SCOPE_REQUIRED")
        if target_report_id in self._state.get("processed_report_ids", []):
            raise ValueError("RECOVERY_TARGET_ALREADY_APPLIED")
        target = next((item for item in comments if item["id"] == target_comment_id), None)
        if not target or self._extract(REPORT_ID_RE, str(target["body"])) != target_report_id or self._extract(REPORT_TYPE_RE, str(target["body"])) not in REPORT_TYPES:
            raise ValueError("RECOVERY_TARGET_NOT_CONFIRMED")
        if not any(self._extract(REPORT_ID_RE, str(item["body"])) == expected_current for item in comments):
            raise ValueError("RECOVERY_PREVIOUS_REPORT_NOT_CONFIRMED")
        record = {
            "previous_report_id": expected_current,
            "previous_report_comment_id": self._state.get("outstanding_report_comment_id"),
            "previous_last_error": self._state.get("last_error"),
            "previous_invalidated_response": self._state.get("last_invalidated_response"),
            "target_report_id": target_report_id, "target_comment_id": target_comment_id,
            "authority": authority, "continuation_scope": continuation_scope,
            "recovered_at": _utc_now(),
        }
        history = list(self._state.get("binding_recovery_history", [])) + [record]
        excluded = list(self._state.get("non_controlling_report_ids", []))
        if expected_current not in excluded:
            excluded.append(expected_current)
        self._set_state(outstanding_report_id=target_report_id, outstanding_report_comment_id=target_comment_id,
                        last_binding_recovery=record, binding_recovery_history=history,
                        non_controlling_report_ids=excluded)
        self._sync_report_registry(comments)
        self._update_report(target_report_id, state="WAITING_RESPONSE")

    def _pending_or_matching_response(self, comments: list[dict[str, object]], report: dict[str, object], report_id: str) -> tuple[dict[str, object] | None, bool]:
        pending_id = int(self._state.get("pending_response_comment_id", 0) or 0)
        if pending_id:
            pending = next((item for item in comments if int(item["id"]) == pending_id), None)
            pending_report_id = self._state.get("pending_response_report_id")
            actual_in_reply_to = self._response_target(str(pending.get("body", ""))) if pending else None
            state_outstanding = self._state.get("outstanding_report_id")
            reasons: list[str] = []
            if report_id in self._state.get("processed_report_ids", []):
                reasons.append("PENDING_RESPONSE_ALREADY_APPLIED")
            if pending is None:
                reasons.append("PENDING_RESPONSE_COMMENT_NOT_FOUND")
            if not isinstance(pending_report_id, str) or pending_report_id != report_id:
                reasons.append("PENDING_RESPONSE_REPORT_ID_MISMATCH")
            if actual_in_reply_to != report_id:
                reasons.append("PENDING_RESPONSE_IN_REPLY_TO_MISMATCH")
            if state_outstanding is not None and state_outstanding != report_id:
                reasons.append("OUTSTANDING_REPORT_ID_MISMATCH")
            if reasons:
                self._invalidate_pending_response(
                    response_comment_id=pending_id,
                    pending_response_report_id=pending_report_id if isinstance(pending_report_id, str) else None,
                    expected_report_id=report_id,
                    actual_in_reply_to=actual_in_reply_to,
                    reason="|".join(reasons),
                    report_comment_id=int(report["id"]),
                )
                return None, True
            return pending, False
        report_comment_id = int(report["id"])
        applied = set(self._state.get("processed_report_ids", []))
        matching = [item for item in comments if int(item["id"]) > report_comment_id and report_id not in applied and self._response_target(str(item.get("body", ""))) == report_id]
        return (min(matching, key=lambda item: int(item["id"])), False) if matching else (None, False)

    def _record_unmatched_response(self, comments: list[dict[str, object]], report: dict[str, object], expected: str) -> None:
        known_reports = set(self._state.get("processed_report_ids", [])) | set(self._state.get("report_registry", {}))
        for item in comments:
            response_id = int(item["id"])
            received = self._extract(IN_REPLY_TO_RE, str(item.get("body", "")))
            if response_id <= int(report["id"]) or not received or received == expected or received in known_reports:
                continue
            self._set_state(
                last_error="REVIEW_RESPONSE_CORRELATION_MISMATCH",
                outstanding_report_id=expected,
                outstanding_report_comment_id=int(report["id"]),
                last_invalidated_response={
                    "response_comment_id": response_id,
                    "pending_response_report_id": None,
                    "expected_report_id": expected,
                    "actual_in_reply_to": received,
                    "reason": "UNMATCHED_RESPONSE_IN_REPLY_TO_MISMATCH",
                    "invalidated_at": _utc_now(),
                },
            )
            return

    def _invalidate_pending_response(self, *, response_comment_id: int, pending_response_report_id: str | None, expected_report_id: str, actual_in_reply_to: str | None, reason: str, report_comment_id: int) -> None:
        """Fail closed: retain mismatch evidence and restore the current report wait."""
        self._set_state(
            pending_response_comment_id=None,
            pending_response_body=None,
            pending_response_report_id=None,
            outstanding_report_id=expected_report_id,
            outstanding_report_comment_id=report_comment_id,
            last_error="REVIEW_RESPONSE_CORRELATION_MISMATCH",
            last_invalidated_response={
                "response_comment_id": response_comment_id,
                "pending_response_report_id": pending_response_report_id,
                "expected_report_id": expected_report_id,
                "actual_in_reply_to": actual_in_reply_to,
                "reason": reason,
                "invalidated_at": _utc_now(),
            },
        )

    def _post_comment(self, body: str) -> int | None:
        gh = self._resolve_executable(self.gh_executable) or self.gh_executable
        argv = [gh, "api", "--method", "POST", f"repos/{REVIEW_REPO}/issues/{REVIEW_PR}/comments", "-f", f"body={body}"]
        try:
            completed = self.command_runner(argv, cwd=self.project_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return None
        return int(result["id"]) if isinstance(result, dict) and isinstance(result.get("id"), int) else None

    def _continue_codex(self, prompt: str) -> subprocess.CompletedProcess[str]:
        codex = self._resolve_executable(self.codex_executable) or self.codex_executable
        codex_sqlite_home = (self.project_root / "state" / "codex-sqlite").resolve()
        codex_sqlite_home.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        if not environment.get("HOME") and environment.get("USERPROFILE"):
            environment["HOME"] = environment["USERPROFILE"]
        environment["CODEX_SQLITE_HOME"] = str(codex_sqlite_home)
        try:
            return self.command_runner([codex, "exec", "--sandbox", "workspace-write", "--json", prompt], cwd=self.project_root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, check=False, shell=False, stdin=subprocess.DEVNULL, env=environment)
        except (OSError, subprocess.TimeoutExpired):
            return subprocess.CompletedProcess([codex], 1, "", "continuation failed")

    @staticmethod
    def _resume_prompt(report_id: str, response_body: str) -> str:
        return (
            "A matching ChatGPT reviewer response arrived on the operational GitHub reviewer bus.\n"
            f"REPORT_ID: {report_id}\n"
            "This is a fresh continuation turn, not a resumed desktop/CLI thread. Reconstruct authoritative context from AGENTS.md, docs/WORKING_RULES.md, docs/CURRENT_WORK.md, relevant engineering history, persisted Day state, and the active plan/runbook before acting. Apply the complete reviewer response and use the minimum sufficient action. Do not access GitHub or publish a report yourself. End with exactly one JSON object: {\"action\":\"POST_REPORT\"|\"NO_REPORT\"|\"HUMAN_REQUIRED\",\"report_id\":\"...\",\"report_type\":\"PROGRESS_UPDATE\"|\"DECISION_REQUEST\"|\"COMPLETION_REPORT\",\"body\":\"...\"}. For POST_REPORT, body must be the complete report and report_id/report_type must match its fields. The watcher owns GitHub delivery.\n\n"
            "REVIEWER_RESPONSE:\n"
            f"{response_body.strip()}\n"
            "Human approvals received directly in the Codex chat are valid operational authority when recorded "
            "with exact text, subject and limits as RECORDED_DIRECT_CONVERSATION. GitHub is the shared audit copy. "
            "Do not request the same approval solely because the Reviewer cannot view that chat. Read the "
            "linked AUTHORITY_RECORD; missing records are a Codex delivery problem first. For a resolved human "
            "wait use a new confirmation REPORT_ID, carrying CONFIRMS_REPORT_ID, CONFIRMS_RESPONSE_ID, DECISION_ID, "
            "AUTHORITY_RECORD and the unchanged REVIEWED_COMMIT. Do not replay an old reply or broaden scope.\n"
        )

    @staticmethod
    def _extract_envelope(output: str) -> dict[str, object] | None:
        candidates = [output.strip()]
        for line in output.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            candidates.extend(ReviewerBusWatcher._json_strings(value))
        for candidate in reversed(candidates):
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and isinstance(value.get("action"), str):
                return value
        return None

    @staticmethod
    def _json_strings(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [text for item in value for text in ReviewerBusWatcher._json_strings(item)]
        if isinstance(value, dict):
            return [text for item in value.values() for text in ReviewerBusWatcher._json_strings(item)]
        return []

    @staticmethod
    def _validated_report_body(envelope: dict[str, object]) -> str | None:
        report_id, report_type, body = envelope.get("report_id"), envelope.get("report_type"), envelope.get("body")
        if not isinstance(report_id, str) or not report_id or not isinstance(report_type, str) or report_type not in REPORT_TYPES or not isinstance(body, str):
            return None
        if ReviewerBusWatcher._extract(REPORT_ID_RE, body) != report_id or ReviewerBusWatcher._extract(REPORT_TYPE_RE, body) != report_type:
            return None
        return body

    def _mark_response_applied(self, response_id: int | str, report_id: str, **changes: object) -> None:
        self._resolve_confirmed_wait(report_id, response_id)
        processed = list(self._state.get("processed_report_ids", []))
        if report_id not in processed:
            processed.append(report_id)
        changes.setdefault("outstanding_report_id", None)
        changes.setdefault("outstanding_report_comment_id", None)
        self._update_report(report_id, state="HUMAN_REQUIRED" if self._state.get("last_continuation_action") == "HUMAN_REQUIRED" else "APPLIED",
                            response_comment_id=response_id if isinstance(response_id, int) else None, applied_at=_utc_now())
        self._set_state(last_applied_response_comment_id=response_id if isinstance(response_id, int) else None,
                        last_applied_response_file_id=response_id if isinstance(response_id, str) else None,
                        processed_report_ids=processed, pending_response_file=None, pending_response_comment_id=None, pending_response_body=None, pending_response_report_id=None, last_error=None, **changes)

    def _load_state(self) -> dict[str, object]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def _set_state(self, **changes: object) -> None:
        with self._lock:
            self._state.update(changes)
            self._state.setdefault("review_repo", REVIEW_REPO)
            self._state.setdefault("review_pr", REVIEW_PR)
            self._state.setdefault("poll_seconds", self.poll_seconds)
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            for attempt in range(10):
                try:
                    temporary.replace(self.state_path)
                    return
                except PermissionError:
                    if attempt == 9:
                        raise
                    sleep(0.1)

    @staticmethod
    def _extract(pattern: re.Pattern[str], body: str) -> str | None:
        match = pattern.search(body)
        return match.group(1).strip() if match else None

    @staticmethod
    def _resolve_executable(value: str) -> str | None:
        candidate = Path(value)
        if candidate.is_file():
            return str(candidate.resolve())
        found = shutil.which(value)
        return str(Path(found).resolve()) if found else None
