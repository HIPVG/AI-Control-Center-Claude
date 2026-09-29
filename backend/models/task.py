from enum import Enum

from pydantic import BaseModel, Field, field_validator


class TaskType(str, Enum):
    CODE_FIX = "code_fix"
    CONFIG = "config"
    DATA = "data"
    PLAN = "plan"


class WorkOrder(BaseModel):
    task_id: str = Field(min_length=1, max_length=120)
    goal: str = Field(min_length=1, max_length=2000)
    task_type: TaskType
    priority: str = "normal"
    allowed_files: list[str] = Field(min_length=1)
    acceptance_tests: list[str] = Field(min_length=1)
    max_retry: int = Field(default=2, ge=0, le=20)
    requires_implementation: bool = False

    @field_validator("allowed_files")
    @classmethod
    def allowed_files_must_be_relative(cls, value: list[str]) -> list[str]:
        normalized = [item.replace("\\", "/").strip() for item in value]
        if any(not item or item.startswith("/") or ":" in item or ".." in item.split("/") for item in normalized):
            raise ValueError("allowed_files must contain safe relative paths")
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed_files must be unique")
        return normalized
