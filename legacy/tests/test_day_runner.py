from pathlib import Path

import pytest

from backend.agents.day_providers import (
    MockDayArchitect, MockSemanticEvaluator, OpenAIDayArchitect, OpenAISemanticEvaluator,
    ProviderConfigurationError, ProviderRequestError, ProviderTimeoutError,
    strict_provider_schema, validate_strict_provider_schema,
)
from backend.control.plans import load_plan_registry
from backend.control.orchestration import load_orchestration_config
from backend.control.model_router import ModelRouter, load_model_profile_registry
from backend.control.tasks import ConfiguredTask, TaskCommand, TaskRegistry
from backend.models.day import (
    ArchitectDecision, ArchitectProviderOutput, DayExecutionMode, DayPlan, DayPlanRegistry, DayRunSnapshot,
    DayRunState, EvaluatorProviderOutput, QueuedTask, SemanticEvaluation,
)
from backend.models.orchestration import ProviderBudget, ProviderSettings
from backend.models.model_routing import ProviderExecutionConfig
from backend.models.runtime import CodexAttemptResult
from backend.models.task import TaskType
from backend.orchestrator.day_runner import DayRunner


def configured_task(task_id: str, *, semantic: bool = False, requires_codex: bool = False) -> ConfiguredTask:
    return ConfiguredTask(task_id=task_id, project_id="test", title=task_id, task_type=TaskType.CODE_FIX, precheck=TaskCommand(argv=["python", "-c", "pass"]), postcheck=TaskCommand(argv=["python", "-c", "pass"]), allowed_files=["src/example.py"], context_files=["src/example.py"], requires_codex=requires_codex, evaluator_type="semantic" if semantic else "deterministic", independent_evaluator_required=semantic, evaluation_metrics=["groundedness"] if semantic else [])


def make_runner(tasks, execute, *, plan_kwargs=None, evaluators=None, provider_budgets=None, persisted=None, saved=None):
    plan_values = {"plan_id": "mock-day", "title": "Mock day", "task_ids": [task.task_id for task in tasks], "max_tasks_per_run": 10, "max_codex_calls": 5, "max_architect_calls": 10, "max_evaluator_calls": 10}
    plan_values.update(plan_kwargs or {})
    plan = DayPlan(**plan_values)
    return DayRunner(DayPlanRegistry(plans={plan.plan_id: plan}), TaskRegistry(tasks={task.task_id: task for task in tasks}), execute, evaluators=evaluators, provider_budgets=provider_budgets, saved=saved, persist=(lambda state: persisted.append(state)) if persisted is not None else None)


def complete_no_change(task_id, **kwargs):
    return {"run_id": f"run-{task_id}", "task_id": task_id, "final_result": "COMPLETE_NO_CHANGE", "codex_attempts": [], "codex_invoked": False, "precheck_result": "PASS"}


def test_multi_task_plan_loads_without_reinterpreting_historical_plan():
    plans = load_plan_registry(Path("config/plans.yaml"))
    assert plans.get("week1-day3-local-llm").task_ids == ["PC-001-A"]
    assert plans.get("week1-day3-local-llm-v2").task_ids == ["PC-001-A", "PC-001-C", "PC-002-A"]
    assert plans.get("week1-day3-local-llm-v2-real-architect").continuous_mode_supported is False
    config = load_orchestration_config(Path("config/orchestration.yaml"))
    assert config.orchestration.architect.provider == "mock"
    assert config.orchestration.evaluator.provider == "mock"


def test_provider_environment_overrides_are_explicit_and_do_not_require_credentials(monkeypatch):
    monkeypatch.setenv("AI_CONTROL_CENTER_ARCHITECT_PROVIDER", "openai")
    monkeypatch.setenv("AI_CONTROL_CENTER_ARCHITECT_MODEL", "configured-test-model")
    monkeypatch.setenv("AI_CONTROL_CENTER_EVALUATOR_PROVIDER", "openai")
    monkeypatch.setenv("AI_CONTROL_CENTER_EVALUATOR_MODEL", "configured-evaluator-model")
    config = load_orchestration_config(Path("config/orchestration.yaml"))
    assert config.orchestration.architect.provider == "openai"
    assert config.orchestration.architect.model == "configured-test-model"
    assert config.orchestration.evaluator.provider == "openai"
    assert config.orchestration.evaluator.model == "configured-evaluator-model"


def test_mock_provider_diagnostics_report_mock_without_a_model():
    execution = ProviderExecutionConfig(provider="mock")
    architect = MockDayArchitect().choose({"eligible_tasks": [{"task_id": "A"}]}, execution)
    evaluator = MockSemanticEvaluator().evaluate({"task_id": "A", "rubric": {"metrics": []}}, execution)
    assert architect.diagnostics["provider"] == evaluator.diagnostics["provider"] == "mock"
    assert architect.diagnostics["execution"]["model"] is None
    assert evaluator.diagnostics["execution"]["model"] is None


def test_provider_output_schemas_are_strict_and_exclude_internal_fields():
    architect_schema = strict_provider_schema(ArchitectProviderOutput)
    evaluator_schema = strict_provider_schema(EvaluatorProviderOutput)
    for schema in (architect_schema, evaluator_schema):
        validate_strict_provider_schema(schema)
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert "token_usage" not in schema["properties"]
        assert "diagnostics" not in schema["properties"]
    for field in ("task_id", "priority", "task_complexity"):
        assert field in architect_schema["required"]
        assert {variant["type"] for variant in architect_schema["properties"][field]["anyOf"]} >= {"null"}
    metric_schema = evaluator_schema["$defs"]["EvaluatorMetricProviderOutput"]
    assert metric_schema["additionalProperties"] is False
    assert set(metric_schema["required"]) == {"name", "score"}


def test_strict_schema_validator_rejects_free_form_or_optional_object_fields():
    with pytest.raises(ValueError, match="forbid additional properties"):
        validate_strict_provider_schema({"type": "object", "properties": {"unsafe": {"type": "object"}}, "required": ["unsafe"]})
    with pytest.raises(ValueError, match="requires every property"):
        validate_strict_provider_schema({"type": "object", "properties": {"missing": {"type": "string"}}, "required": [], "additionalProperties": False})
    with pytest.raises(ValueError, match="unsupported keywords"):
        validate_strict_provider_schema({"type": "object", "properties": {"name": {"type": "string", "pattern": ".*"}}, "required": ["name"], "additionalProperties": False})


def test_single_step_pauses_after_first_task_then_completes_final_task_with_progress():
    calls = []

    def execute(task_id, **kwargs):
        calls.append(task_id)
        return complete_no_change(task_id, **kwargs)

    day = make_runner([configured_task("A"), configured_task("B")], execute)
    first = day.start("mock-day")
    assert first["state"] == "PAUSED"
    assert first["day_progress"] == 50
    assert first["overall_progress"] == 50
    second = day.resume()
    assert second["state"] == "COMPLETE"
    assert second["day_progress"] == 100
    assert calls == ["A", "B"]


def test_active_paused_day_cannot_be_replaced_and_must_resume_its_trusted_queue():
    calls = []

    def execute(task_id, **kwargs):
        calls.append(task_id)
        return complete_no_change(task_id, **kwargs)

    day = make_runner([configured_task("A"), configured_task("B")], execute)
    assert day.start("mock-day")["state"] == "PAUSED"
    rejected = day.start("mock-day", mode=DayExecutionMode.CONTINUOUS)
    assert rejected["error_code"] == "ACTIVE_DAY_REQUIRES_RESUME_OR_REVIEW"
    assert [item["task_id"] for item in rejected["queue"]] == ["A", "B"]
    assert day.resume(mode=DayExecutionMode.CONTINUOUS)["state"] == "COMPLETE"
    assert calls == ["A", "B"]


def test_continuous_mode_processes_all_tasks_and_preserves_mode_for_resume():
    day = make_runner([configured_task("A"), configured_task("B"), configured_task("C")], complete_no_change)
    result = day.start("mock-day", mode=DayExecutionMode.CONTINUOUS)
    assert result["state"] == "COMPLETE"
    assert result["mode"] == "continuous"
    assert result["day_progress"] == 100


def test_retryable_architect_failure_is_retried_once_and_persisted_as_typed_escalation():
    class Architect:
        calls = 0

        def choose(self, request, execution):
            self.calls += 1
            if self.calls == 1:
                raise ProviderRequestError("CODEX_TIMEOUT", "bounded timeout")
            return ArchitectDecision(task_id="A", reason="retry selected trusted task")

    plan = DayPlan(plan_id="p", title="p", task_ids=["A"], architect_provider="codex", max_auto_provider_retries=1)
    day = DayRunner(DayPlanRegistry(plans={"p": plan}), TaskRegistry(tasks={"A": configured_task("A")}), complete_no_change, architects={"codex": Architect()})
    result = day.start("p", mode=DayExecutionMode.CONTINUOUS)
    assert result["state"] == "COMPLETE"
    assert result["auto_provider_retries"] == 1
    assert result["escalation_events"] == [{"category": "RETRYABLE", "reason": "CODEX_TIMEOUT", "action": "RETRY_ARCHITECT"}]


def test_external_architect_failure_remains_a_typed_human_boundary():
    class Architect:
        def choose(self, request, execution):
            raise ProviderRequestError("CODEX_NOT_FOUND", "requires runtime installation")

    plan = DayPlan(plan_id="p", title="p", task_ids=["A"], architect_provider="codex")
    day = DayRunner(DayPlanRegistry(plans={"p": plan}), TaskRegistry(tasks={"A": configured_task("A")}), complete_no_change, architects={"codex": Architect()})
    result = day.start("p", mode=DayExecutionMode.CONTINUOUS)
    assert result["state"] == "HUMAN_REVIEW"
    assert result["human_review_queue"][0]["escalation_category"] == "EXTERNAL_ACTION_REQUIRED"


def test_continuous_human_review_and_hard_limits_stop_without_extra_task_execution():
    review = make_runner([configured_task("A")], lambda task_id, **kwargs: {"task_id": task_id, "final_result": "HUMAN_REVIEW", "codex_attempts": []})
    assert review.start("mock-day", mode=DayExecutionMode.CONTINUOUS)["state"] == "HUMAN_REVIEW"
    max_tasks = make_runner([configured_task("A"), configured_task("B")], complete_no_change, plan_kwargs={"max_tasks_per_run": 1})
    assert max_tasks.start("mock-day", mode=DayExecutionMode.CONTINUOUS)["stop_reason"] == "MAX_TASKS_PER_RUN_REACHED"
    max_architect = make_runner([configured_task("A")], complete_no_change, plan_kwargs={"max_architect_calls": 0})
    assert max_architect.start("mock-day", mode=DayExecutionMode.CONTINUOUS)["stop_reason"] == "MAX_ARCHITECT_CALLS_REACHED"
    max_codex = make_runner([configured_task("A", requires_codex=True)], complete_no_change, plan_kwargs={"max_codex_calls": 0})
    assert max_codex.start("mock-day", mode=DayExecutionMode.CONTINUOUS)["stop_reason"] == "MAX_CODEX_CALLS_REACHED"


def test_semantic_evaluator_is_called_only_after_deterministic_success_and_repair_is_bounded():
    requests = []

    class Evaluator:
        def evaluate(self, request, execution):
            requests.append(request)
            assert execution.provider == "mock"
            return SemanticEvaluation(decision="REPAIR" if len(requests) == 1 else "PASS", reason="repair", repair_instruction="Fix the bounded semantic issue.", metrics={"groundedness": 5.0})

    executions = []

    def execute(task_id, **kwargs):
        executions.append(kwargs)
        return {"task_id": task_id, "final_result": "COMPLETE", "precheck_result": "FAIL", "postcheck_result": "PASS", "scope_guard_result": "PASS", "codex_attempts": []}

    result = make_runner([configured_task("S", semantic=True)], execute, evaluators={"mock": Evaluator()}).start("mock-day")
    assert result["state"] == "COMPLETE"
    assert len(requests) == 2
    assert executions[1]["repair_instruction"] == "Fix the bounded semantic issue."
    assert "architect_history" not in requests[0]


def test_evaluator_cannot_override_deterministic_postcheck_failure():
    called = []

    class Evaluator:
        def evaluate(self, request, execution):
            called.append(request)
            return SemanticEvaluation(decision="PASS")

    result = make_runner([configured_task("S", semantic=True)], lambda task_id, **kwargs: {"task_id": task_id, "final_result": "COMPLETE", "postcheck_result": "FAIL", "scope_guard_result": "PASS", "codex_attempts": []}, evaluators={"mock": Evaluator()}).start("mock-day")
    assert result["state"] == "HUMAN_REVIEW"
    assert called == []


def test_provider_budget_precheck_stops_and_post_run_overage_warns_without_rewriting_success():
    stopped = make_runner([configured_task("A")], complete_no_change, provider_budgets={"architect": ProviderBudget(max_calls=0), "evaluator": ProviderBudget()})
    assert stopped.start("mock-day")["stop_reason"] == "ARCHITECT_BUDGET_GUARD"

    class Architect:
        def choose(self, request, execution):
            from backend.models.day import ArchitectDecision
            from backend.models.result import TokenUsage
            return ArchitectDecision(task_id="A", reason="test", token_usage=TokenUsage(input_tokens=10, output_tokens=1, available=True))

    overage = DayRunner(DayPlanRegistry(plans={"mock-day": DayPlan(plan_id="mock-day", title="x", task_ids=["A"])}), TaskRegistry(tasks={"A": configured_task("A")}), complete_no_change, architects={"mock": Architect()}, provider_budgets={"architect": ProviderBudget(daily_input_tokens=1, daily_output_tokens=1), "evaluator": ProviderBudget()})
    result = overage.start("mock-day")
    assert result["state"] == "COMPLETE"
    assert "ARCHITECT_POST_RUN_BUDGET_OVERAGE" in result["budget_warnings"]
    assert result["token_totals"]["day_total"] == 11


class FakeResponses:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, responses):
        self.responses = responses


def test_openai_architect_and_evaluator_build_structured_sdk_requests_parse_usage_and_retry(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    architect_responses = FakeResponses([type("APITimeout", (Exception,), {})(), {"output_text": '{"decision":"RUN_TASK","task_id":"A","reason":"ok","priority":1,"task_complexity":"simple"}', "usage": {"input_tokens": 12, "input_tokens_details": {"cached_tokens": 3}, "output_tokens": 4}}])
    settings = ProviderSettings(provider="openai", model="ignored-test-model", max_transient_retries=1)
    execution = ProviderExecutionConfig(profile_id="standard", provider="openai", model="gpt-5.6-terra", reasoning_effort="medium", timeout_seconds=123, max_output_tokens=456)
    client_options = []
    architect = OpenAIDayArchitect(settings, client_factory=lambda **kwargs: client_options.append(kwargs) or FakeClient(architect_responses))
    decision = architect.choose({"plan_id": "p", "eligible_tasks": [{"task_id": "A"}]}, execution)
    assert decision.task_id == "A"
    assert decision.token_usage.cached_input_tokens == 3
    assert len(architect_responses.calls) == 2
    assert architect_responses.calls[0]["store"] is False
    assert "json_schema" == architect_responses.calls[0]["text"]["format"]["type"]
    assert architect_responses.calls[0]["model"] == "gpt-5.6-terra"
    assert architect_responses.calls[0]["reasoning"] == {"effort": "medium"}
    assert architect_responses.calls[0]["max_output_tokens"] == 456
    architect_schema = architect_responses.calls[0]["text"]["format"]["schema"]
    assert architect_schema["additionalProperties"] is False
    assert set(architect_schema["required"]) == set(architect_schema["properties"])
    assert "token_usage" not in architect_schema["properties"]
    assert "diagnostics" not in architect_schema["properties"]
    assert client_options == [{"api_key": "test-key", "timeout": 123, "max_retries": 0}]
    assert decision.diagnostics["model"] == "gpt-5.6-terra"
    assert decision.diagnostics["timeout_seconds"] == 123

    evaluator_responses = FakeResponses([{"output_text": '{"decision":"PASS","reason":"ok","metrics":[{"name":"groundedness","score":4.8}],"blocking_issues":[],"repair_instruction":null}', "usage": {"input_tokens": 8, "output_tokens": 2}}])
    evaluation = OpenAISemanticEvaluator(settings, client_factory=lambda **kwargs: FakeClient(evaluator_responses)).evaluate({"task_id": "S", "rubric": {"metrics": ["groundedness"]}}, execution)
    assert evaluation.decision == "PASS"
    assert evaluation.metrics["groundedness"] == 4.8
    assert evaluation.diagnostics["reasoning_effort"] == "medium"
    evaluator_schema = evaluator_responses.calls[0]["text"]["format"]["schema"]
    assert evaluator_schema["additionalProperties"] is False
    assert "token_usage" not in evaluator_schema["properties"]
    assert "diagnostics" not in evaluator_schema["properties"]


def test_openai_bad_request_is_sanitized_as_schema_diagnostic(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class BadRequestError(Exception):
        status_code = 400
        code = "invalid_json_schema"

        def __str__(self):
            return "Invalid schema: additionalProperties must be false"

    execution = ProviderExecutionConfig(provider="openai", model="gpt-5.6-terra", reasoning_effort="medium", timeout_seconds=30, max_output_tokens=100)
    provider = OpenAIDayArchitect(ProviderSettings(provider="openai", max_transient_retries=0), client_factory=lambda **_kwargs: FakeClient(FakeResponses([BadRequestError()])))
    with pytest.raises(ProviderRequestError) as captured:
        provider.choose({"eligible_tasks": []}, execution)
    assert captured.value.code == "OPENAI_SCHEMA_INVALID"
    assert captured.value.diagnostics == {
        "provider_error_type": "BadRequestError", "request_stage": "responses.create",
        "http_status": 400, "api_error_code": "invalid_json_schema",
        "safe_message": "Invalid schema: additionalProperties must be false",
    }


def test_openai_evaluator_rejects_an_unsafe_repair_instruction_as_typed_output_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    execution = ProviderExecutionConfig(provider="openai", model="gpt-5.6-terra", reasoning_effort="medium", timeout_seconds=30, max_output_tokens=100)
    response = {
        "output_text": '{"decision":"REPAIR","reason":"needs change","metrics":[],"blocking_issues":[],"repair_instruction":"Run /unsafe-command"}',
        "usage": {"input_tokens": 8, "output_tokens": 2},
    }
    evaluator = OpenAISemanticEvaluator(
        ProviderSettings(provider="openai", max_transient_retries=0),
        client_factory=lambda **_kwargs: FakeClient(FakeResponses([response])),
    )
    with pytest.raises(ProviderRequestError) as captured:
        evaluator.evaluate({"task_id": "S", "rubric": {"metrics": []}}, execution)
    assert captured.value.code == "OPENAI_INVALID_STRUCTURED_OUTPUT"
    assert captured.value.diagnostics == {
        "provider_error_type": "RepairInstructionPolicy",
        "request_stage": "structured_output",
    }


def test_openai_provider_fails_closed_without_credentials_and_exposes_typed_timeout(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    execution = ProviderExecutionConfig(provider="openai", model="gpt-5.6-terra", reasoning_effort="medium", timeout_seconds=30, max_output_tokens=100)
    with pytest.raises(ProviderConfigurationError):
        OpenAIDayArchitect(ProviderSettings(provider="openai", model="test")).choose({"eligible_tasks": []}, execution)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    responses = FakeResponses([type("APITimeout", (Exception,), {})()])
    with pytest.raises(ProviderTimeoutError):
        OpenAIDayArchitect(ProviderSettings(provider="openai", model="test", max_transient_retries=0), client_factory=lambda **kwargs: FakeClient(responses)).choose({"eligible_tasks": []}, execution)


def test_real_architect_plan_without_credentials_enters_human_review_without_mock_or_codex(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    task = configured_task("A")
    plan = DayPlan(plan_id="real", title="real", task_ids=["A"], architect_provider="openai", evaluator_provider="mock", continuous_mode_supported=False)
    day = DayRunner(
        DayPlanRegistry(plans={"real": plan}), TaskRegistry(tasks={"A": task}),
        lambda *_args, **_kwargs: pytest.fail("Codex must not be invoked when the Architect is unavailable"),
        architects={"openai": OpenAIDayArchitect()},
        model_router=ModelRouter(load_model_profile_registry(Path("config/model_profiles.yaml"))),
        provider_budgets={role: ProviderBudget(daily_input_tokens=100000, daily_output_tokens=100000) for role in ("architect", "evaluator", "codex")},
    )
    result = day.start("real")
    assert result["state"] == "HUMAN_REVIEW"
    assert result["codex_calls"] == 0
    review = result["human_review_queue"][0]
    assert review["failure_type"] == "provider_error"
    assert review["routing_profile_id"] == "standard"


def test_sanitized_provider_diagnostics_are_persisted_in_human_review():
    task = configured_task("A")
    plan = DayPlan(plan_id="real", title="real", task_ids=["A"], architect_provider="openai", evaluator_provider="mock", continuous_mode_supported=False)

    class ErrorArchitect:
        def choose(self, request, execution):
            raise ProviderRequestError("OPENAI_SCHEMA_INVALID", "OpenAI rejected the structured output schema", diagnostics={"http_status": 400, "request_stage": "responses.create"})

    day = DayRunner(
        DayPlanRegistry(plans={"real": plan}), TaskRegistry(tasks={"A": task}),
        lambda *_args, **_kwargs: pytest.fail("Codex must not run"), architects={"openai": ErrorArchitect()},
        model_router=ModelRouter(load_model_profile_registry(Path("config/model_profiles.yaml"))),
        provider_budgets={role: ProviderBudget(daily_input_tokens=100000, daily_output_tokens=100000) for role in ("architect", "evaluator", "codex")},
    )
    review = day.start("real")["human_review_queue"][0]
    assert review["reason"] == "ARCHITECT_PROVIDER_ERROR:OPENAI_SCHEMA_INVALID"
    assert review["summary"] == "OpenAI rejected the structured output schema"
    assert review["provider_diagnostics"] == {"http_status": 400, "request_stage": "responses.create"}


def test_interrupted_day_preserves_structured_state_and_requires_explicit_resume():
    persisted = []
    saved = DayRunSnapshot(
        plan_id="mock-day", state=DayRunState.RUNNING, mode=DayExecutionMode.CONTINUOUS,
        queue=[QueuedTask(task_id="A", attempts=1)], architect_calls=1, codex_calls=0,
        deterministic_zero_usage_task_ids=["A"], day_progress=50, overall_progress=50,
    ).model_dump(mode="json")
    day = make_runner([configured_task("A")], complete_no_change, saved=saved, persisted=persisted)
    restored = day.view()
    assert restored["state"] == "PAUSED"
    assert restored["stop_reason"] == "INTERRUPTED_REQUIRES_RESUME"
    assert restored["queue"][0]["attempts"] == 1
    assert restored["mode"] == "continuous"
    assert persisted[-1]["state"] == "PAUSED"


def test_interrupted_state_pauses_and_persists_meaningful_transitions():
    persisted = []
    saved = DayRunSnapshot(plan_id="mock-day", state=DayRunState.RUNNING, queue=[QueuedTask(task_id="A")]).model_dump(mode="json")
    day = make_runner([configured_task("A")], complete_no_change, saved=saved, persisted=persisted)
    assert day.view()["state"] == "PAUSED"
    assert day.resume()["state"] == "COMPLETE"
    assert persisted[-1]["state"] == "COMPLETE"


def test_launchers_use_expected_port_and_reload_boundaries():
    development = Path("scripts/start_dev.ps1").read_text(encoding="utf-8")
    production = Path("scripts/start.ps1").read_text(encoding="utf-8")
    assert "--port $Port --reload" in development and "127.0.0.1" in development
    assert "--port $Port" in production and "--reload" not in production and "127.0.0.1" in production
