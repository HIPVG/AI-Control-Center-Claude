"""Read-only, commit-pinned GitHub file transport for reviewer responses."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess

BRANCH = "poc/review-loop-report-types-20260924"
LIMIT = 64 * 1024
SHA = re.compile(r"[0-9a-f]{40}")
FIELDS = ("REPORT_ID", "REPORT_TYPE", "RESPONSE_REQUIRED", "RESPONSE_TRANSPORT",
          "REQUEST_COMMIT", "REQUEST_PATH", "RESPONSE_PATH", "REVIEWED_COMMIT",
          "CONFIRMS_REPORT_ID", "CONFIRMS_RESPONSE_ID", "DECISION_ID", "AUTHORITY_RECORD")


class FileReplyError(ValueError):
    pass


def field(body, name):
    values = re.findall(r"(?m)^" + re.escape(name) + r":[ \t]*([^\r\n]*)$", body)
    if len(values) > 1:
        raise FileReplyError("DUPLICATE_" + name)
    return values[0].strip() if values else None


def binding(body):
    values = {key: field(body, key) for key in FIELDS}
    rid = values["REPORT_ID"] or ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", rid):
        raise FileReplyError("INVALID_REPORT_ID")
    if values["RESPONSE_TRANSPORT"] != "github_file":
        raise FileReplyError("INVALID_TRANSPORT")
    if not SHA.fullmatch(values["REQUEST_COMMIT"] or ""):
        raise FileReplyError("INVALID_REQUEST_COMMIT")
    for key, folder in (("REQUEST_PATH", "requests"), ("RESPONSE_PATH", "responses")):
        if values[key] != f"poc/file-review-{folder}/{rid}.md":
            raise FileReplyError("INVALID_" + key)
    return values


def validate_trigger_binding(request_body, trigger_body):
    """Reject a file-mode trigger that does not repeat its request binding.

    The immutable request may omit REQUEST_COMMIT because that value is the commit
    which introduces the request itself.  Every other control field must match the
    top-level PR trigger before it is published.
    """
    trigger = binding(trigger_body)
    request = {key: field(request_body, key) for key in FIELDS}
    for key in FIELDS:
        expected = trigger[key] if key == "REQUEST_COMMIT" and request[key] is None else request[key]
        if expected != trigger[key]:
            raise FileReplyError("REQUEST_BINDING_MISMATCH_" + key)
    return trigger


class GitHubFileReader:
    def __init__(self, watcher, repo, pr):
        self.watcher, self.repo, self.pr = watcher, repo, pr

    def get(self, endpoint, *, missing_ok=False):
        w = self.watcher
        argv = [w._resolve_executable(w.gh_executable) or w.gh_executable,
                "api", f"repos/{self.repo}/{endpoint}"]
        try:
            result = w.command_runner(argv, cwd=w.project_root, capture_output=True,
                text=True, encoding="utf-8", errors="strict", timeout=30, check=False, shell=False)
            data = json.loads(result.stdout)
        except (OSError, subprocess.TimeoutExpired, UnicodeError, json.JSONDecodeError):
            raise FileReplyError("GITHUB_FILE_FETCH_FAILED") from None
        if result.returncode:
            if missing_ok and isinstance(data, dict) and str(data.get("status")) == "404":
                return None
            raise FileReplyError("GITHUB_FILE_FETCH_FAILED")
        if not isinstance(data, dict):
            raise FileReplyError("GITHUB_FILE_OUTPUT_INVALID")
        return data

    def head(self):
        pr = self.get(f"pulls/{self.pr}")
        head = pr.get("head", {})
        if (not isinstance(head, dict) or head.get("ref") != BRANCH or
                not isinstance(head.get("repo"), dict) or
                head["repo"].get("full_name") != self.repo or
                not SHA.fullmatch(str(head.get("sha", "")))):
            raise FileReplyError("GITHUB_FILE_HEAD_INVALID")
        return head["sha"]

    def content(self, path, commit, *, missing_ok=False):
        data = self.get(f"contents/{path}?ref={commit}", missing_ok=missing_ok)
        if data is None:
            return None
        if (data.get("type") != "file" or data.get("path") != path or
                data.get("encoding") != "base64" or
                not isinstance(data.get("size"), int) or not 0 <= data["size"] <= LIMIT or
                not isinstance(data.get("content"), str) or len(data["content"]) > LIMIT * 2):
            raise FileReplyError("GITHUB_FILE_CONTENT_INVALID")
        try:
            raw = base64.b64decode("".join(data["content"].split()), validate=True)
            body = raw.decode("utf-8")
        except (ValueError, UnicodeError):
            raise FileReplyError("GITHUB_FILE_ENCODING_INVALID") from None
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if len(raw) != data["size"] or blob != data.get("sha"):
            raise FileReplyError("GITHUB_FILE_HASH_MISMATCH")
        return body, blob

    def response(self, pin, head):
        request, _ = self.content(pin["REQUEST_PATH"], pin["REQUEST_COMMIT"])
        # REQUEST_COMMIT may be absent inside its own immutable request file.
        for key, expected in pin.items():
            if key != "REQUEST_COMMIT" and field(request, key) != expected:
                raise FileReplyError("REQUEST_BINDING_MISMATCH_" + key)
        value = self.content(pin["RESPONSE_PATH"], head, missing_ok=True)
        if value is None:
            return None
        body, blob = value
        expected = {"IN_REPLY_TO": pin["REPORT_ID"], "REQUEST_COMMIT": pin["REQUEST_COMMIT"],
                    "REQUEST_PATH": pin["REQUEST_PATH"], "RESPONSE_KIND": "GIT_FILE"}
        for key, wanted in expected.items():
            if field(body, key) != wanted:
                raise FileReplyError("RESPONSE_BINDING_MISMATCH_" + key)
        results = {"ACKNOWLEDGED"} if pin["RESPONSE_REQUIRED"] == "no" else {
            "CONTINUE", "DECISION", "ACCEPT_COMPLETE", "REJECT", "HUMAN_REQUIRED"}
        if field(body, "RESULT") not in results or not field(body, "NEXT_ACTION"):
            raise FileReplyError("RESPONSE_RESULT_INVALID")
        return {"id": f"git-file:{blob}:{pin['RESPONSE_PATH']}", "body": body,
                "source": "github_file", "head_commit": head, "blob_sha": blob,
                "path": pin["RESPONSE_PATH"], "request_commit": pin["REQUEST_COMMIT"]}
