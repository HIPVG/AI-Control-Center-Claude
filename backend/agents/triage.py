from enum import Enum

from backend.models.task import TaskType, WorkOrder


class TriageDecision(str, Enum):
    CODE_FIX = "CODE_FIX"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class MockTriage:
    def classify(self, work_order: WorkOrder) -> TriageDecision:
        return TriageDecision.CODE_FIX if work_order.task_type == TaskType.CODE_FIX and work_order.requires_implementation else TriageDecision.HUMAN_REVIEW
