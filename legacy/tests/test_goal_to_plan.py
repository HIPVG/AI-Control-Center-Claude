import json
import subprocess

from backend.control.projects import ConfiguredProject, ProjectRegistry
from backend.models.experiment import TrustedExperiment
from backend.models.runtime import RuntimeConfig
from backend.orchestrator.engine import ControlCenterEngine


def goal_engine(tmp_path):
    runner = tmp_path / "scripts" / "run_process_consistency_smoke.py"
    runner.parent.mkdir()
    runner.write_text("# trusted runner", encoding="utf-8")
    engine = ControlCenterEngine(runtime_config=RuntimeConfig(), project_registry=ProjectRegistry(projects={"local_llm_lab": ConfiguredProject(name="LocalLLM-Lab", path=tmp_path, default_branch="main")}))
    engine.experiments = {"local_llm_process_consistency_smoke": TrustedExperiment(experiment_id="local_llm_process_consistency_smoke", project_id="local_llm_lab", runner="scripts/run_process_consistency_smoke.py", model="qwen3-8b-q4:latest", cases=["PC-001-A"], output_root="results/process-consistency")}
    return engine


def test_goal_proposes_only_configured_local_llm_experiment_and_executes_after_confirmation(monkeypatch, tmp_path):
    engine = goal_engine(tmp_path)
    proposal = engine.propose_goal("Run the trusted LocalLLM process consistency experiment")
    assert proposal["status"] == "PROPOSED"
    assert proposal["target_id"] == "local_llm_process_consistency_smoke"
    assert "trusted LocalLLM" in proposal["summary"]
    payload = {"status": "completed", "output_directory": str(tmp_path / "results" / "run"), "response_count": 1, "success_count": 1, "failed_count": 0, "error": None}
    monkeypatch.setattr("backend.orchestrator.engine.subprocess.run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, json.dumps(payload), ""))
    executed = engine.execute_goal(proposal["goal_id"])
    assert executed["goal_plan"]["status"] == "EXECUTED"
    assert executed["result"]["builder_invoked"] is False
    assert engine.execute_goal(proposal["goal_id"])["error_code"] == "GOAL_PLAN_NOT_EXECUTABLE"


def test_goal_cannot_request_new_authority_or_unrecognized_capability(tmp_path):
    engine = goal_engine(tmp_path)
    assert engine.propose_goal("Download a model then run a LocalLLM experiment")["policy_reason"] == "FORBIDDEN_AUTHORITY_REQUEST"
    assert engine.propose_goal("Refactor the repository")["policy_reason"] == "GOAL_NOT_RECOGNIZED"
    stored = engine.goal_plans()[0]
    assert "Download" not in str(stored)
