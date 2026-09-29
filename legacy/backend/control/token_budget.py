from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from backend.models.result import TokenUsage


class BudgetDecision(str, Enum):
    ALLOWED = "ALLOWED"
    TASK_BUDGET_EXCEEDED = "TASK_BUDGET_EXCEEDED"
    DAILY_BUDGET_EXCEEDED = "DAILY_BUDGET_EXCEEDED"
    RETRY_LIMIT_EXCEEDED = "RETRY_LIMIT_EXCEEDED"


class CodexBudget(BaseModel):
    daily_input_tokens: int = Field(default=300000, ge=0)
    daily_output_tokens: int = Field(default=50000, ge=0)
    task_input_tokens: int = Field(default=40000, ge=0)
    task_output_tokens: int = Field(default=10000, ge=0)
    max_retry: int = Field(default=2, ge=0)


class EvaluatorBudget(BaseModel):
    daily_input_tokens: int = Field(default=150000, ge=0)
    daily_output_tokens: int = Field(default=20000, ge=0)


class BudgetConfig(BaseModel):
    codex: CodexBudget = Field(default_factory=CodexBudget)
    evaluator: EvaluatorBudget = Field(default_factory=EvaluatorBudget)


class TokenBudgetManager:
    def __init__(self, config: BudgetConfig | None = None, day_usage: TokenUsage | None = None) -> None:
        self.config = config or BudgetConfig()
        self.day_usage = day_usage or TokenUsage()

    def check(self, requested: TokenUsage, *, retry_count: int) -> BudgetDecision:
        budget = self.config.codex
        if retry_count >= budget.max_retry:
            return BudgetDecision.RETRY_LIMIT_EXCEEDED
        if requested.input_tokens > budget.task_input_tokens or requested.output_tokens > budget.task_output_tokens:
            return BudgetDecision.TASK_BUDGET_EXCEEDED
        if self.day_usage.input_tokens + requested.input_tokens > budget.daily_input_tokens or self.day_usage.output_tokens + requested.output_tokens > budget.daily_output_tokens:
            return BudgetDecision.DAILY_BUDGET_EXCEEDED
        return BudgetDecision.ALLOWED

    def record(self, usage: TokenUsage, *, retry_count: int) -> BudgetDecision:
        decision = self.check(usage, retry_count=retry_count)
        if decision == BudgetDecision.ALLOWED:
            self._add_usage(usage)
        return decision

    def record_actual(self, usage: TokenUsage, *, retry_count: int) -> BudgetDecision:
        """Record observed usage without retroactively invalidating completed work."""
        decision = self.check(usage, retry_count=retry_count)
        self._add_usage(usage)
        return decision

    def _add_usage(self, usage: TokenUsage) -> None:
        self.day_usage = TokenUsage(
            input_tokens=self.day_usage.input_tokens + usage.input_tokens,
            cached_input_tokens=self.day_usage.cached_input_tokens + usage.cached_input_tokens,
            output_tokens=self.day_usage.output_tokens + usage.output_tokens,
            available=self.day_usage.available or usage.available,
        )

    def usage_view(self) -> dict[str, int | float]:
        budget = self.config.codex
        total_limit = budget.daily_input_tokens + budget.daily_output_tokens
        return {
            **self.day_usage.model_dump(),
            "task_total": self.day_usage.total_tokens,
            "day_total": self.day_usage.total_tokens,
            "budget_percent": round((self.day_usage.total_tokens / total_limit * 100) if total_limit else 0, 2),
        }


def load_budget_config(path: Path) -> BudgetConfig:
    """Load deterministic defaults followed by an optional local override."""
    from backend.control.config_layers import load_yaml_layers

    return BudgetConfig.model_validate(load_yaml_layers(path))
