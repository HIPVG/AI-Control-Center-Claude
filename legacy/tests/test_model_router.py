from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.control.model_router import ModelRouter, load_model_profile_registry
from backend.control.tasks import ConfiguredTask, TaskCommand, TaskRegistry
from backend.models.day import ArchitectDecision, DayPlan, DayPlanRegistry
from backend.models.model_routing import FailureType, ModelProfileRegistry, RoutingPolicy, RoutingRequest, RoutingRole, TaskComplexity
from backend.models.orchestration import ProviderBudget
from backend.models.result import TokenUsage
from backend.models.task import TaskType
from backend.orchestrator.day_runner import DayRunner


PROFILES = Path("config/model_profiles.yaml")


def router() -> ModelRouter:
    return ModelRouter(load_model_profile_registry(PROFILES))


def request(*, role=RoutingRole.CODEX, execution_provider="codex", complexity=TaskComplexity.NORMAL, attempts=0, failure=None, policy=None, input_tokens=1000000, output_tokens=1000000):
    return RoutingRequest(
        role=role, execution_provider=execution_provider, task_type="code_fix", task_complexity=complexity,
        previous_attempt_count=attempts, previous_failure_type=failure,
        context_size=500, remaining_role_input_tokens=input_tokens,
        remaining_role_output_tokens=output_tokens, remaining_day_input_tokens=input_tokens,
        remaining_day_output_tokens=output_tokens, plan_policy=policy or RoutingPolicy(),
    )


def test_profile_config_loads_and_rejects_missing_required_profiles():
    profiles = load_model_profile_registry(PROFILES)
    assert list(profiles.model_profiles) == ["economical", "standard", "deep"]
    with pytest.raises(ValidationError):
        ModelProfileRegistry.model_validate({"model_profiles": {"economical": profiles.model_profiles["economical"]}})


def test_complexity_and_role_defaults_choose_lowest_sufficient_profile():
    selected = router()
    assert selected.select(request(complexity=TaskComplexity.SIMPLE)).profile_id == "economical"
    assert selected.select(request(complexity=TaskComplexity.NORMAL)).profile_id == "standard"
    assert selected.select(request(complexity=TaskComplexity.COMPLEX)).profile_id == "deep"
    assert selected.select(request(role=RoutingRole.ARCHITECT, complexity=TaskComplexity.SIMPLE)).profile_id == "economical"
    assert selected.select(request(role=RoutingRole.EVALUATOR, complexity=TaskComplexity.SIMPLE)).profile_id == "economical"
    assert selected.select(request(role=RoutingRole.EVALUATOR, complexity=TaskComplexity.NORMAL)).profile_id == "standard"


def test_provider_mapping_resolves_concrete_openai_execution_and_mock_never_claims_a_model():
    selected = router()
    real = selected.select(request(role=RoutingRole.ARCHITECT, execution_provider="openai"))
    assert (real.profile_id, real.provider, real.model, real.reasoning_effort) == ("standard", "openai", "gpt-5.6-terra", "medium")
    assert (real.timeout_seconds, real.max_output_tokens) == (300, 8000)
    mock = selected.select(request(role=RoutingRole.ARCHITECT, execution_provider="mock"))
    assert (mock.provider, mock.model, mock.reasoning_effort) == ("mock", None, None)


def test_unknown_execution_provider_fails_closed_instead_of_falling_back_to_codex():
    decision = router().select(request(role=RoutingRole.ARCHITECT, execution_provider="unknown-provider"))
    assert decision.outcome == "HUMAN_REVIEW"


def test_day_runner_passes_the_router_resolved_openai_config_to_the_architect():
    task = configured_task("A")
    plan = DayPlan(plan_id="real", title="real", task_ids=["A"], architect_provider="openai")
    observed = []

    class Architect:
        def choose(self, request, execution):
            observed.append((request, execution.model_dump(mode="json")))
            return ArchitectDecision(task_id="A", reason="trusted queue")

    result = DayRunner(
        DayPlanRegistry(plans={"real": plan}), TaskRegistry(tasks={"A": task}),
        lambda task_id, **kwargs: {"task_id": task_id, "final_result": "COMPLETE_NO_CHANGE", "codex_invoked": False, "codex_attempts": []},
        architects={"openai": Architect()}, model_router=router(),
        provider_budgets={role: ProviderBudget(daily_input_tokens=100000, daily_output_tokens=100000) for role in ("architect", "evaluator", "codex")},
    ).start("real")
    context, execution = observed[0]
    assert "routing" not in context
    assert set(context) == {"plan", "validation_day", "eligible_tasks", "queue", "prior_results", "remaining_calls", "stop_limits", "progress"}
    assert execution["provider"] == "openai"
    assert execution["model"] == "gpt-5.6-terra"
    assert execution["reasoning_effort"] == "medium"
    assert result["model_routing_decisions"][0]["model"] == "gpt-5.6-terra"


def test_retry_is_not_escalation_but_reasoning_failure_is_bounded_escalation():
    selected = router()
    assert selected.select(request(attempts=1)).profile_id == "standard"
    decision = selected.select(request(failure=FailureType.REASONING))
    assert (decision.outcome, decision.profile_id, decision.escalation_level) == ("SELECTED", "deep", 2)
    review = selected.select(request(failure=FailureType.REASONING, policy=RoutingPolicy(max_escalation_level=1)))
    assert review.outcome == "HUMAN_REVIEW"


def test_infrastructure_and_scope_failures_never_trigger_reasoning_escalation():
    selected = router()
    for failure in (FailureType.NETWORK, FailureType.MISSING_DEPENDENCY, FailureType.SCOPE_GUARD):
        decision = selected.select(request(complexity=TaskComplexity.SIMPLE, attempts=2, failure=failure))
        assert (decision.profile_id, decision.escalation_level) == ("economical", 0)


def test_profile_budget_precheck_fails_closed_unless_trusted_policy_allows_downgrade():
    selected = router()
    blocked = selected.select(request(input_tokens=15000, output_tokens=3000))
    assert blocked.outcome == "HUMAN_REVIEW"
    downgraded = selected.select(request(
        input_tokens=15000, output_tokens=3000,
        policy=RoutingPolicy(allow_budget_downgrade=True),
    ))
    assert downgraded.profile_id == "economical"


def configured_task(task_id: str) -> ConfiguredTask:
    return ConfiguredTask(
        task_id=task_id, project_id="test", title=task_id, task_type=TaskType.CODE_FIX,
        precheck=TaskCommand(argv=["python", "-c", "pass"]), postcheck=TaskCommand(argv=["python", "-c", "pass"]),
        allowed_files=["src/example.py"], context_files=["src/example.py"], requires_codex=True,
    )


def test_day_runner_persists_router_selection_and_profile_token_accounting():
    task = configured_task("A")
    plan = DayPlan(plan_id="p", title="p", task_ids=["A"])
    persisted = []

    class Architect:
        def choose(self, request, execution):
            assert "routing" not in request
            assert request["eligible_tasks"] == [{"task_id": "A", "title": "A", "task_type": "code_fix"}]
            assert (execution.provider, execution.model) == ("mock", None)
            return ArchitectDecision(task_id="A", reason="trusted queue", task_complexity="complex", token_usage=TokenUsage(input_tokens=20, cached_input_tokens=5, output_tokens=3, available=True))

    def execute(task_id, **kwargs):
        selected = kwargs["codex_routing_selector"](64, 0)
        assert selected and selected.profile_id == "deep"
        return {
            "task_id": task_id, "run_id": "r", "final_result": "COMPLETE", "codex_invoked": True,
            "codex_attempts": [object()], "gross_input_tokens": 10, "cached_input_tokens": 2,
            "output_tokens": 4, "precheck_result": "FAIL", "postcheck_result": "PASS", "scope_guard_result": "PASS",
        }

    day = DayRunner(
        DayPlanRegistry(plans={"p": plan}), TaskRegistry(tasks={"A": task}), execute,
        architects={"mock": Architect()}, model_router=router(),
        provider_budgets={role: ProviderBudget(daily_input_tokens=100000, daily_output_tokens=100000) for role in ("architect", "evaluator", "codex")},
        persist=persisted.append,
    )
    result = day.start("p")
    assert result["state"] == "COMPLETE"
    assert [decision["profile_id"] for decision in result["model_routing_decisions"]] == ["standard", "deep"]
    assert result["profile_token_usage"]["standard"]["call_count"] == 1
    assert result["profile_token_usage"]["deep"]["gross_input_tokens"] == 10
    assert persisted[-1]["model_routing_decisions"][-1]["selection_reason"]


def test_deterministic_completion_records_explicit_zero_ai_usage():
    task = configured_task("A")
    plan = DayPlan(plan_id="p", title="p", task_ids=["A"])

    def execute(task_id, **kwargs):
        return {"task_id": task_id, "final_result": "COMPLETE_NO_CHANGE", "codex_invoked": False, "codex_attempts": []}

    day = DayRunner(
        DayPlanRegistry(plans={"p": plan}), TaskRegistry(tasks={"A": task}), execute,
        model_router=router(),
        provider_budgets={role: ProviderBudget(daily_input_tokens=100000, daily_output_tokens=100000) for role in ("architect", "evaluator", "codex")},
    )
    result = day.start("p")
    assert result["deterministic_zero_usage_task_ids"] == ["A"]
    assert result["token_usage"]["codex"]["gross_input_tokens"] == 0
    assert [decision["role"] for decision in result["model_routing_decisions"]] == ["architect"]
    assert result["profile_token_usage"]["standard"]["call_count"] == 1
    assert result["profile_token_usage"]["economical"]["call_count"] == 0
    assert result["profile_token_usage"]["deep"]["call_count"] == 0
