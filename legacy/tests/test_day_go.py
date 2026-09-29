from pathlib import Path
import shutil
import subprocess

from backend.control.local_llm_day_program import LocalLLMDayProgram


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "day-contract"


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    shutil.copytree(FIXTURE_ROOT, root)
    _git(tmp_path, "init", "-b", "main", str(root))
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture baseline")
    return root


def test_go_creates_one_run_intent_and_hands_same_identity_only_to_preflight(tmp_path):
    root = _repository(tmp_path)
    program = LocalLLMDayProgram(root)

    result = program.prepare_go(6, effective_permission=True, external_prerequisite=True)

    assert result["run_id"] == result["admission_run_id"] == result["run_intent"]["run_id"]
    assert result["selected_day"] == result["run_intent"]["selected_day"] == 6
    assert result["admission"]["status"] == "ADMISSIBLE"
    assert result["admission"]["next_state"] == "PREFLIGHT"
    assert result["execution_started"] is False
    assert result["snapshot"]["selected_day"] is None
    assert result["snapshot"]["state"] == "IDLE"
    assert program._thread is None


def test_go_does_not_accept_browser_permission_or_start_a_day(tmp_path):
    root = _repository(tmp_path)
    program = LocalLLMDayProgram(root)

    result = program.prepare_go(6)

    assert result["run_id"] == result["run_intent"]["run_id"]
    assert result["admission"]["status"] == "BLOCKED"
    assert result["admission"]["reason_code"] == "EFFECTIVE_PERMISSION_UNKNOWN"
    assert result["execution_started"] is False
    assert program.snapshot.selected_day is None
    assert program.snapshot.state.value == "IDLE"
    assert program._thread is None
