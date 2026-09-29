import json
import subprocess

from backend.control.projects import ConfiguredProject, ProjectRegistry
from backend.models.experiment import ExperimentOutcome, TrustedExperiment
from backend.models.runtime import RuntimeConfig
from backend.orchestrator.engine import ControlCenterEngine


def experiment_engine(tmp_path):
    runner = tmp_path / "scripts" / "run_process_consistency_smoke.py"
    runner.parent.mkdir()
    runner.write_text("# trusted runner fixture\n", encoding="utf-8")
    engine = ControlCenterEngine(
        runtime_config=RuntimeConfig(),
        project_registry=ProjectRegistry(projects={"local_llm_lab": ConfiguredProject(name="LocalLLM-Lab", path=tmp_path, default_branch="main")}),
    )
    engine.experiments = {"trusted": TrustedExperiment(experiment_id="trusted", project_id="local_llm_lab", runner="scripts/run_process_consistency_smoke.py", model="qwen3-8b-q4:latest", cases=["PC-001-A"], output_root="results/process-consistency")}
    return engine


def test_real_experiment_adapter_reuses_only_trusted_runner_and_records_artifact(monkeypatch, tmp_path):
    engine = experiment_engine(tmp_path)
    payload = {"status": "completed", "output_directory": str(tmp_path / "results" / "run"), "response_count": 1, "success_count": 1, "failed_count": 0, "error": None}
    observed = {}

    def run(argv, **kwargs):
        observed.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("backend.orchestrator.engine.subprocess.run", run)
    result = engine.run_experiment("trusted")
    assert result["outcome"] == ExperimentOutcome.RESULT_RECORDED.value
    assert result["artifact_path"] == payload["output_directory"]
    assert result["builder_invoked"] is False
    assert "--dry-run" not in observed["argv"]
    assert observed["kwargs"]["cwd"] == tmp_path


def test_model_quality_result_never_invokes_builder(monkeypatch, tmp_path):
    engine = experiment_engine(tmp_path)
    payload = {"status": "completed_with_errors", "output_directory": str(tmp_path / "results" / "run"), "response_count": 1, "success_count": 0, "failed_count": 1, "error": None}
    monkeypatch.setattr("backend.orchestrator.engine.subprocess.run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout=json.dumps(payload), stderr=""))
    result = engine.run_experiment("trusted")
    assert result["outcome"] == ExperimentOutcome.MODEL_QUALITY_FINDING.value
    assert result["builder_invoked"] is False


def test_missing_local_model_is_an_external_runtime_result(monkeypatch, tmp_path):
    engine = experiment_engine(tmp_path)
    payload = {"status": "blocked", "response_count": 0, "success_count": 0, "failed_count": 0, "error": {"code": "model_not_found"}}
    monkeypatch.setattr("backend.orchestrator.engine.subprocess.run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 2, stdout=json.dumps(payload), stderr=""))
    assert engine.run_experiment("trusted")["outcome"] == ExperimentOutcome.MODEL_NOT_FOUND.value
