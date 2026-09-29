from datetime import datetime, timezone
from pathlib import Path

from backend.control.task_discovery import DeterministicTaskDiscovery, DiscoveryDefinition, load_discovery_registry
from backend.control.tasks import TaskCommand
from backend.models.runtime import CommandRunResult


def definition(*, candidates=None, code_fix_error_codes=None):
    return DiscoveryDefinition(
        discovery_id="process_consistency",
        project_id="local_llm_lab",
        candidate_case_ids=candidates or ["PC-001-A", "PC-001-C", "PC-002-A"],
        precheck=TaskCommand(argv=["python", "scripts/check.py", "--case", "{case_id}", "--output-root", "{artifact_root}"]),
        code_fix_error_codes=code_fix_error_codes or ["ASSERTION_FAILURE"],
    )


def run_discovery(definition_value, responses):
    calls = []

    def fake_command(command, cwd, artifact_root):
        calls.append(command.argv)
        return responses[len(calls) - 1]

    result = DeterministicTaskDiscovery().discover(
        definition_value,
        run_id="test-run",
        started_at=datetime.now(timezone.utc),
        run_command=fake_command,
        working_directory=Path("C:/worktree"),
        artifact_root=Path("C:/artifacts"),
    )
    return result, calls


def test_process_consistency_candidates_are_configured_from_real_case_ids():
    registry = load_discovery_registry(Path("config/discovery.yaml"))
    configured = registry.get("process_consistency")
    assert configured is not None
    assert len(configured.candidate_case_ids) == 24
    assert configured.candidate_case_ids[:3] == ["PC-001-A", "PC-001-C", "PC-002-A"]


def test_discovery_skips_pass_then_selects_first_eligible_code_fix_and_stops():
    result, calls = run_discovery(definition(), [
        CommandRunResult(exit_code=0, passed=True),
        CommandRunResult(exit_code=1, passed=False, error_code="ASSERTION_FAILURE"),
        CommandRunResult(exit_code=1, passed=False, error_code="ASSERTION_FAILURE"),
    ])
    assert result.final_result == "CODE_FIX_SELECTED"
    assert result.selected_case_id == "PC-001-C"
    assert [item.status for item in result.cases] == ["PASS", "FAIL"]
    assert [item.classification for item in result.cases] == ["none", "CODE_FIX"]
    assert len(calls) == 2
    assert "PC-001-C" in calls[1]


def test_infrastructure_failure_is_not_selected_as_code_fix():
    result, calls = run_discovery(definition(candidates=["PC-001-A", "PC-001-C"]), [
        CommandRunResult(passed=False, error_code="COMMAND_NOT_FOUND"),
        CommandRunResult(exit_code=1, passed=False, error_code="ASSERTION_FAILURE"),
    ])
    assert result.selected_case_id == "PC-001-C"
    assert result.cases[0].status == "INFRASTRUCTURE_ERROR"
    assert result.cases[0].classification == "ENVIRONMENT"
    assert len(calls) == 2


def test_no_eligible_failure_requires_human_review_after_all_candidates():
    result, calls = run_discovery(definition(), [
        CommandRunResult(exit_code=0, passed=True),
        CommandRunResult(exit_code=1, passed=False, error_code="INVALID_CASE"),
        CommandRunResult(exit_code=1, passed=False, error_code="OUTPUT_ERROR"),
    ])
    assert result.final_result == "HUMAN_REVIEW_REQUIRED"
    assert result.selected_case_id is None
    assert result.cases[1].classification == "CONFIGURATION"
    assert result.cases[2].classification == "ENVIRONMENT"
    assert len(calls) == 3


def test_real_task_registry_contains_only_explicitly_configured_cases():
    from backend.control.tasks import load_task_registry

    tasks = load_task_registry(Path("config/tasks.yaml"))
    assert tasks.get("PC-001-A") is not None
    assert tasks.get("PC-001-C") is not None
    assert tasks.get("PC-002-A") is not None
    assert tasks.get("PC-999-A") is None


def test_engine_discovery_persists_no_candidate_human_review(tmp_path, monkeypatch):
    from backend.control.projects import ConfiguredProject, ProjectRegistry
    from backend.control.task_discovery import DiscoveryRegistry
    from backend.orchestrator.engine import ControlCenterEngine

    source = tmp_path / "LocalLLM-Lab"
    source.mkdir()
    (source / "check.py").write_text("pass\n", encoding="utf-8")
    for argv in (
        ["git", "init"],
        ["git", "add", "check.py"],
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "initial"],
    ):
        assert __import__("subprocess").run(argv, cwd=source, capture_output=True, check=False).returncode == 0
    discovery = definition(candidates=["PC-001-A"], code_fix_error_codes=[])
    engine = ControlCenterEngine(
        project_registry=ProjectRegistry(projects={"local_llm_lab": ConfiguredProject(name="LocalLLM-Lab", path=source, default_branch="main")}),
        discovery_registry=DiscoveryRegistry(discoveries={"process_consistency": discovery}),
    )
    monkeypatch.setattr(engine, "_run_task_command", lambda command, cwd, artifact: CommandRunResult(argv=command.argv, cwd=str(cwd), exit_code=0, passed=True))
    result = engine.discover_failing_task("process_consistency")
    assert result["final_result"] == "HUMAN_REVIEW_REQUIRED"
    assert result["cases"][0]["case_id"] == "PC-001-A"
    assert engine.status()["task_discoveries"][-1]["run_id"] == result["run_id"]
