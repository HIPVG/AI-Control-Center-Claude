import json
import os
import shutil
import subprocess
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from backend.control.context_broker import TaskContext
from backend.models.result import ExecutionResult, ProcessDiagnostics, TokenUsage
from backend.models.runtime import CodexRuntimeConfig

SMOKE_FILENAME = "smoke.txt"
SMOKE_CONTENT = "AI Control Center Codex smoke test PASS"
MAX_CAPTURE_CHARS = 2000
MAX_INVALID_LINE_CHARS = 200


class JsonlParseResult(BaseModel):
    output_line_count: int = 0
    event_count: int = 0
    malformed_lines: int = 0
    invalid_line_summary: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    event_types: list[str] = Field(default_factory=list)
    first_error_event: str | None = None
    thread_started: bool = False
    turn_started: bool = False
    turn_completed: bool = False
    turn_failed: bool = False
    error_event: bool = False
    final_output: str | None = None


class StructuredCodexResult(BaseModel):
    """Bounded read-only Codex role result; raw JSONL is never persisted."""

    status: str
    output_text: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    diagnostics: ProcessDiagnostics | None = None
    error_code: str | None = None
    safe_message: str | None = None
    duration_ms: float = Field(default=0, ge=0)


class SmokeWorkspace:
    """Owns the only directory that a smoke runner may use."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def create_run(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        run_directory = (self.root / uuid4().hex).resolve()
        self._ensure_within_root(run_directory)
        run_directory.mkdir()
        return run_directory

    def result_path(self, run_directory: Path) -> Path:
        resolved_run = run_directory.resolve()
        self._ensure_within_root(resolved_run)
        target = (resolved_run / SMOKE_FILENAME).resolve()
        self._ensure_within_root(target)
        return target

    def accepts(self, target: Path) -> bool:
        return self.acceptance_error(target) is None

    def acceptance_error(self, target: Path) -> str | None:
        resolved_target = target.resolve()
        self._ensure_within_root(resolved_target)
        if not resolved_target.is_file():
            return "SMOKE_RESULT_MISSING"
        try:
            content = resolved_target.read_text(encoding="utf-8")
            if content.endswith("\r\n"):
                content = content[:-2]
            elif content.endswith("\n"):
                content = content[:-1]
            return None if content == SMOKE_CONTENT else "SMOKE_CONTENT_MISMATCH"
        except (OSError, UnicodeError):
            return "SMOKE_CONTENT_MISMATCH"

    def _ensure_within_root(self, candidate: Path) -> None:
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("smoke path must remain inside the configured smoke directory") from exc


class CodexRunner:
    """Isolated adapter boundary for `codex exec --json`."""

    @staticmethod
    def parse_usage(raw_json: str) -> TokenUsage:
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            return TokenUsage()
        return CodexRunner._usage_from_payload(payload)

    @classmethod
    def parse_jsonl(cls, raw_jsonl: str) -> JsonlParseResult:
        parsed = JsonlParseResult(output_line_count=len(raw_jsonl.splitlines()))
        latest_usage = TokenUsage()
        for line in raw_jsonl.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                parsed.malformed_lines += 1
                if parsed.invalid_line_summary is None:
                    parsed.invalid_line_summary = line[:MAX_INVALID_LINE_CHARS]
                continue
            parsed.event_count += 1
            if isinstance(payload, dict) and isinstance(payload.get("type"), str):
                event_type = payload["type"]
                if event_type not in parsed.event_types:
                    parsed.event_types.append(event_type)
                parsed.thread_started = parsed.thread_started or event_type == "thread.started"
                parsed.turn_started = parsed.turn_started or event_type == "turn.started"
                parsed.turn_completed = parsed.turn_completed or event_type == "turn.completed"
                parsed.turn_failed = parsed.turn_failed or event_type == "turn.failed"
                parsed.error_event = parsed.error_event or event_type == "error"
                if event_type in {"turn.failed", "error"} and parsed.first_error_event is None:
                    parsed.first_error_event = str(payload.get("message") or payload.get("error") or event_type)[:MAX_INVALID_LINE_CHARS]
                message = cls._agent_message_text(payload)
                if message is not None:
                    parsed.final_output = message
            usage = cls._usage_from_payload(payload)
            if usage.available:
                latest_usage = usage
        parsed.token_usage = latest_usage
        return parsed

    @staticmethod
    def _agent_message_text(payload: dict[str, Any]) -> str | None:
        """Extract only the final agent message from known Codex JSONL events."""
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            return None
        text = item.get("text")
        return text if isinstance(text, str) else None

    @staticmethod
    def _usage_from_payload(payload: Any) -> TokenUsage:
        latest = TokenUsage()

        def visit(value: Any) -> None:
            nonlocal latest
            if isinstance(value, dict):
                keys = {"input_tokens", "cached_input_tokens", "output_tokens"}
                if keys.intersection(value):
                    latest = TokenUsage(
                        input_tokens=CodexRunner._safe_token(value.get("input_tokens")),
                        cached_input_tokens=CodexRunner._safe_token(value.get("cached_input_tokens")),
                        output_tokens=CodexRunner._safe_token(value.get("output_tokens")),
                        available=True,
                    )
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(payload)
        return latest

    @staticmethod
    def _safe_token(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class MockCodexRunner(CodexRunner):
    def run(self, context: TaskContext) -> ExecutionResult:
        return ExecutionResult(
            status="completed",
            files_changed=[context.allowed_files[0]],
            tests_run=context.acceptance_tests,
            test_result="pass",
            summary="Mock builder applied an in-scope change.",
            token_usage=TokenUsage(),
        )


class RealCodexRunner(CodexRunner):
    def __init__(self, config: CodexRuntimeConfig) -> None:
        self.config = config

    def run_smoke(self, smoke_directory: Path, target: Path) -> ExecutionResult:
        prompt = f"Create {target.name} in the current working directory containing exactly this text: {SMOKE_CONTENT}"
        return self._run_isolated_file_change(smoke_directory, target, prompt)

    def run_isolated_file_change(self, working_directory: Path, target: Path, expected_content: str) -> ExecutionResult:
        prompt = (
            f"Change {target.name} in the current directory so that its exact content is:\n"
            f"{expected_content}\n"
            "Do not modify any other file. No explanation is required."
        )
        return self._run_isolated_file_change(working_directory, target, prompt)

    def run_worktree_task(self, working_directory: Path, prompt: str) -> ExecutionResult:
        """Run a bounded task prompt in an already-created Git worktree."""
        return self._run_command(working_directory, prompt, skip_git_repo_check=False, changed_file=None)

    def run_readonly_structured(self, working_directory: Path, prompt: str, schema_path: Path) -> StructuredCodexResult:
        """Execute one read-only Codex role call with an explicit JSON schema."""
        started = monotonic()
        workspace = working_directory.resolve()
        schema = schema_path.resolve()
        try:
            schema.relative_to(workspace)
        except ValueError:
            return StructuredCodexResult(status="failed", error_code="CODEX_SCHEMA_OUTSIDE_ROLE_WORKSPACE", safe_message="Codex role schema must remain in its isolated workspace.")
        if not workspace.is_dir() or not schema.is_file():
            return StructuredCodexResult(status="failed", error_code="CODEX_ROLE_WORKSPACE_INVALID", safe_message="Codex role workspace or schema is unavailable.")
        executable_path, command = self.build_structured_readonly_command(prompt, schema)
        codex_home, codex_sqlite_home = self.resolve_codex_home(), self.resolve_codex_sqlite_home()
        if codex_home is None or codex_sqlite_home is None:
            code = "CODEX_HOME_NOT_FOUND" if codex_home is None else "CODEX_SQLITE_HOME_NOT_FOUND"
            return StructuredCodexResult(
                status="failed", error_code=code, safe_message="Configured Codex runtime directory was not found.",
                diagnostics=self._diagnostics(command, workspace, executable_path=executable_path),
                duration_ms=round((monotonic() - started) * 1000, 2),
            )
        try:
            completed = subprocess.run(
                command, cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.config.timeout_seconds, check=False, shell=False, stdin=subprocess.DEVNULL,
                env=self._process_environment(codex_home, codex_sqlite_home),
            )
        except FileNotFoundError:
            return StructuredCodexResult(status="failed", error_code="CODEX_NOT_FOUND", safe_message="Configured Codex executable was not found.", duration_ms=round((monotonic() - started) * 1000, 2))
        except subprocess.TimeoutExpired as exc:
            return StructuredCodexResult(
                status="failed", error_code="CODEX_TIMEOUT", safe_message="Codex role execution timed out.",
                diagnostics=self._diagnostics(command, workspace, executable_path=executable_path, timed_out=True, stderr=exc.stderr),
                duration_ms=round((monotonic() - started) * 1000, 2),
            )
        parsed = self.parse_jsonl(completed.stdout)
        diagnostics = self._diagnostics(command, workspace, executable_path=executable_path, exit_code=completed.returncode, parsed=parsed, stderr=completed.stderr)
        duration = round((monotonic() - started) * 1000, 2)
        if completed.returncode != 0 or parsed.turn_failed or parsed.error_event or not parsed.turn_completed:
            return StructuredCodexResult(status="failed", error_code="CODEX_ROLE_FAILED", safe_message="Codex role did not complete successfully.", token_usage=parsed.token_usage, diagnostics=diagnostics, duration_ms=duration)
        if not parsed.final_output:
            return StructuredCodexResult(status="failed", error_code="CODEX_STRUCTURED_OUTPUT_MISSING", safe_message="Codex role returned no structured output.", token_usage=parsed.token_usage, diagnostics=diagnostics, duration_ms=duration)
        return StructuredCodexResult(status="completed", output_text=parsed.final_output, token_usage=parsed.token_usage, diagnostics=diagnostics, duration_ms=duration)

    def _run_isolated_file_change(self, working_directory: Path, target: Path, prompt: str) -> ExecutionResult:
        smoke_directory = working_directory.resolve()
        if target.resolve().parent != smoke_directory:
            return ExecutionResult(
                status="failed",
                test_result="not_run",
                summary="Codex target must be a direct child of the isolated working directory.",
                error_code="SMOKE_TARGET_OUTSIDE_WORKSPACE",
            )
        return self._run_command(smoke_directory, prompt, skip_git_repo_check=True, changed_file=target.name)

    def _run_command(self, working_directory: Path, prompt: str, *, skip_git_repo_check: bool, changed_file: str | None) -> ExecutionResult:
        smoke_directory = working_directory.resolve()
        if not smoke_directory.is_dir():
            return ExecutionResult(
                status="failed",
                test_result="not_run",
                summary="Codex working directory does not exist.",
                error_code="CODEX_WORKING_DIRECTORY_NOT_FOUND",
            )
        executable_path, command = self.build_smoke_command(prompt, skip_git_repo_check=skip_git_repo_check)
        codex_home = self.resolve_codex_home()
        if codex_home is None:
            return ExecutionResult(
                status="failed",
                test_result="not_run",
                summary="Configured Codex home directory was not found.",
                error_code="CODEX_HOME_NOT_FOUND",
                diagnostics=self._diagnostics(command, smoke_directory, executable_path=executable_path),
            )
        codex_sqlite_home = self.resolve_codex_sqlite_home()
        if codex_sqlite_home is None:
            return ExecutionResult(
                status="failed",
                test_result="not_run",
                summary="Configured Codex SQLite directory was not found.",
                error_code="CODEX_SQLITE_HOME_NOT_FOUND",
                diagnostics=self._diagnostics(command, smoke_directory, executable_path=executable_path),
            )
        try:
            completed = subprocess.run(
                command,
                cwd=smoke_directory,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.config.timeout_seconds,
                check=False,
                shell=False,
                stdin=subprocess.DEVNULL,
                env=self._process_environment(codex_home, codex_sqlite_home),
            )
        except FileNotFoundError:
            return ExecutionResult(
                status="failed",
                test_result="not_run",
                summary="Configured Codex executable was not found.",
                error_code="CODEX_NOT_FOUND",
                diagnostics=self._diagnostics(command, smoke_directory, executable_path=executable_path),
            )
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult(
                status="failed",
                test_result="not_run",
                summary="Codex smoke execution timed out.",
                error_code="CODEX_TIMEOUT",
                stderr=self._truncate(exc.stderr),
                diagnostics=self._diagnostics(
                    command,
                    smoke_directory,
                    executable_path=executable_path,
                    timed_out=True,
                    stderr=exc.stderr,
                ),
            )

        parsed = self.parse_jsonl(completed.stdout)
        diagnostics = self._diagnostics(
            command,
            smoke_directory,
            executable_path=executable_path,
            exit_code=completed.returncode,
            parsed=parsed,
            stderr=completed.stderr,
        )
        if completed.returncode != 0:
            return ExecutionResult(
                status="failed",
                test_result="not_run",
                summary="Codex smoke execution returned a non-zero exit code.",
                token_usage=parsed.token_usage,
                exit_code=completed.returncode,
                error_code="CODEX_TURN_FAILED" if parsed.turn_failed else "CODEX_EXIT_NONZERO",
                stderr=self._truncate(completed.stderr),
                diagnostics=diagnostics,
            )
        if parsed.malformed_lines or parsed.turn_failed or parsed.error_event or not parsed.turn_completed:
            return ExecutionResult(
                status="failed",
                test_result="not_run",
                summary="Codex did not produce a successful completed turn event.",
                token_usage=parsed.token_usage,
                exit_code=completed.returncode,
                error_code="CODEX_TURN_FAILED" if parsed.turn_failed else "CODEX_OUTPUT_INVALID",
                stderr=self._truncate(completed.stderr),
                diagnostics=diagnostics,
            )
        return ExecutionResult(
            status="completed",
            files_changed=[changed_file] if changed_file else [],
            tests_run=["deterministic smoke file check"],
            test_result="pending",
            summary="Codex smoke execution completed.",
            token_usage=parsed.token_usage,
            exit_code=completed.returncode,
            stderr=self._truncate(completed.stderr),
            diagnostics=diagnostics,
        )

    def build_smoke_command(self, prompt: str, *, skip_git_repo_check: bool = True) -> tuple[str | None, list[str]]:
        executable_path = self.resolve_executable()
        executable = executable_path or self.config.executable
        arguments = [
            executable,
            "exec",
            "--sandbox",
            "workspace-write",
            "--json",
            prompt,
        ]
        if skip_git_repo_check:
            arguments.insert(4, "--skip-git-repo-check")
        if Path(executable).suffix.lower() in {".cmd", ".bat"}:
            # A Windows command shim cannot be launched reliably with
            # CreateProcess. Invoke cmd explicitly while retaining shell=False.
            command_processor = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
            return executable_path, [command_processor, "/d", "/s", "/c", subprocess.list2cmdline(arguments)]
        return executable_path, arguments

    def build_structured_readonly_command(self, prompt: str, schema_path: Path) -> tuple[str | None, list[str]]:
        """Build the non-interactive, read-only command used by Architect/Reviewer."""
        executable_path = self.resolve_executable()
        executable = executable_path or self.config.executable
        arguments = [
            executable, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--json",
            "--output-schema", str(schema_path), prompt,
        ]
        if Path(executable).suffix.lower() in {".cmd", ".bat"}:
            command_processor = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
            return executable_path, [command_processor, "/d", "/s", "/c", subprocess.list2cmdline(arguments)]
        return executable_path, arguments

    def resolve_executable(self) -> str | None:
        configured = Path(self.config.executable)
        if configured.is_file():
            return str(configured.resolve())
        resolved = shutil.which(self.config.executable)
        return str(Path(resolved).resolve()) if resolved else None

    @staticmethod
    def resolve_codex_home() -> Path | None:
        configured = os.environ.get("CODEX_HOME")
        if configured:
            candidate = Path(configured)
        elif os.environ.get("USERPROFILE"):
            candidate = Path(os.environ["USERPROFILE"]) / ".codex"
        else:
            return None
        return candidate.resolve() if candidate.is_dir() else None

    @staticmethod
    def resolve_codex_sqlite_home() -> Path | None:
        configured = os.environ.get("CODEX_SQLITE_HOME")
        candidate = Path(configured) if configured else Path(__file__).resolve().parents[2] / "state" / "codex-sqlite"
        return candidate.resolve() if candidate.is_dir() else None

    @staticmethod
    def _process_environment(
        codex_home: Path | None = None,
        codex_sqlite_home: Path | None = None,
    ) -> dict[str, str]:
        """Preserve the inherited environment and supply Codex's verified home."""
        environment = os.environ.copy()
        if not environment.get("HOME") and environment.get("USERPROFILE"):
            environment["HOME"] = environment["USERPROFILE"]
        if codex_home is not None:
            environment["CODEX_HOME"] = str(codex_home)
        if codex_sqlite_home is not None:
            environment["CODEX_SQLITE_HOME"] = str(codex_sqlite_home)
        return environment

    @staticmethod
    def _diagnostics(
        command: list[str],
        smoke_directory: Path,
        *,
        executable_path: str | None = None,
        exit_code: int | None = None,
        timed_out: bool = False,
        parsed: JsonlParseResult | None = None,
        stderr: str | bytes | None = None,
    ) -> ProcessDiagnostics:
        return ProcessDiagnostics(
            executable_path=executable_path,
            argv=command,
            cwd=str(smoke_directory),
            exit_code=exit_code,
            timed_out=timed_out,
            stdin_closed=True,
            stdout_line_count=parsed.output_line_count if parsed else 0,
            stdout_event_count=parsed.event_count if parsed else 0,
            invalid_json_lines=parsed.malformed_lines if parsed else 0,
            invalid_line_summary=parsed.invalid_line_summary if parsed else None,
            event_types=parsed.event_types if parsed else [],
            first_error_event=parsed.first_error_event if parsed else None,
            thread_started=parsed.thread_started if parsed else False,
            turn_started=parsed.turn_started if parsed else False,
            turn_completed=parsed.turn_completed if parsed else False,
            turn_failed=parsed.turn_failed if parsed else False,
            error_event=parsed.error_event if parsed else False,
            stderr_summary=RealCodexRunner._truncate(stderr),
        )

    @staticmethod
    def _truncate(value: str | bytes | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        return value[:MAX_CAPTURE_CHARS] or None
