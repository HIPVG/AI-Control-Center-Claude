from typing import Protocol

from backend.models.task import WorkOrder


class Architect(Protocol):
    def create_work_order(self) -> WorkOrder: ...


class MockArchitect:
    def create_work_order(self) -> WorkOrder:
        return WorkOrder(
            task_id="PC-014",
            goal="Restore evidence grounding validation.",
            task_type="code_fix",
            allowed_files=["src/evaluator.py"],
            acceptance_tests=["pytest tests/test_pc014.py"],
            requires_implementation=True,
        )
