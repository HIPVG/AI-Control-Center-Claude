from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from backend.models.task import TaskType


def _safe_relative_paths(values: list[str]) -> list[str]:
    normalized = [value.replace("\\", "/").strip() for value in values]
    if any(not value or value.startswith("/") or ":" in value or ".." in value.split("/") for value in normalized):
        raise ValueError("task paths must be safe relative paths")
    if len(normalized) != len(set(normalized)):
        raise ValueError("task paths must be unique")
    return normalized


class TaskCommand(BaseModel):
    argv: list[str] = Field(min_length=1)

    @field_validator("argv")
    @classmethod
    def argv_must_not_be_empty(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("command arguments must not be empty")
        return value


class ConfiguredTask(BaseModel):
    task_id: str = Field(min_length=1, max_length=120)
    project_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=300)
    task_type: TaskType
    working_directory: str = "."
    precheck: TaskCommand
    postcheck: TaskCommand
    allowed_files: list[str] = Field(min_length=1)
    context_files: list[str] = Field(min_length=1)
    max_retry: int = Field(default=1, ge=0, le=20)
    requires_codex: bool = True
    evaluator_type: Literal["deterministic", "semantic"] = "deterministic"
    independent_evaluator_required: bool = False
    evaluation_metrics: list[str] = Field(default_factory=list)
    context_max_characters: int = Field(default=16000, ge=1, le=100000)

    @field_validator("working_directory")
    @classmethod
    def working_directory_must_be_safe(cls, value: str) -> str:
        return _safe_relative_paths([value])[0]

    @field_validator("allowed_files", "context_files")
    @classmethod
    def task_paths_must_be_safe(cls, value: list[str]) -> list[str]:
        return _safe_relative_paths(value)

    @field_validator("evaluation_metrics")
    @classmethod
    def metrics_must_be_named(cls, value: list[str]) -> list[str]:
        if any(not metric.strip() for metric in value) or len(value) != len(set(value)):
            raise ValueError("evaluation metrics must be unique non-empty names")
        return value


class TaskRegistry(BaseModel):
    tasks: dict[str, ConfiguredTask] = Field(default_factory=dict)

    def get(self, task_id: str) -> ConfiguredTask | None:
        return self.tasks.get(task_id)

    def metadata(self) -> list[dict[str, str]]:
        return [
            {
                "task_id": task.task_id,
                "project_id": task.project_id,
                "title": task.title,
                "task_type": task.task_type.value,
                "evaluator_type": task.evaluator_type,
                "independent_evaluator_required": task.independent_evaluator_required,
            }
            for task in self.tasks.values()
        ]


def load_task_registry(path: Path) -> TaskRegistry:
    if not path.exists():
        return TaskRegistry()
    import yaml

    return TaskRegistry.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
