import json
import subprocess

from backend.control.projects import ConfiguredProject, ProjectRegistry
from backend.models.experiment import TrustedExperiment
from backend.models.runtime import RuntimeConfig
from backend.orchestrator.engine import ControlCenterEngine


def next_action_engine(tmp_path):
    runner = tmp_path / "scripts" / "run_process_consistency_smoke.py"
    runner.parent.mkdir()
    runner.write_text("# trusted runner", encoding="utf-8")
    engine = ControlCenterEngine(runtime_config=RuntimeConfig(), project_registry=ProjectRegistry(projects={"local_llm_lab": ConfiguredProject(name="LocalLLM-Lab", path=tmp_path, default_branch="main")}))
    engine.experiments = {"local_llm_process_consistency_smoke": TrustedExperiment(experiment_id="local_llm_process_consistency_smoke", project_id="local_llm_lab", runner="scripts/run_process_consistency_smoke.py", model="qwen3-8b-q4:latest", cases=["PC-001-A"], output_root="results/process-consistency")}
    return engine


def test_next_action_is_a_configured_trusted_experiment_and_continues_without_goal(monkeypatch, tmp_path):
    engine = next_action_engine(tmp_path)
    action = engine.next_action()
    assert action["action_type"] == "RUN_TRUSTED_EXPERIMENT"
    assert action["target_id"] == "local_llm_process_consistency_smoke"
    payload = {"status": "completed", "output_directory": str(tmp_path / "results" / "run"), "response_count": 1, "success_count": 1, "failed_count": 0, "error": None}
    calls = []
    monkeypatch.setattr("backend.orchestrator.engine.subprocess.run", lambda argv, **kwargs: calls.append((argv, kwargs)) or subprocess.CompletedProcess(argv, 0, json.dumps(payload), ""))
    result = engine.continue_autonomously()
    assert result["result"]["outcome"] == "RESULT_RECORDED"
    assert result["result"]["builder_invoked"] is False
    assert calls[0][0][0] != "codex"
    assert any(event.event_type.value == "NEXT_ACTION_CONTINUED" for event in engine.timeline)


def test_runtime_blocked_experiment_requires_external_action_and_cannot_continue(monkeypatch, tmp_path):
    engine = next_action_engine(tmp_path)
    payload = {"status": "blocked", "error": {"code": "engine_unavailable"}}
    monkeypatch.setattr("backend.orchestrator.engine.subprocess.run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, json.dumps(payload), ""))
    assert engine.run_experiment("local_llm_process_consistency_smoke")["outcome"] == "ENGINE_UNAVAILABLE"
    action = engine.next_action()
    assert action["action_type"] == "EXTERNAL_ACTION_REQUIRED"
    assert action["human_attention_required"] is True
    assert engine.continue_autonomously()["error_code"] == "NEXT_ACTION_REQUIRES_ATTENTION"


def test_verified_task_run_is_prioritized_as_the_next_automatic_git_action(tmp_path):
    engine = next_action_engine(tmp_path)
    engine.data["task_runs"].append({
        "run_id": "verified-run", "task_id": "TASK-1", "project_id": "local_llm_lab",
        "worktree_path": str(tmp_path), "task_branch": "agent/task-1", "allowed_files": ["src/a.py"],
        "changed_files": ["src/a.py"], "state": "COMPLETE", "final_result": "COMPLETE",
        "postcheck_result": "PASS", "scope_guard_result": "PASS",
    })
    action = engine.next_action()
    assert action["action_type"] == "COMPLETE_VERIFIED_WORK"
    assert action["target_id"] == "verified-run"
    assert action["policy_result"] == "VERIFIED_AGENT_BRANCH_ONLY"
