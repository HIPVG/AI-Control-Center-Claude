from datetime import datetime, timezone
from pathlib import Path
import subprocess

import pytest

from backend.control.day_git import day_admission, fingerprint
from backend.control.local_llm_day_program import LocalLLMDayProgram
from backend.models.local_llm_day import RunIntent, RunLimits


def _git(root: Path, *args: str) -> None:
    completed = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    _git(tmp_path, "init", "-b", "main", str(root))
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    (root / "docs").mkdir()
    (root / "docs" / "contract.md").write_text("fixed contract\n", encoding="utf-8")
    _git(root, "add", "docs/contract.md")
    _git(root, "commit", "-m", "fixture baseline")
    return root


def _intent(
    root: Path,
    *,
    day: int = 1,
    contract: str = "contract-v1",
    active_work_seconds: int = 1800,
    max_attempts: int = 2,
) -> RunIntent:
    return RunIntent(
        run_id="admission-fixture", selected_day=day, go_at=datetime.now(timezone.utc),
        contract_fingerprint=contract, policy_fingerprint="policy-v1", config_fingerprint="config-v1",
        git_fingerprint=fingerprint(root),
        requested_limits=RunLimits(active_work_seconds=active_work_seconds, max_attempts=max_attempts,
                                   max_cost=0, currency="JPY"),
    )


def _admit(root: Path, intent: RunIntent | None, **kwargs):
    return day_admission(
        root, intent=intent, expected_day=1, expected_contract_fingerprint="contract-v1",
        effective_permission=kwargs.get("effective_permission", True),
        external_prerequisite=kwargs.get("external_prerequisite", True),
    )


def test_clean_matching_inputs_are_admissible_only_to_preflight(tmp_path):
    root = _repository(tmp_path)

    result = _admit(root, _intent(root))

    assert result.status == "ADMISSIBLE"
    assert result.next_state == "PREFLIGHT"
    assert result.reason_code is None
    assert result.git_fingerprint == fingerprint(root)


def test_dirty_git_is_preserved_and_blocks_before_preflight(tmp_path):
    root = _repository(tmp_path)
    intent = _intent(root)
    changed = root / "docs" / "contract.md"
    changed.write_text("user change\n", encoding="utf-8")

    result = _admit(root, intent)

    assert (result.status, result.next_state, result.reason_code) == (
        "BLOCKED", "HUMAN_ACTION_REQUIRED", "DIRTY_GIT_BASELINE")
    assert changed.read_text(encoding="utf-8") == "user change\n"


def test_unknown_permission_blocks_without_trying_a_day_action(tmp_path):
    root = _repository(tmp_path)

    result = _admit(root, _intent(root), effective_permission=None)

    assert (result.status, result.next_state, result.reason_code) == (
        "BLOCKED", "HUMAN_ACTION_REQUIRED", "EFFECTIVE_PERMISSION_UNKNOWN")


def test_missing_limits_and_contract_mismatch_are_separate_blockers(tmp_path):
    root = _repository(tmp_path)

    missing = _admit(root, None)
    mismatch = _admit(root, _intent(root, contract="other-contract"))

    assert (missing.status, missing.reason_code) == ("BLOCKED", "LIMITS_UNCONFIRMED")
    assert (mismatch.status, mismatch.reason_code) == ("BLOCKED", "CONTRACT_FINGERPRINT_MISMATCH")


@pytest.mark.parametrize(("limits", "value"), [
    ("active_work_seconds", 1801),
    ("max_attempts", 3),
])
def test_limits_above_approved_bounds_fail_closed_before_git_or_day_action(tmp_path, limits, value):
    root = _repository(tmp_path)
    intent = _intent(root, **{limits: value})
    changed = root / "docs" / "contract.md"
    changed.write_text("user change\n", encoding="utf-8")

    result = _admit(root, intent)

    assert (result.status, result.next_state, result.reason_code) == (
        "BLOCKED", "HUMAN_ACTION_REQUIRED", "LIMITS_EXCEED_APPROVED_BOUND")
    assert result.git_fingerprint is None
    assert result.next_action == "Use limits at or below 1800 active-work seconds and 2 attempts."
    assert changed.read_text(encoding="utf-8") == "user change\n"


def test_unconfirmed_external_prerequisite_stops_without_day_execution(tmp_path):
    root = _repository(tmp_path)

    result = _admit(root, _intent(root), external_prerequisite=False)

    assert (result.status, result.next_state, result.reason_code) == (
        "BLOCKED", "EXTERNAL_ACTION_REQUIRED", "EXTERNAL_PREREQUISITE_UNCONFIRMED")


def test_program_boundary_exposes_missing_intent_without_selecting_or_starting_a_day():
    fixture_root = Path(__file__).parent / "fixtures" / "day-contract"
    program = LocalLLMDayProgram(fixture_root)

    result = program.admission(None, effective_permission=None, external_prerequisite=None)

    assert (result["status"], result["next_state"], result["reason_code"]) == (
        "BLOCKED", "HUMAN_ACTION_REQUIRED", "LIMITS_UNCONFIRMED")
    assert program.snapshot.selected_day is None
    assert program.snapshot.state.value == "IDLE"
