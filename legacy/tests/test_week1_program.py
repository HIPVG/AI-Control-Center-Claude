import json

from backend.control.projects import ConfiguredProject, ProjectRegistry
from backend.models.runtime import RuntimeConfig
from backend.orchestrator.engine import ControlCenterEngine


def local_lab(tmp_path, *, cross_family=False, benchmark=False):
    (tmp_path / "config").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "week1-runbook.md").write_text("approved Week 1 runbook", encoding="utf-8")
    models = [{"id": "qwen", "family": "qwen3", "runtime_model_name": "qwen:latest"}]
    if cross_family:
        models.append({"id": "gemma", "family": "gemma3", "runtime_model_name": "gemma:latest"})
    (tmp_path / "config" / "model-matrix.json").write_text(json.dumps({"models": models}), encoding="utf-8")
    (tmp_path / "config" / "benchmark-plan.yaml").write_text(json.dumps({"execution_enabled": benchmark}), encoding="utf-8")
    (tmp_path / "config" / "run-profiles.json").write_text(json.dumps({"profiles": [{"id": "context", "execution_enabled": False}]}), encoding="utf-8")
    return tmp_path


def engine_for(tmp_path):
    root = local_lab(tmp_path)
    return ControlCenterEngine(runtime_config=RuntimeConfig(), project_registry=ProjectRegistry(projects={"local_llm_lab": ConfiguredProject(name="LocalLLM-Lab", path=root, default_branch="main")}))


def test_week1_recommends_day4_without_a_new_goal_and_records_typed_missing_capability(tmp_path):
    engine = engine_for(tmp_path)
    assert engine.next_action()["action_type"] == "RUN_WEEK1_DAY"
    result = engine.continue_autonomously()
    assert result["error_code"] == "EXTERNAL_ACTION_REQUIRED"
    assert result["result"]["reason_code"] == "APPROVED_CROSS_FAMILY_RUNTIME_MISSING"
    assert engine.next_action()["action_type"] == "EXTERNAL_ACTION_REQUIRED"
    assert engine.data["experiment_runs"] == []


def test_day6_preparation_is_deterministic_and_day7_requires_direction(tmp_path):
    engine = engine_for(tmp_path)
    engine.data["week1_days"] = [{"day": 4, "status": "COMPLETE"}, {"day": 5, "status": "COMPLETE"}]
    assert engine.next_action()["target_id"] == "week1-day6"
    assert engine.continue_autonomously()["result"]["reason_code"] == "CONTEXT_PREPARATION_RECORDED"
    result = engine.continue_autonomously()
    assert result["error_code"] == "HUMAN_DECISION_REQUIRED"
    assert result["result"]["reason_code"] == "NEXT_PHASE_DIRECTION_REQUIRED"
