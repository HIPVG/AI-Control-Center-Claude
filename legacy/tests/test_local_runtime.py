import subprocess

from backend.control.local_runtime import ApprovedLocalRuntimeService
from backend.models.local_runtime import LocalRuntimeReadiness, LocalRuntimeReadinessState
from backend.models.runtime import RuntimeConfig
from backend.orchestrator.engine import ControlCenterEngine


class ReadyRuntime:
    def readiness(self):
        return LocalRuntimeReadiness(state=LocalRuntimeReadinessState.READY, reason_code="OLLAMA_READY", attempts=0)


class UnavailableRuntime:
    def readiness(self):
        return LocalRuntimeReadiness(state=LocalRuntimeReadinessState.EXTERNAL_ACTION_REQUIRED, reason_code="OLLAMA_START_TIMEOUT", attempts=8)


def test_approved_runtime_reports_ready_without_starting():
    starts = []
    service = ApprovedLocalRuntimeService(
        which=lambda _: "C:/approved/ollama.exe",
        run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
        popen=lambda *args, **kwargs: starts.append(args),
        sleep=lambda _: None,
    )
    result = service.readiness()
    assert result.state == LocalRuntimeReadinessState.READY
    assert result.reason_code == "OLLAMA_READY"
    assert starts == []


def test_approved_runtime_starts_only_fixed_ollama_command_then_waits():
    calls, starts = [], []

    def check(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0 if len(calls) == 2 else 1, "", "")

    service = ApprovedLocalRuntimeService(
        which=lambda _: "C:/approved/ollama.exe", run=check,
        popen=lambda argv, **kwargs: starts.append(argv), sleep=lambda _: None,
    )
    result = service.readiness()
    assert result.state == LocalRuntimeReadinessState.STARTED
    assert result.attempts == 1
    assert starts == [["ollama", "serve"]]
    assert calls == [["ollama", "list"], ["ollama", "list"]]


def test_missing_or_unstartable_runtime_requires_external_action_without_install():
    starts = []
    missing = ApprovedLocalRuntimeService(which=lambda _: None, popen=lambda *args, **kwargs: starts.append(args))
    assert missing.readiness().reason_code == "OLLAMA_NOT_INSTALLED"
    assert starts == []
    failed = ApprovedLocalRuntimeService(
        which=lambda _: "C:/approved/ollama.exe",
        run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", ""),
        popen=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("blocked")), sleep=lambda _: None,
    )
    assert failed.readiness().reason_code == "OLLAMA_START_FAILED"


def test_zero_touch_stops_at_external_runtime_boundary_before_experiment(monkeypatch):
    engine = ControlCenterEngine(runtime_config=RuntimeConfig(), local_runtime_service=UnavailableRuntime())
    calls = []
    monkeypatch.setattr(engine, "run_experiment", lambda target: calls.append(target))
    result = engine.start_zero_touch("Run the trusted LocalLLM process consistency experiment")
    assert result["status"] == "ATTENTION"
    assert result["action_type"] == "EXTERNAL_ACTION_REQUIRED"
    assert result["completion_reason"] == "OLLAMA_START_TIMEOUT"
    assert calls == []
    assert engine.runtime_readiness()["state"] == "EXTERNAL_ACTION_REQUIRED"
    assert engine.timeline[-2].event_type.value == "LOCAL_RUNTIME_EXTERNAL_ACTION"


def test_zero_touch_runs_after_ready_runtime_preflight(monkeypatch):
    engine = ControlCenterEngine(runtime_config=RuntimeConfig(), local_runtime_service=ReadyRuntime())
    monkeypatch.setattr(engine, "run_experiment", lambda target: {"experiment_id": target, "outcome": "RESULT_RECORDED"})
    result = engine.start_zero_touch("Run the trusted LocalLLM process consistency experiment")
    assert result["status"] == "COMPLETE"
    assert engine.runtime_readiness()["state"] == "READY"
    assert any(event.event_type.value == "LOCAL_RUNTIME_PREFLIGHT" for event in engine.timeline)
