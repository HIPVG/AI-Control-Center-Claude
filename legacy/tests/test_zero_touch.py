import json
import subprocess

from backend.control.projects import ConfiguredProject, ProjectRegistry
from backend.models.experiment import TrustedExperiment
from backend.models.runtime import RuntimeConfig
from backend.models.local_runtime import LocalRuntimeReadiness, LocalRuntimeReadinessState
from backend.orchestrator.engine import ControlCenterEngine


def zero_touch_engine(tmp_path):
    runner = tmp_path / "scripts" / "run_process_consistency_smoke.py"
    runner.parent.mkdir()
    runner.write_text("# trusted runner", encoding="utf-8")
    class ReadyRuntime:
        def readiness(self):
            return LocalRuntimeReadiness(state=LocalRuntimeReadinessState.READY, reason_code="OLLAMA_READY", attempts=0)

    engine = ControlCenterEngine(runtime_config=RuntimeConfig(), project_registry=ProjectRegistry(projects={"local_llm_lab": ConfiguredProject(name="LocalLLM-Lab", path=tmp_path, default_branch="main")}), local_runtime_service=ReadyRuntime())
    engine.experiments = {"local_llm_process_consistency_smoke": TrustedExperiment(experiment_id="local_llm_process_consistency_smoke", project_id="local_llm_lab", runner="scripts/run_process_consistency_smoke.py", model="qwen3-8b-q4:latest", cases=["PC-001-A"], output_root="results/process-consistency")}
    return engine


def test_zero_touch_goal_plans_executes_and_completes_without_repeated_goal(monkeypatch, tmp_path):
    engine = zero_touch_engine(tmp_path)
    payload = {"status": "completed", "output_directory": str(tmp_path / "results" / "run"), "response_count": 1, "success_count": 1, "failed_count": 0, "error": None}
    monkeypatch.setattr("backend.orchestrator.engine.subprocess.run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, json.dumps(payload), ""))
    result = engine.start_zero_touch("Run the trusted LocalLLM process consistency experiment")
    assert result["status"] == "COMPLETE"
    assert result["completion_reason"] == "TRUSTED_GOAL_EXECUTED"
    assert result["outcome"] == "RESULT_RECORDED"
    assert engine.goal_plans()[-1]["status"] == "EXECUTED"
    assert engine.data["experiment_runs"][-1]["builder_invoked"] is False
    assert engine.next_action()["action_type"] == "NO_FURTHER_ACTION"
    assert [event.event_type.value for event in engine.timeline if event.event_type.value.startswith("ZERO_TOUCH_")] == ["ZERO_TOUCH_STARTED", "ZERO_TOUCH_COMPLETE"]


def test_zero_touch_rejected_goal_requires_attention_without_execution(tmp_path):
    engine = zero_touch_engine(tmp_path)
    result = engine.start_zero_touch("Download a model then run a LocalLLM experiment")
    assert result["status"] == "ATTENTION"
    assert result["completion_reason"] == "FORBIDDEN_AUTHORITY_REQUEST"
    assert engine.data["experiment_runs"] == []


def test_zero_touch_continue_records_current_trusted_action(monkeypatch, tmp_path):
    engine = zero_touch_engine(tmp_path)
    monkeypatch.setattr(engine, "continue_autonomously", lambda: {"next_action": {"action_type": "COMPLETE_VERIFIED_WORK", "target_id": "run"}, "result": {"status": "PR_READY"}})
    result = engine.continue_zero_touch()
    assert result["status"] == "COMPLETE"
    assert result["outcome"] == "PR_READY"
