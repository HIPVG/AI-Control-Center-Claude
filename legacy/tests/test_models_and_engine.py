import pytest
from pydantic import ValidationError

from backend.models.task import TaskType, WorkOrder
from backend.control.scope_guard import ScopeGuard
from backend.orchestrator.engine import ControlCenterEngine, StateStore
from backend.orchestrator.progress import calculate_progress


def test_structured_model_rejects_unsafe_scope_path():
    with pytest.raises(ValidationError):
        WorkOrder(task_id="T", goal="x", task_type=TaskType.CODE_FIX, allowed_files=["../secret"], acceptance_tests=["pytest"])


def test_scope_guard_preserves_leading_dot_in_safe_fixture_directory():
    work_order = WorkOrder(task_id="T", goal="x", task_type=TaskType.CODE_FIX, allowed_files=[".fixture/status.txt"], acceptance_tests=["pytest"])
    assert ScopeGuard().check(work_order, [".fixture/status.txt"]).allowed


def test_progress_values_are_independent_and_in_range():
    status = ControlCenterEngine().status()
    assert (status["overall_progress"], status["day_progress"], status["task_progress"]) == (58, 63, 82)
    assert all(0 <= value <= 100 for value in (status["overall_progress"], status["day_progress"], status["task_progress"]))
    assert calculate_progress(18, 22) == 82


def test_mock_workflow_completes_without_external_services():
    status = ControlCenterEngine().run_mock()
    assert status["run_state"]["state"] == "COMPLETE"
    assert status["task_progress"] == 100
    assert status["token_usage"]["input_tokens"] == 0
    assert status["token_usage"]["cached_input_tokens"] == 0
    assert status["token_usage"]["output_tokens"] == 0
    assert status["overall_progress"] == 62
    assert status["day_progress"] == 67
    assert status["timeline"][-1]["event_type"] == "PROGRESS_UPDATED"
    assert status["timeline"][-1]["details"]["delta"] == {"overall": 4, "day": 4, "task": 18}


def test_mock_workflow_records_structured_state_transitions():
    timeline = ControlCenterEngine().run_mock()["timeline"]
    transitions = [event for event in timeline if event["event_type"] == "STATE_TRANSITION"]
    assert transitions[0]["from_state"] == "IDLE"
    assert transitions[0]["to_state"] == "PLANNING"
    assert transitions[-1]["to_state"] == "COMPLETE"


def test_completed_task_is_not_counted_twice():
    engine = ControlCenterEngine()
    completed = engine.run_mock()
    repeated = engine.run_mock()
    assert repeated["overall_progress"] == completed["overall_progress"]
    assert repeated["day_progress"] == completed["day_progress"]
    assert repeated["timeline"][-1]["event_type"] == "WORKFLOW_SKIPPED"


class LegacyStore(StateStore):
    def load(self):
        return {
            "overall_progress": 58,
            "day_progress": 63,
            "task_progress": 100,
            "current_task": {"task_id": "PC-014", "title": "Evidence Grounding", "completed": 22, "total": 22, "retry": 0, "max_retry": 2},
            "tasks": [{"task_id": "PC-014", "title": "Evidence Grounding", "state": "COMPLETE", "progress": 100}],
            "timeline": [{"timestamp": "2026-01-01T00:00:00+00:00", "message": "legacy completion"}],
        }

    def save(self, data):
        pass


def test_legacy_completed_task_backfills_progress_with_audit_evidence():
    status = ControlCenterEngine(state_store=LegacyStore()).status()
    assert (status["overall_progress"], status["day_progress"]) == (62, 67)
    assert status["timeline"][-1]["details"]["migration"] == "v0.1 progress tracking backfill"


def test_legacy_mock_token_usage_is_reconciled_with_audit_evidence():
    class LegacyTokenStore(LegacyStore):
        def load(self):
            data = super().load()
            data["token_usage"] = {"input_tokens": 1200, "cached_input_tokens": 200, "output_tokens": 350}
            return data

    status = ControlCenterEngine(state_store=LegacyTokenStore()).status()
    assert status["token_usage"]["input_tokens"] == 0
    assert status["timeline"][-1]["event_type"] == "TOKEN_USAGE_RECONCILED"
