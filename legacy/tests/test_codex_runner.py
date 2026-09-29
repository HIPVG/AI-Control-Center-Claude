import subprocess
from pathlib import Path

import pytest

from backend.control.token_budget import BudgetConfig, CodexBudget, TokenBudgetManager
from backend.models.result import ExecutionResult, TokenUsage
from backend.models.runtime import CodexMode, CodexRuntimeConfig, RuntimeConfig
from backend.orchestrator.engine import ControlCenterEngine
from backend.runners.codex import CodexRunner, RealCodexRunner, SMOKE_CONTENT, SmokeWorkspace


def real_config(executable="codex"):
    return CodexRuntimeConfig(mode=CodexMode.REAL, executable=executable, timeout_seconds=5)


@pytest.fixture(autouse=True)
def verified_codex_home(monkeypatch, tmp_path):
    home = tmp_path / "codex-home"
    sqlite_home = tmp_path / "codex-sqlite"
    home.mkdir()
    sqlite_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(sqlite_home))
    return home, sqlite_home


def test_default_runtime_mode_is_mock():
    assert RuntimeConfig().codex.mode == CodexMode.MOCK


def test_jsonl_usage_parsing_uses_reported_values_only():
    parsed = CodexRunner.parse_jsonl('{"type":"started"}\n{"usage":{"input_tokens":12,"cached_input_tokens":3,"output_tokens":4}}')
    assert parsed.malformed_lines == 0
    assert parsed.token_usage == TokenUsage(input_tokens=12, cached_input_tokens=3, output_tokens=4, available=True)


def test_token_usage_tracks_gross_cached_and_uncached_input():
    usage = TokenUsage(input_tokens=10, cached_input_tokens=4, output_tokens=2, available=True)
    assert usage.gross_input_tokens == 10
    assert usage.cached_input_tokens == 4
    assert usage.uncached_input_tokens == 6
    assert TokenUsage(input_tokens=3, cached_input_tokens=8).uncached_input_tokens == 0


def test_utf8_jsonl_records_japanese_events_and_token_usage():
    parsed = CodexRunner.parse_jsonl(
        '{"type":"thread.started","message":"開始"}\n'
        '{"type":"turn.completed","usage":{"input_tokens":12,"cached_input_tokens":3,"output_tokens":4}}'
    )
    assert parsed.output_line_count == 2
    assert parsed.event_count == 2
    assert parsed.malformed_lines == 0
    assert parsed.thread_started
    assert parsed.turn_completed
    assert parsed.token_usage.output_tokens == 4


def test_malformed_jsonl_is_reported_without_raising(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "backend.runners.codex.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="not-json", stderr=""),
    )
    workspace = SmokeWorkspace(tmp_path / "smoke")
    run = workspace.create_run()
    result = RealCodexRunner(real_config()).run_smoke(run, workspace.result_path(run))
    assert result.error_code == "CODEX_OUTPUT_INVALID"


def test_malformed_jsonl_keeps_bounded_invalid_line_diagnostics():
    parsed = CodexRunner.parse_jsonl("not-json\n" + ("x" * 300) + "\n{\"type\":\"thread.started\"}")
    assert parsed.output_line_count == 3
    assert parsed.event_count == 1
    assert parsed.malformed_lines == 2
    assert parsed.invalid_line_summary == "not-json"


def test_nonzero_exit_keeps_utf8_stderr_and_exit_code(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "backend.runners.codex.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 7, stdout='{"type":"thread.started"}\nnot-json', stderr="エラー: 中断"
        ),
    )
    workspace = SmokeWorkspace(tmp_path / "smoke")
    run = workspace.create_run()
    result = RealCodexRunner(real_config()).run_smoke(run, workspace.result_path(run))
    assert result.exit_code == 7
    assert result.stderr == "エラー: 中断"
    assert result.diagnostics.stdout_line_count == 2
    assert result.diagnostics.invalid_json_lines == 1
    assert result.diagnostics.event_types == ["thread.started"]


def test_error_event_records_a_bounded_first_error_message():
    parsed = CodexRunner.parse_jsonl('{"type":"error","message":"sandbox denied"}')
    assert parsed.first_error_event == "sandbox denied"


def test_executable_not_found_is_structured_error(tmp_path):
    workspace = SmokeWorkspace(tmp_path / "smoke")
    run = workspace.create_run()
    result = RealCodexRunner(real_config("missing-codex-executable")).run_smoke(run, workspace.result_path(run))
    assert result.error_code == "CODEX_NOT_FOUND"


def test_timeout_is_structured_error(monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], stderr="timed out")

    monkeypatch.setattr("backend.runners.codex.subprocess.run", timeout)
    workspace = SmokeWorkspace(tmp_path / "smoke")
    run = workspace.create_run()
    result = RealCodexRunner(real_config()).run_smoke(run, workspace.result_path(run))
    assert result.error_code == "CODEX_TIMEOUT"


def test_real_smoke_uses_workspace_write_with_controlled_process_arguments(monkeypatch, tmp_path):
    captured = {}

    def completed(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        stdout = '{"type":"thread.started"}\n{"type":"turn.started"}\n{"type":"turn.completed","usage":{"input_tokens":5,"cached_input_tokens":1,"output_tokens":2}}'
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("backend.runners.codex.subprocess.run", completed)
    workspace = SmokeWorkspace(tmp_path / "smoke")
    run = workspace.create_run()
    monkeypatch.setattr("backend.runners.codex.shutil.which", lambda _: r"C:\\Tools\\codex.exe")
    result = RealCodexRunner(real_config()).run_smoke(run, workspace.result_path(run))
    assert result.status == "completed"
    assert captured["command"][0].endswith("codex.exe")
    assert captured["command"][1:6] == ["exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "--json"]
    assert captured["kwargs"]["cwd"] == run
    assert captured["kwargs"]["capture_output"]
    assert captured["kwargs"]["text"]
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "replace"
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL
    assert isinstance(captured["kwargs"]["env"], dict)
    assert captured["kwargs"]["env"]["CODEX_HOME"].endswith("codex-home")
    assert captured["kwargs"]["env"]["CODEX_SQLITE_HOME"].endswith("codex-sqlite")
    assert result.diagnostics.stdout_event_count == 3
    assert result.diagnostics.executable_path.endswith("codex.exe")
    assert result.diagnostics.thread_started
    assert result.diagnostics.turn_started
    assert result.diagnostics.turn_completed
    assert result.token_usage.available


def test_readonly_structured_role_uses_schema_and_extracts_only_agent_message(monkeypatch, tmp_path):
    captured = {}

    def completed(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(
            command, 0,
            stdout=(
                '{"type":"thread.started"}\n'
                '{"type":"turn.started"}\n'
                '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"decision\\":\\"DAY_COMPLETE\\"}"}}\n'
                '{"type":"turn.completed","usage":{"input_tokens":9,"cached_input_tokens":2,"output_tokens":1}}'
            ), stderr="",
        )

    monkeypatch.setattr("backend.runners.codex.subprocess.run", completed)
    workspace = tmp_path / "role"
    workspace.mkdir()
    schema = workspace / "output-schema.json"
    schema.write_text('{"type":"object"}', encoding="utf-8")
    result = RealCodexRunner(real_config()).run_readonly_structured(workspace, "choose", schema)
    assert result.status == "completed"
    assert result.output_text == '{"decision":"DAY_COMPLETE"}'
    assert captured["command"][1:8] == ["exec", "--sandbox", "read-only", "--skip-git-repo-check", "--json", "--output-schema", str(schema)]
    assert result.diagnostics.turn_completed
    assert result.token_usage.uncached_input_tokens == 7


def test_isolated_file_change_uses_only_direct_child_target(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "backend.runners.codex.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout='{"type":"turn.completed"}', stderr=""),
    )
    workdir = tmp_path / "fixture"
    workdir.mkdir()
    result = RealCodexRunner(real_config()).run_isolated_file_change(workdir, workdir / "status.txt", "PASS")
    assert result.status == "completed"
    rejected = RealCodexRunner(real_config()).run_isolated_file_change(workdir, tmp_path / "status.txt", "PASS")
    assert rejected.error_code == "SMOKE_TARGET_OUTSIDE_WORKSPACE"


def test_worktree_command_keeps_git_repository_check_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "backend.runners.codex.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout='{"type":"turn.completed"}', stderr=""),
    )
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    result = RealCodexRunner(real_config()).run_worktree_task(worktree, "fix only allowed file")
    assert result.status == "completed"
    assert "--skip-git-repo-check" not in result.diagnostics.argv


def test_windows_executable_is_resolved_before_execution(monkeypatch):
    monkeypatch.setattr("backend.runners.codex.shutil.which", lambda _: r"C:\\OpenAI\\bin\\codex.exe")
    runner = RealCodexRunner(real_config())
    path, command = runner.build_smoke_command("prompt")
    assert path.endswith("codex.exe")
    assert command[0].endswith("codex.exe")
    assert command[-1] == "prompt"


def test_missing_executable_keeps_configured_name_for_structured_not_found(monkeypatch):
    monkeypatch.setattr("backend.runners.codex.shutil.which", lambda _: None)
    path, command = RealCodexRunner(real_config("codex")).build_smoke_command("prompt")
    assert path is None
    assert command[0] == "codex"


def test_windows_command_shim_uses_explicit_command_processor(monkeypatch):
    monkeypatch.setattr("backend.runners.codex.shutil.which", lambda _: r"C:\\OpenAI\\bin\\codex.cmd")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    path, command = RealCodexRunner(real_config()).build_smoke_command("prompt with spaces")
    assert path.endswith("codex.cmd")
    assert command[:4] == [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c"]
    assert "codex.cmd" in command[4]
    assert "prompt with spaces" in command[4]


def test_windows_profile_is_mapped_to_home_for_codex(monkeypatch):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", r"C:\\Users\\control-center")
    environment = RealCodexRunner._process_environment()
    assert environment["HOME"] == r"C:\\Users\\control-center"


def test_environment_preserves_inherited_values_and_sets_verified_codex_home(monkeypatch, tmp_path):
    home = tmp_path / "verified-codex-home"
    sqlite_home = tmp_path / "verified-codex-sqlite"
    home.mkdir()
    sqlite_home.mkdir()
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", r"C:\\Users\\control-center")
    monkeypatch.setenv("PATH", r"C:\\Windows\\System32")
    monkeypatch.setenv("HOMEDRIVE", "C:")
    monkeypatch.setenv("HOMEPATH", r"\\Users\\control-center")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\\Users\\control-center\\AppData\\Local")
    monkeypatch.setenv("APPDATA", r"C:\\Users\\control-center\\AppData\\Roaming")
    environment = RealCodexRunner._process_environment(home, sqlite_home)
    assert environment["CODEX_HOME"] == str(home)
    assert environment["CODEX_SQLITE_HOME"] == str(sqlite_home)
    assert environment["HOME"] == r"C:\\Users\\control-center"
    assert environment["USERPROFILE"] == r"C:\\Users\\control-center"
    assert environment["PATH"] == r"C:\\Windows\\System32"
    assert environment["HOMEDRIVE"] == "C:"
    assert environment["HOMEPATH"] == r"\\Users\\control-center"
    assert environment["LOCALAPPDATA"].endswith("Local")
    assert environment["APPDATA"].endswith("Roaming")


def test_missing_explicit_codex_home_is_rejected_without_starting_subprocess(monkeypatch, tmp_path):
    called = False

    def unexpected_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess must not start")

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-codex-home"))
    monkeypatch.setattr("backend.runners.codex.subprocess.run", unexpected_run)
    workspace = SmokeWorkspace(tmp_path / "smoke")
    run = workspace.create_run()
    result = RealCodexRunner(real_config()).run_smoke(run, workspace.result_path(run))
    assert result.error_code == "CODEX_HOME_NOT_FOUND"
    assert not called


def test_missing_codex_sqlite_home_is_rejected_without_starting_subprocess(monkeypatch, tmp_path):
    called = False

    def unexpected_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess must not start")

    monkeypatch.setenv("CODEX_SQLITE_HOME", str(tmp_path / "missing-codex-sqlite"))
    monkeypatch.setattr("backend.runners.codex.subprocess.run", unexpected_run)
    workspace = SmokeWorkspace(tmp_path / "smoke")
    run = workspace.create_run()
    result = RealCodexRunner(real_config()).run_smoke(run, workspace.result_path(run))
    assert result.error_code == "CODEX_SQLITE_HOME_NOT_FOUND"
    assert not called


def test_diagnostics_do_not_include_environment_or_authentication_material():
    diagnostics = RealCodexRunner._diagnostics(["codex", "exec"], Path("smoke"))
    serialized = diagnostics.model_dump_json().lower()
    assert "codex_home" not in serialized
    assert "auth" not in serialized


def test_turn_failed_event_is_structured_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "backend.runners.codex.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout='{"type":"turn.failed"}', stderr="failed"),
    )
    workspace = SmokeWorkspace(tmp_path / "smoke")
    run = workspace.create_run()
    result = RealCodexRunner(real_config()).run_smoke(run, workspace.result_path(run))
    assert result.error_code == "CODEX_TURN_FAILED"
    assert result.diagnostics.event_types == ["turn.failed"]


def test_smoke_workspace_rejects_path_outside_root(tmp_path):
    workspace = SmokeWorkspace(tmp_path / "smoke")
    with pytest.raises(ValueError):
        workspace.result_path(tmp_path)


def test_smoke_acceptance_is_deterministic(tmp_path):
    workspace = SmokeWorkspace(tmp_path / "smoke")
    run = workspace.create_run()
    target = workspace.result_path(run)
    target.write_text(SMOKE_CONTENT, encoding="utf-8")
    assert workspace.accepts(target)
    target.write_text(f"{SMOKE_CONTENT}\n", encoding="utf-8")
    assert workspace.accepts(target)
    target.write_bytes(f"{SMOKE_CONTENT}\r\n".encode("utf-8"))
    assert workspace.accepts(target)
    target.write_text(f"{SMOKE_CONTENT}\n\n", encoding="utf-8")
    assert not workspace.accepts(target)
    target.write_text(f" {SMOKE_CONTENT}", encoding="utf-8")
    assert not workspace.accepts(target)


def test_real_smoke_is_rejected_while_mode_is_mock(tmp_path):
    engine = ControlCenterEngine(runtime_config=RuntimeConfig(), smoke_root=tmp_path / "smoke")
    result = engine.run_codex_smoke()
    assert result["status"] == "rejected"
    assert result["error_code"] == "REAL_MODE_REQUIRED"


class SmokeWriter:
    def __init__(self):
        self.called = False

    def run_smoke(self, smoke_directory, target):
        self.called = True
        target.write_text(SMOKE_CONTENT, encoding="utf-8")
        return ExecutionResult(
            status="completed",
            test_result="pending",
            summary="mocked real smoke",
            token_usage=TokenUsage(input_tokens=7, cached_input_tokens=2, output_tokens=3, available=True),
        )


def test_budget_guard_runs_before_real_execution(tmp_path):
    runner = SmokeWriter()
    runtime = RuntimeConfig(codex=real_config())
    engine = ControlCenterEngine(runtime_config=runtime, smoke_root=tmp_path / "smoke", real_runner=runner)
    engine.budgets = TokenBudgetManager(BudgetConfig(codex=CodexBudget(max_retry=0)))
    result = engine.run_codex_smoke()
    assert result["error_code"] == "RETRY_LIMIT_EXCEEDED"
    assert not runner.called


def test_real_smoke_records_actual_reported_usage(tmp_path):
    runtime = RuntimeConfig(codex=real_config())
    engine = ControlCenterEngine(runtime_config=runtime, smoke_root=tmp_path / "smoke", real_runner=SmokeWriter())
    result = engine.run_codex_smoke()
    assert result["status"] == "completed"
    assert result["deterministic_passed"]
    assert engine.status()["token_usage"]["input_tokens"] == 7


def test_completed_smoke_records_post_run_overage_without_rewriting_pass(tmp_path):
    class OverageWriter:
        def run_smoke(self, smoke_directory, target):
            target.write_text(f"{SMOKE_CONTENT}\n", encoding="utf-8")
            return ExecutionResult(
                status="completed",
                test_result="pending",
                summary="completed with warning",
                stderr="recoverable warning",
                token_usage=TokenUsage(input_tokens=50, cached_input_tokens=40, output_tokens=3, available=True),
            )

    runtime = RuntimeConfig(codex=real_config())
    engine = ControlCenterEngine(runtime_config=runtime, smoke_root=tmp_path / "smoke", real_runner=OverageWriter())
    engine.budgets = TokenBudgetManager(BudgetConfig(codex=CodexBudget(task_input_tokens=10, task_output_tokens=10)))
    result = engine.run_codex_smoke()
    assert result["status"] == "completed"
    assert result["deterministic_passed"]
    assert engine.status()["token_usage"]["input_tokens"] == 50
    assert engine.status()["token_usage"]["uncached_input_tokens"] == 10
    assert engine.timeline[-2].event_type.value == "TOKEN_BUDGET_WARNING"
    assert result["execution"]["stderr"] == "recoverable warning"


class NoResultWriter:
    def run_smoke(self, smoke_directory, target):
        return ExecutionResult(status="completed", test_result="pending", summary="no result", token_usage=TokenUsage())


def test_real_smoke_reports_missing_result_file(tmp_path):
    runtime = RuntimeConfig(codex=real_config())
    engine = ControlCenterEngine(runtime_config=runtime, smoke_root=tmp_path / "smoke", real_runner=NoResultWriter())
    result = engine.run_codex_smoke()
    assert result["error_code"] == "SMOKE_RESULT_MISSING"


def test_smoke_start_event_is_persisted_before_runner_execution(tmp_path):
    class Store:
        data = None

        def load(self):
            return None

        def save(self, data):
            self.data = data

    class ObservingRunner:
        def __init__(self, store):
            self.store = store

        def run_smoke(self, smoke_directory, target):
            assert self.store.data["timeline"][-1]["event_type"] == "SMOKE_STARTED"
            target.write_text(SMOKE_CONTENT, encoding="utf-8")
            return ExecutionResult(status="completed", test_result="pending", summary="smoke", token_usage=TokenUsage())

    store = Store()
    engine = ControlCenterEngine(
        state_store=store,
        runtime_config=RuntimeConfig(codex=real_config()),
        smoke_root=tmp_path / "smoke",
        real_runner=ObservingRunner(store),
    )
    assert engine.run_codex_smoke()["status"] == "completed"
