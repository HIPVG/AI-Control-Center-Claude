from pathlib import Path

import pytest

from backend.models.runtime import CodexMode, load_runtime_config
from backend.control.git_guard import GitGuard
from backend.control.scope_guard import ScopeGuard
from backend.control.token_budget import BudgetDecision, BudgetConfig, CodexBudget, TokenBudgetManager, load_budget_config
from backend.models.result import TokenUsage
from backend.models.task import TaskType, WorkOrder


def work_order():
    return WorkOrder(task_id="T-1", goal="Fix one file", task_type=TaskType.CODE_FIX, allowed_files=["src/a.py"], acceptance_tests=["pytest tests/test_a.py"], needs_codex=True)


def test_task_token_budget_is_rejected():
    manager = TokenBudgetManager(BudgetConfig(codex=CodexBudget(task_input_tokens=10, task_output_tokens=10)))
    assert manager.check(TokenUsage(input_tokens=11), retry_count=0) == BudgetDecision.TASK_BUDGET_EXCEEDED


def test_retry_limit_is_rejected():
    manager = TokenBudgetManager(BudgetConfig(codex=CodexBudget(max_retry=2)))
    assert manager.check(TokenUsage(), retry_count=2) == BudgetDecision.RETRY_LIMIT_EXCEEDED


def test_scope_guard_accepts_allowed_file():
    assert ScopeGuard().check(work_order(), ["src/a.py"]).allowed


def test_scope_guard_detects_out_of_scope_file():
    check = ScopeGuard().check(work_order(), ["src/a.py", "src/b.py"])
    assert not check.allowed
    assert check.out_of_scope == ["src/b.py"]


def test_git_guard_blocks_destructive_command():
    assert not GitGuard().check("git reset --hard").allowed


def test_runtime_and_budget_local_layers_override_tracked_defaults(tmp_path: Path):
    runtime = tmp_path / "runtime.yaml"
    runtime.write_text("codex:\n  mode: mock\n  timeout_seconds: 300\n", encoding="utf-8")
    runtime.with_name("runtime.local.yaml").write_text("codex:\n  mode: real\n", encoding="utf-8")
    budget = tmp_path / "budget.yaml"
    budget.write_text("codex:\n  daily_input_tokens: 10\n  task_input_tokens: 4\n", encoding="utf-8")
    budget.with_name("budget.local.yaml").write_text("codex:\n  daily_input_tokens: 12\nevaluator:\n  daily_output_tokens: 7\n", encoding="utf-8")
    assert load_runtime_config(runtime).codex.mode == CodexMode.REAL
    loaded_budget = load_budget_config(budget)
    assert loaded_budget.codex.daily_input_tokens == 12
    assert loaded_budget.codex.task_input_tokens == 4
    assert loaded_budget.evaluator.daily_output_tokens == 7


def test_invalid_local_configuration_fails_closed(tmp_path: Path):
    runtime = tmp_path / "runtime.yaml"
    runtime.with_name("runtime.local.yaml").write_text("- unsafe-list-root\n", encoding="utf-8")
    with pytest.raises(ValueError, match="configuration root"):
        load_runtime_config(runtime)
