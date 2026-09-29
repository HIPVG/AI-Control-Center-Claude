import subprocess
from pathlib import Path

from backend.control.projects import ConfiguredProject, ProjectRegistry, load_project_registry
from backend.control.token_budget import BudgetConfig, CodexBudget, TokenBudgetManager
from backend.models.result import ExecutionResult, ProcessDiagnostics, TokenUsage
from backend.models.runtime import CodexMode, CodexRuntimeConfig, RuntimeConfig
from backend.models.task import TaskType, WorkOrder
from backend.orchestrator.engine import ControlCenterEngine


def real_runtime() -> RuntimeConfig:
    return RuntimeConfig(codex=CodexRuntimeConfig(mode=CodexMode.REAL, executable="codex"))


def configured_project(path: Path) -> ProjectRegistry:
    return ProjectRegistry(projects={
        "local_llm_lab": ConfiguredProject(name="LocalLLM-Lab", path=path, default_branch="main"),
    })


def git_project(tmp_path: Path) -> Path:
    root = tmp_path / "LocalLLM-Lab"
    root.mkdir(exist_ok=True)
    completed = subprocess.run(["git", "init"], cwd=root, capture_output=True, text=True, check=False)
    assert completed.returncode == 0
    return root


class FixtureWriter:
    def __init__(self, *, content: str = "PASS", outside_file: bool = False, usage: TokenUsage | None = None):
        self.content = content
        self.outside_file = outside_file
        self.usage = usage or TokenUsage(input_tokens=12, cached_input_tokens=7, output_tokens=3, available=True)
        self.called = False
        self.working_directory = None
        self.target = None

    def run_isolated_file_change(self, working_directory, target, expected_content):
        self.called = True
        self.working_directory = working_directory
        self.target = target
        assert expected_content == "PASS"
        target.write_bytes(self.content.encode("utf-8"))
        if self.outside_file:
            (working_directory.parent.parent / "outside.txt").write_text("unexpected", encoding="utf-8")
        return ExecutionResult(
            status="completed",
            test_result="pending",
            summary="mocked project smoke",
            exit_code=0,
            token_usage=self.usage,
            diagnostics=ProcessDiagnostics(thread_started=True, turn_started=True, turn_completed=True),
        )


def project_engine(tmp_path: Path, runner: FixtureWriter, *, runtime: RuntimeConfig | None = None) -> tuple[ControlCenterEngine, Path]:
    root = git_project(tmp_path)
    engine = ControlCenterEngine(
        runtime_config=runtime or real_runtime(),
        real_runner=runner,
        project_registry=configured_project(root),
    )
    return engine, root


def test_configured_local_llm_lab_project_loads():
    registry = load_project_registry(Path("config/projects.yaml"))
    project = registry.get("local_llm_lab")
    assert project is not None
    assert project.name == "LocalLLM-Lab"
    assert str(project.path) == r"C:\LocalLLM-Lab"


def test_unknown_project_id_is_rejected_without_runner(tmp_path):
    runner = FixtureWriter()
    engine, _ = project_engine(tmp_path, runner)
    result = engine.run_project_smoke("unknown")
    assert result["error_code"] == "PROJECT_NOT_CONFIGURED"
    assert not runner.called


def test_missing_and_non_git_project_are_rejected_without_runner(tmp_path):
    runner = FixtureWriter()
    missing = tmp_path / "missing"
    missing_engine = ControlCenterEngine(runtime_config=real_runtime(), real_runner=runner, project_registry=configured_project(missing))
    assert missing_engine.run_project_smoke("local_llm_lab")["error_code"] == "PROJECT_PATH_NOT_FOUND"
    non_git = tmp_path / "not-a-repository"
    non_git.mkdir()
    non_git_engine = ControlCenterEngine(runtime_config=real_runtime(), real_runner=runner, project_registry=configured_project(non_git))
    assert non_git_engine.run_project_smoke("local_llm_lab")["error_code"] == "PROJECT_GIT_UNAVAILABLE"
    assert not runner.called


def test_real_mode_and_budget_guard_run_before_fixture_runner(tmp_path):
    runner = FixtureWriter()
    engine, _ = project_engine(tmp_path, runner, runtime=RuntimeConfig())
    assert engine.run_project_smoke("local_llm_lab")["error_code"] == "REAL_MODE_REQUIRED"
    assert not runner.called
    engine, _ = project_engine(tmp_path, runner)
    engine.budgets = TokenBudgetManager(BudgetConfig(codex=CodexBudget(max_retry=0)))
    assert engine.run_project_smoke("local_llm_lab")["error_code"] == "RETRY_LIMIT_EXCEEDED"
    assert not runner.called


def test_project_smoke_creates_unique_fail_fixture_and_minimal_context(tmp_path, monkeypatch):
    runner = FixtureWriter()
    engine, root = project_engine(tmp_path, runner)
    contexts = []
    original_build = __import__("backend.orchestrator.engine", fromlist=["ContextBroker"]).ContextBroker.build

    def capture_context(self, *args, **kwargs):
        context = original_build(self, *args, **kwargs)
        contexts.append(context)
        return context

    monkeypatch.setattr("backend.orchestrator.engine.ContextBroker.build", capture_context)
    first = engine.run_project_smoke("local_llm_lab")
    engine = ControlCenterEngine(runtime_config=real_runtime(), real_runner=FixtureWriter(), project_registry=configured_project(root))
    second = engine.run_project_smoke("local_llm_lab")
    assert first["final_result"] == "COMPLETE"
    assert first["fixture_path"] != second["fixture_path"]
    assert runner.called
    assert runner.working_directory == root / Path(first["fixture_path"]).parent
    assert contexts[0].allowed_files == [first["fixture_path"]]
    assert contexts[0].goal == "Change the isolated status fixture from FAIL to PASS."
    assert contexts[0].configuration["required_final_content"] == "PASS"
    assert not (root / "status.txt").exists()


def test_precheck_unexpected_pass_does_not_invoke_codex(tmp_path, monkeypatch):
    runner = FixtureWriter()
    engine, _ = project_engine(tmp_path, runner)
    monkeypatch.setattr(engine, "_status_fixture_result", lambda target: "PASS")
    result = engine.run_project_smoke("local_llm_lab")
    assert result["error_code"] == "PRECHECK_UNEXPECTED_PASS"
    assert result["precheck_result"] == "PASS"
    assert not runner.called


def test_success_records_tokens_scope_postcheck_and_preserves_dirty_baseline(tmp_path):
    runner = FixtureWriter()
    engine, root = project_engine(tmp_path, runner)
    (root / "user-change.txt").write_text("keep", encoding="utf-8")
    result = engine.run_project_smoke("local_llm_lab")
    assert result["state"] == "COMPLETE"
    assert result["precheck_result"] == "FAIL"
    assert result["postcheck_result"] == "PASS"
    assert result["scope_guard_result"] == "PASS"
    assert result["changed_files"] == [result["fixture_path"]]
    assert result["gross_input_tokens"] == 12
    assert result["cached_input_tokens"] == 7
    assert result["uncached_input_tokens"] == 5
    assert result["output_tokens"] == 3
    assert (root / "user-change.txt").read_text(encoding="utf-8") == "keep"
    assert engine.status()["project_smoke_results"][-1]["run_id"] == result["run_id"]


def test_postcheck_accepts_one_newline_and_rejects_other_content(tmp_path):
    newline_runner = FixtureWriter(content="PASS\r\n")
    engine, _ = project_engine(tmp_path, newline_runner)
    assert engine.run_project_smoke("local_llm_lab")["state"] == "COMPLETE"
    bad_runner = FixtureWriter(content="PASS\n\n")
    engine, _ = project_engine(tmp_path, bad_runner)
    result = engine.run_project_smoke("local_llm_lab")
    assert result["state"] == "FAILED"
    assert result["error_code"] == "POSTCHECK_FAILED"


def test_scope_guard_outside_runtime_change_requires_human_review(tmp_path):
    runner = FixtureWriter(outside_file=True)
    engine, _ = project_engine(tmp_path, runner)
    result = engine.run_project_smoke("local_llm_lab")
    assert result["state"] == "HUMAN_REVIEW"
    assert result["final_result"] == "HUMAN_REVIEW"
    assert result["scope_guard_result"] == "FAIL"
    assert "outside.txt" in result["changed_files"]


def test_post_run_budget_warning_does_not_rewrite_project_success(tmp_path):
    runner = FixtureWriter(usage=TokenUsage(input_tokens=100, cached_input_tokens=90, output_tokens=1, available=True))
    engine, _ = project_engine(tmp_path, runner)
    engine.budgets = TokenBudgetManager(BudgetConfig(codex=CodexBudget(task_input_tokens=10, task_output_tokens=10)))
    result = engine.run_project_smoke("local_llm_lab")
    assert result["state"] == "COMPLETE"
    assert result["budget_warning"] == "TASK_BUDGET_EXCEEDED"
    assert engine.status()["token_usage"]["input_tokens"] == 100


def test_work_order_scope_is_limited_to_exact_fixture_path():
    work_order = WorkOrder(
        task_id="CONTROL-CENTER-SMOKE-001",
        goal="Change the isolated status fixture from FAIL to PASS.",
        task_type=TaskType.CODE_FIX,
        allowed_files=[".ai-control-center-smoke/run-id/status.txt"],
        acceptance_tests=["status.txt content equals PASS"],
        max_retry=0,
        needs_codex=True,
    )
    assert work_order.allowed_files == [".ai-control-center-smoke/run-id/status.txt"]
