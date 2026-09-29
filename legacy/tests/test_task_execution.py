import subprocess
from pathlib import Path

import pytest

from backend.control.projects import ConfiguredProject, ProjectRegistry
from backend.control.local_ollama_repair import RepairEdit, RepairProposal
from backend.control.tasks import ConfiguredTask, TaskCommand, TaskRegistry, load_task_registry
from backend.control.token_budget import BudgetConfig, CodexBudget, TokenBudgetManager
from backend.models.result import ExecutionResult, ProcessDiagnostics, TokenUsage
from backend.models.runtime import CodexMode, CodexRuntimeConfig, CommandRunResult, RuntimeConfig
from backend.models.task import TaskType
from backend.orchestrator.engine import ControlCenterEngine


def real_runtime() -> RuntimeConfig:
    return RuntimeConfig(codex=CodexRuntimeConfig(mode=CodexMode.REAL, executable="codex"))


def configured_task() -> ConfiguredTask:
    return ConfiguredTask(
        task_id="PC-001-A",
        project_id="local_llm_lab",
        title="TH-PAD QA / Shipment quantity consistency",
        task_type=TaskType.CODE_FIX,
        working_directory=".",
        precheck=TaskCommand(argv=["python", "scripts/check.py", "--output", "{artifact_root}"]),
        postcheck=TaskCommand(argv=["python", "scripts/check.py", "--output", "{artifact_root}"]),
        allowed_files=["scripts/check.py"],
        context_files=["scripts/check.py"],
        max_retry=1,
        requires_codex=True,
        evaluator_type="deterministic",
        context_max_characters=1000,
    )


def git_source(tmp_path: Path) -> Path:
    root = tmp_path / "LocalLLM-Lab"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "scripts" / "check.py").write_text("VALUE = 'before'\n", encoding="utf-8")
    (root / "tests" / "test_check.py").write_text("def test_fixture():\n    assert True\n", encoding="utf-8")
    for argv in (
        ["git", "init"],
        ["git", "add", "scripts/check.py", "tests/test_check.py"],
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial"],
    ):
        completed = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False)
        assert completed.returncode == 0, completed.stderr
    return root


def engine_for(tmp_path: Path, runner, *, runtime=None) -> tuple[ControlCenterEngine, Path]:
    source = git_source(tmp_path)
    projects = ProjectRegistry(projects={"local_llm_lab": ConfiguredProject(name="LocalLLM-Lab", path=source, default_branch="main")})
    tasks = TaskRegistry(tasks={"PC-001-A": configured_task()})
    return ControlCenterEngine(
        runtime_config=runtime or real_runtime(), real_runner=runner, project_registry=projects,
        task_registry=tasks, worktree_root=tmp_path / "managed-worktrees",
    ), source


class TaskWriter:
    def __init__(self, *, outside=False, usage=None):
        self.called = 0
        self.outside = outside
        self.usage = usage or TokenUsage(input_tokens=15, cached_input_tokens=6, output_tokens=4, available=True)
        self.commands = []

    def run_worktree_task(self, working_directory, prompt):
        self.called += 1
        self.commands.append(prompt)
        (working_directory / "scripts" / "check.py").write_text(f"VALUE = 'attempt-{self.called}'\n", encoding="utf-8")
        if self.outside:
            (working_directory / "outside.py").write_text("outside", encoding="utf-8")
        return ExecutionResult(
            status="completed", test_result="pending", summary="mock task", exit_code=0, token_usage=self.usage,
            diagnostics=ProcessDiagnostics(thread_started=True, turn_started=True, turn_completed=True),
        )


def command_results(engine, values):
    sequence = iter(values)

    def fake_command(command, cwd, artifact_root):
        value = next(sequence)
        return CommandRunResult(argv=command.argv, cwd=str(cwd), **value)

    engine._run_task_command = fake_command


def test_configured_real_task_loads_and_metadata_is_safe():
    registry = load_task_registry(Path("config/tasks.yaml"))
    assert registry.get("PC-001-A").project_id == "local_llm_lab"
    metadata = registry.metadata()
    assert [item["task_id"] for item in metadata] == ["PC-001-A", "PC-001-C", "PC-002-A"]
    assert all(set(item) == {"task_id", "project_id", "title", "task_type", "evaluator_type", "independent_evaluator_required"} for item in metadata)
    assert all(item["project_id"] == "local_llm_lab" and item["evaluator_type"] == "deterministic" for item in metadata)


def test_unknown_task_and_real_mode_are_rejected_without_worktree_or_codex(tmp_path):
    runner = TaskWriter()
    engine, _ = engine_for(tmp_path, runner)
    assert engine.run_task("unknown")["error_code"] == "TASK_NOT_CONFIGURED"
    engine, _ = engine_for(tmp_path / "mock", runner, runtime=RuntimeConfig())
    assert engine.run_task("PC-001-A")["error_code"] == "REAL_MODE_REQUIRED"
    assert runner.called == 0


def test_task_source_validation_rejects_missing_repository(tmp_path):
    runner = TaskWriter()
    missing = tmp_path / "missing"
    projects = ProjectRegistry(projects={"local_llm_lab": ConfiguredProject(name="LocalLLM-Lab", path=missing, default_branch="main")})
    tasks = TaskRegistry(tasks={"PC-001-A": configured_task()})
    engine = ControlCenterEngine(runtime_config=real_runtime(), real_runner=runner, project_registry=projects, task_registry=tasks, worktree_root=tmp_path / "managed")
    assert engine.run_task("PC-001-A")["error_code"] == "PROJECT_PATH_NOT_FOUND"
    assert runner.called == 0


def test_precheck_pass_completes_without_codex_and_source_remains_unchanged(tmp_path):
    runner = TaskWriter()
    engine, source = engine_for(tmp_path, runner)
    command_results(engine, [{"exit_code": 0, "passed": True, "stdout": "ok"}])
    selections = []
    result = engine.run_task("PC-001-A", codex_routing_selector=lambda context_size, retry: selections.append((context_size, retry)) or object())
    assert result["final_result"] == "COMPLETE_NO_CHANGE"
    assert result["codex_invoked"] is False
    assert runner.called == 0
    assert selections == []
    assert (source / "scripts" / "check.py").read_text(encoding="utf-8") == "VALUE = 'before'\n"
    assert Path(result["worktree_path"]).is_dir()
    assert result["task_branch"].startswith("agent/pc-001-a-")


def test_dynamic_day_engineering_work_uses_guarded_worktree_without_manufacturing_criterion_evidence(tmp_path):
    runner = TaskWriter()
    engine, _ = engine_for(tmp_path, runner)
    command_results(engine, [
        {"exit_code": 1, "passed": False, "stderr": "fixture failure"},
        {"exit_code": 0, "passed": True, "stdout": "fixed"},
    ])
    result = engine._execute_local_llm_day_work_order({
        "kind": "DYNAMIC_ENGINEERING_WORK", "criterion_ids": ["d6-fail_closed_temporal"],
        "dynamic_work_order": {
            "task_id": "day-6-temporal-fixture", "project_id": "local_llm_lab", "task_type": "code_fix",
            "allowed_files": ["scripts/check.py"], "context_files": ["scripts/check.py"],
            "acceptance_test_files": ["tests/test_check.py"],
        },
    })
    assert result["final_result"] == "COMPLETE"
    assert result["evidence"] == {}
    assert runner.called == 1


@pytest.mark.parametrize("task_id", ["PC-001-A", "PC-001-C", "PC-002-A"])
def test_completed_static_process_task_has_no_day_evidence_capability(tmp_path, task_id):
    runner = TaskWriter()
    engine, _ = engine_for(tmp_path, runner)
    engine.tasks.tasks[task_id] = configured_task().model_copy(update={"task_id": task_id})
    command_results(engine, [{"exit_code": 0, "passed": True, "stdout": "ok"}])
    result = engine._execute_local_llm_day_work_order({"kind": "ENGINE_WORK_ORDER", "engine_task_id": task_id, "criterion_ids": ["d1-repository_relationship"]})
    assert result["final_result"] == "COMPLETE_NO_CHANGE"
    assert result["evidence"] == {}


def test_dynamic_day_engineering_work_rejects_protected_path_before_execution(tmp_path):
    runner = TaskWriter()
    engine, _ = engine_for(tmp_path, runner)
    result = engine._execute_local_llm_day_work_order({
        "kind": "DYNAMIC_ENGINEERING_WORK", "criterion_ids": ["d6-fail_closed_temporal"],
        "dynamic_work_order": {
            "task_id": "day-6-protected-fixture", "project_id": "local_llm_lab", "task_type": "code_fix",
            "allowed_files": ["results/unsafe.py"], "context_files": ["scripts/check.py"],
            "acceptance_test_files": ["tests/test_check.py"],
        },
    })
    assert result["error_code"] == "DAY_DYNAMIC_WORK_ORDER_REJECTED"
    assert runner.called == 0


def test_local_llm_countermeasure_is_reviewed_by_codex_in_a_guarded_worktree(tmp_path):
    runner = TaskWriter()
    engine, _ = engine_for(tmp_path, runner)
    command_results(engine, [
        {"exit_code": 1, "passed": False, "stderr": "fixture failure"},
        {"exit_code": 0, "passed": True, "stdout": "fixed"},
    ])
    result = engine._execute_local_llm_day_work_order({
        "kind": "LOCAL_LLM_COUNTERMEASURE",
        "proposal": RepairProposal("replace the fixture value", (RepairEdit("scripts/check.py", "before", "after"),)),
        "dynamic_work_order": {
            "task_id": "day-6-repair-fixture", "project_id": "local_llm_lab", "task_type": "code_fix",
            "allowed_files": ["scripts/check.py"], "context_files": ["scripts/check.py"],
            "acceptance_test_files": ["tests/test_check.py"],
        },
    })
    assert result["final_result"] == "COMPLETE"
    assert result["scope_guard_result"] == "PASS"
    assert "LOCAL LLM COUNTERMEASURE" in runner.commands[0]


def test_dirty_unrelated_source_is_preserved_but_dirty_task_dependency_requires_review(tmp_path):
    runner = TaskWriter()
    engine, source = engine_for(tmp_path, runner)
    (source / "notes.txt").write_text("keep", encoding="utf-8")
    command_results(engine, [{"exit_code": 0, "passed": True}])
    assert engine.run_task("PC-001-A")["final_result"] == "COMPLETE_NO_CHANGE"
    assert (source / "notes.txt").read_text(encoding="utf-8") == "keep"
    (source / "scripts" / "check.py").write_text("VALUE = 'dirty'\n", encoding="utf-8")
    result = engine.run_task("PC-001-A")
    assert result["state"] == "HUMAN_REVIEW"
    assert result["error_code"] == "SOURCE_TASK_DEPENDENCY_DIRTY"


def test_precheck_infrastructure_failure_never_invokes_codex(tmp_path):
    runner = TaskWriter()
    engine, _ = engine_for(tmp_path, runner)
    command_results(engine, [{"passed": False, "error_code": "COMMAND_NOT_FOUND"}])
    result = engine.run_task("PC-001-A")
    assert result["state"] == "HUMAN_REVIEW"
    assert result["triage_result"] == "INFRASTRUCTURE_FAILURE"
    assert runner.called == 0


def test_code_fix_runs_in_isolated_worktree_with_bounded_context_and_records_tokens(tmp_path):
    runner = TaskWriter()
    engine, source = engine_for(tmp_path, runner)
    command_results(engine, [
        {"exit_code": 1, "passed": False, "stderr": "assertion failed"},
        {"exit_code": 0, "passed": True, "stdout": "fixed"},
    ])
    selections = []
    result = engine.run_task("PC-001-A", codex_routing_selector=lambda context_size, retry: selections.append((context_size, retry)) or object())
    assert result["state"] == "COMPLETE"
    assert result["precheck_result"] == "FAIL"
    assert result["triage_result"] == "CODE_FIX"
    assert result["scope_guard_result"] == "PASS"
    assert result["postcheck_result"] == "PASS"
    assert result["changed_files"] == ["scripts/check.py"]
    assert result["gross_input_tokens"] == 15
    assert result["cached_input_tokens"] == 6
    assert result["uncached_input_tokens"] == 9
    assert result["output_tokens"] == 4
    assert "assertion failed" in runner.commands[0]
    assert "scripts/check.py" in runner.commands[0]
    assert len(selections) == 1
    assert selections[0][0] > 0
    assert selections[0][1] == 0
    assert (source / "scripts" / "check.py").read_text(encoding="utf-8") == "VALUE = 'before'\n"


def test_scope_guard_failure_escalates_without_discarding_worktree_change(tmp_path):
    runner = TaskWriter(outside=True)
    engine, _ = engine_for(tmp_path, runner)
    command_results(engine, [{"exit_code": 1, "passed": False}, {"exit_code": 0, "passed": True}])
    result = engine.run_task("PC-001-A")
    assert result["state"] == "HUMAN_REVIEW"
    assert result["scope_guard_result"] == "FAIL"
    assert result["out_of_scope_files"] == ["outside.py"]
    assert Path(result["worktree_path"], "outside.py").is_file()


def test_postcheck_retry_is_bounded_and_second_failure_requires_human_review(tmp_path):
    runner = TaskWriter()
    engine, _ = engine_for(tmp_path, runner)
    command_results(engine, [
        {"exit_code": 1, "passed": False, "stderr": "first failure"},
        {"exit_code": 1, "passed": False, "stderr": "repair still failing"},
        {"exit_code": 1, "passed": False, "stderr": "repair still failing"},
    ])
    result = engine.run_task("PC-001-A")
    assert result["state"] == "HUMAN_REVIEW"
    assert result["error_code"] == "RETRY_LIMIT_EXCEEDED"
    assert len(result["codex_attempts"]) == 2
    assert runner.called == 2


def test_post_run_budget_warning_does_not_rewrite_task_success(tmp_path):
    runner = TaskWriter(usage=TokenUsage(input_tokens=100, cached_input_tokens=80, output_tokens=1, available=True))
    engine, _ = engine_for(tmp_path, runner)
    engine.budgets = TokenBudgetManager(BudgetConfig(codex=CodexBudget(task_input_tokens=10, task_output_tokens=10)))
    command_results(engine, [{"exit_code": 1, "passed": False}, {"exit_code": 0, "passed": True}])
    result = engine.run_task("PC-001-A")
    assert result["state"] == "COMPLETE"
    assert result["budget_warning"] == "TASK_BUDGET_EXCEEDED"


def test_worktree_path_cannot_escape_managed_root(tmp_path):
    runner = TaskWriter()
    engine, _ = engine_for(tmp_path, runner)
    with __import__("pytest").raises(ValueError):
        engine._task_working_directory(tmp_path / "managed", "../escape")


def test_task_pipeline_contains_no_destructive_git_operations():
    source = Path("backend/orchestrator/engine.py").read_text(encoding="utf-8")
    assert "reset --hard" not in source
    assert "clean -fd" not in source
    assert "branch -D" not in source
