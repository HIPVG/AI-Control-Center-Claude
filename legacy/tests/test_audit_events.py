import inspect
import re

from backend.models.audit import AuditEvent, AuditEventType
from backend.orchestrator.day_runner import DayRunner


def test_model_router_and_deterministic_day_events_are_first_class_enum_members():
    assert AuditEventType("DAY_MODEL_ROUTING") is AuditEventType.DAY_MODEL_ROUTING
    assert AuditEventType("DAY_DETERMINISTIC_NO_AI") is AuditEventType.DAY_DETERMINISTIC_NO_AI
    assert AuditEventType("AUTHORITY_BLOCKER_REPLACED_BY_REGISTERED_RETRY") is AuditEventType.AUTHORITY_BLOCKER_REPLACED_BY_REGISTERED_RETRY


def test_every_static_day_runner_audit_event_is_registered():
    source = inspect.getsource(DayRunner)
    produced = set(re.findall(r'self\._audit\([^,]+,\s*"(DAY_[A-Z_]+)"', source))
    registered = {event.value for event in AuditEventType}
    assert produced == {
        "DAY_PLAN_STARTED",
        "DAY_ARCHITECT_DECISION",
        "DAY_MODEL_ROUTING",
        "DAY_DETERMINISTIC_NO_AI",
        "DAY_TASK_RESULT",
        "DAY_EVALUATION_RESULT",
        "DAY_HUMAN_REVIEW",
        "DAY_ESCALATION",
        "DAY_STOPPED",
        "DAY_COMPLETE",
    }
    assert produced <= registered


def test_new_day_audit_events_remain_serializable():
    for event_type in (AuditEventType.DAY_MODEL_ROUTING, AuditEventType.DAY_ESCALATION):
        event = AuditEvent(task_id="PC-001-A", event_type=event_type, message="structured Day event")
        assert event.model_dump(mode="json")["event_type"] == event_type.value
