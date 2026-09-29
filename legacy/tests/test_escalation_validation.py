from backend.models.runtime import RuntimeConfig
from backend.orchestrator.engine import ControlCenterEngine


def test_validation_only_transient_architect_timeout_recovers_once_without_mutating_normal_day():
    engine = ControlCenterEngine(runtime_config=RuntimeConfig())
    result = engine.run_escalation_validation("transient-architect")
    day = result["result"]
    assert result["isolated"] is True and result["normal_day_unchanged"] is True
    assert day["state"] == "COMPLETE"
    assert day["auto_provider_retries"] == 1
    assert day["human_review_queue"] == []


def test_validation_only_missing_runtime_is_external_action_without_retry():
    engine = ControlCenterEngine(runtime_config=RuntimeConfig())
    day = engine.run_escalation_validation("missing-runtime")["result"]
    assert day["state"] == "HUMAN_REVIEW"
    assert day["auto_provider_retries"] == 0
    assert day["human_review_queue"][0]["escalation_category"] == "EXTERNAL_ACTION_REQUIRED"


def test_validation_only_invalid_architect_task_replans_once_and_completes():
    engine = ControlCenterEngine(runtime_config=RuntimeConfig())
    day = engine.run_escalation_validation("invalid-architect-task")["result"]
    assert day["state"] == "COMPLETE"
    assert day["auto_replans"] == 1
    assert day["auto_provider_retries"] == 0
    assert day["human_review_queue"] == []
    assert day["escalation_events"][-1] == {
        "category": "REPLAN_REQUIRED", "reason": "ARCHITECT_INVALID_TASK", "action": "REPLAN_ARCHITECT",
    }
