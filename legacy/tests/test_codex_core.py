import json

import pytest

from backend.agents.day_providers import CodexArchitectProvider, CodexReviewerProvider, ProviderRequestError
from backend.models.day import DayExecutionMode
from backend.models.model_routing import ProviderExecutionConfig
from backend.models.result import ProcessDiagnostics, TokenUsage
from backend.models.runtime import CodexMode, CodexRuntimeConfig, RuntimeConfig
from backend.orchestrator.engine import ControlCenterEngine
from backend.runners.codex import StructuredCodexResult


class FakeRoleRunner:
    def __init__(self, output: dict):
        self.output = output
        self.calls = []

    def run_readonly_structured(self, workspace, prompt, schema_path):
        self.calls.append((workspace, prompt, schema_path))
        return StructuredCodexResult(
            status="completed", output_text=json.dumps(self.output),
            token_usage=TokenUsage(input_tokens=12, cached_input_tokens=4, output_tokens=3, available=True),
            diagnostics=ProcessDiagnostics(thread_started=True, turn_started=True, turn_completed=True),
            duration_ms=8.5,
        )


def architect_request():
    return {
        "plan_id": "p", "validation_day": 3,
        "eligible_tasks": [{"task_id": "A", "title": "A", "task_type": "code_fix"}],
        "queue": [{"task_id": "A", "state": "PENDING", "final_result": None}],
        "completed": [], "review": [], "remaining_budgets": {"architect_calls": 1},
        "stop_conditions": {"max_tasks_per_run": 1}, "routing": {"profile_id": "standard"},
    }


def execution():
    return ProviderExecutionConfig(profile_id="standard", provider="codex", model="terra", reasoning_effort="medium", timeout_seconds=30, max_output_tokens=200)


def test_codex_architect_uses_strict_schema_bounded_context_and_records_role_usage(tmp_path):
    runner = FakeRoleRunner({"decision": "RUN_TASK", "task_id": "A", "reason": "trusted queue", "priority": None, "task_complexity": "normal"})
    decision = CodexArchitectProvider(runner, tmp_path).choose(architect_request(), execution())
    workspace, prompt, schema_path = runner.calls[0]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert workspace == tmp_path / "architect"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert decision.task_id == "A"
    assert decision.token_usage.uncached_input_tokens == 8
    assert decision.diagnostics["provider"] == "codex"
    assert decision.diagnostics["role"] == "architect"
    assert "repository source" not in prompt
    assert "allowed_files" not in prompt


def test_codex_architect_rejects_a_task_outside_the_eligible_queue(tmp_path):
    runner = FakeRoleRunner({"decision": "RUN_TASK", "task_id": "other", "reason": "bad", "priority": None, "task_complexity": None})
    with pytest.raises(ProviderRequestError, match="CODEX_ARCHITECT_TASK_NOT_ELIGIBLE"):
        CodexArchitectProvider(runner, tmp_path).choose(architect_request(), execution())


def test_codex_reviewer_is_read_only_and_not_an_independent_evaluator(tmp_path):
    runner = FakeRoleRunner({"decision": "NOT_REQUIRED", "reason": "deterministic checks are conclusive", "repair_instruction": None})
    review = CodexReviewerProvider(runner, tmp_path).review({"task_id": "A", "postcheck": "FAIL"}, execution())
    _, prompt, schema_path = runner.calls[0]
    assert review.decision == "NOT_REQUIRED"
    assert review.diagnostics["role"] == "reviewer"
    assert "read-only" in prompt
    assert json.loads(schema_path.read_text(encoding="utf-8"))["additionalProperties"] is False


def test_default_codex_core_plan_does_not_need_openai_key_and_pauses_after_precheck(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    engine = ControlCenterEngine(runtime_config=RuntimeConfig(codex=CodexRuntimeConfig(mode=CodexMode.MOCK)))
    engine.day_runner.execute_task = lambda task_id, **_kwargs: {
        "run_id": "day", "task_id": task_id, "final_result": "COMPLETE_NO_CHANGE",
        "precheck_result": "PASS", "codex_invoked": False, "codex_attempts": [],
    }
    result = engine.start_day("week1-day3-local-llm-v3-codex-core")
    assert result["state"] == "PAUSED"
    assert result["architect_calls"] == 1
    assert result["codex_calls"] == 0
    assert result["evaluator_calls"] == 0
    assert result["day_progress"] == pytest.approx(33.33)
    assert result["current_routing"]["provider"] == "codex"


def test_codex_core_continuous_resume_finishes_the_remaining_trusted_queue_without_live_codex(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    engine = ControlCenterEngine(runtime_config=RuntimeConfig(codex=CodexRuntimeConfig(mode=CodexMode.MOCK)))
    executed = []

    def deterministic_task(task_id, **_kwargs):
        executed.append(task_id)
        return {
            "run_id": f"day-{task_id}", "task_id": task_id, "final_result": "COMPLETE_NO_CHANGE",
            "precheck_result": "PASS", "codex_invoked": False, "codex_attempts": [],
        }

    engine.day_runner.execute_task = deterministic_task
    first = engine.start_day("week1-day3-local-llm-v3-codex-core")
    result = engine.resume_day(mode=DayExecutionMode.CONTINUOUS)
    assert first["state"] == "PAUSED"
    assert result["state"] == "COMPLETE"
    assert result["mode"] == "continuous"
    assert result["day_progress"] == result["overall_progress"] == 100
    assert result["architect_calls"] == 3
    assert result["codex_calls"] == result["evaluator_calls"] == 0
    assert executed == ["PC-001-A", "PC-001-C", "PC-002-A"]


def test_role_workspace_defaults_outside_the_control_center_repository(monkeypatch, tmp_path):
    monkeypatch.delenv("AI_CONTROL_CENTER_CODEX_ROLE_WORKSPACE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    root = ControlCenterEngine._codex_role_workspace_root()
    assert root == (tmp_path / "local-app-data" / "AI-Control-Center" / "codex-roles").resolve()
    assert "AI-Control-Center\\state" not in str(root)


def test_explicit_role_workspace_override_is_available_for_managed_local_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_CONTROL_CENTER_CODEX_ROLE_WORKSPACE", str(tmp_path / "roles"))
    assert ControlCenterEngine._codex_role_workspace_root() == (tmp_path / "roles").resolve()


def test_successful_deterministic_task_never_calls_independent_evaluator():
    from backend.control.tasks import ConfiguredTask, TaskCommand, TaskRegistry
    from backend.models.day import DayPlan, DayPlanRegistry
    from backend.models.task import TaskType
    from backend.orchestrator.day_runner import DayRunner

    task = ConfiguredTask(
        task_id="A", project_id="p", title="A", task_type=TaskType.CODE_FIX,
        precheck=TaskCommand(argv=["python", "-c", "pass"]), postcheck=TaskCommand(argv=["python", "-c", "pass"]),
        allowed_files=["a.py"], context_files=["a.py"], evaluator_type="semantic", independent_evaluator_required=False,
    )
    class Evaluator:
        def evaluate(self, *_args):
            raise AssertionError("independent evaluator must not run")

    result = DayRunner(
        DayPlanRegistry(plans={"p": DayPlan(plan_id="p", title="p", task_ids=["A"])}), TaskRegistry(tasks={"A": task}),
        lambda *_args, **_kwargs: {"task_id": "A", "final_result": "COMPLETE", "postcheck_result": "PASS", "scope_guard_result": "PASS", "codex_attempts": []},
        evaluators={"mock": Evaluator()},
    ).start("p")
    assert result["state"] == "COMPLETE"
    assert result["evaluator_calls"] == 0
