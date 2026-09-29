import json
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from backend.agents.day_providers import DayArchitect, DayEvaluator, MockDayArchitect, MockSemanticEvaluator
from backend.control.tasks import ConfiguredTask, TaskRegistry
from backend.control.model_router import ModelRouter, accumulate_profile_usage
from backend.models.day import DayExecutionMode, DayPlan, DayPlanRegistry, DayRunSnapshot, DayRunState, EscalationCategory, HumanReviewItem, QueueTaskState, QueuedTask
from backend.models.model_routing import FailureType, ProviderExecutionConfig, RoutingDecision, RoutingPolicy, RoutingRequest, RoutingRole, TaskComplexity
from backend.models.orchestration import ProviderBudget
from backend.models.result import TokenUsage


class DayTaskExecutor(Protocol):
    def __call__(self, task_id: str, max_codex_attempts: int | None = None, repair_instruction: str | None = None, codex_routing_selector: Callable[[int, int], RoutingDecision | None] | None = None) -> dict[str, Any]: ...


class DayRunner:
    """Persistent scheduler; it delegates every build action to the guarded task engine."""

    def __init__(self, plans: DayPlanRegistry, tasks: TaskRegistry, execute_task: DayTaskExecutor, *, architects: dict[str, DayArchitect] | None = None, evaluators: dict[str, DayEvaluator] | None = None, provider_budgets: dict[str, ProviderBudget] | None = None, model_router: ModelRouter | None = None, persist: Callable[[dict[str, Any]], None] | None = None, audit: Callable[[str, str, dict[str, Any]], None] | None = None, saved: dict[str, Any] | None = None) -> None:
        self.plans, self.tasks, self.execute_task = plans, tasks, execute_task
        self.architects = architects or {"mock": MockDayArchitect()}
        self.evaluators = evaluators or {"mock": MockSemanticEvaluator()}
        self.provider_budgets = provider_budgets or {"architect": ProviderBudget(), "evaluator": ProviderBudget()}
        self.model_router = model_router
        self.persist, self.audit = persist, audit
        self.snapshot = DayRunSnapshot.model_validate(saved or {})
        self._ensure_profile_usage()
        if self.snapshot.state == DayRunState.RUNNING:
            self.snapshot.state, self.snapshot.stop_reason = DayRunState.PAUSED, "INTERRUPTED_REQUIRES_RESUME"
            self._save()

    def view(self) -> dict[str, Any]:
        value = self.snapshot.model_dump(mode="json")
        current = next(
            (item for item in self.snapshot.queue if item.state == QueueTaskState.RUNNING),
            next((item for item in self.snapshot.queue if item.state in {QueueTaskState.PENDING, QueueTaskState.READY, QueueTaskState.REPAIR_PENDING}), None),
        )
        value["current_task"] = current.model_dump(mode="json") if current else None
        value["current_routing"] = self.snapshot.model_routing_decisions[-1].model_dump(mode="json") if self.snapshot.model_routing_decisions else None
        value["token_totals"] = {
            "architect": self.snapshot.token_usage["architect"].total_tokens,
            "codex": self.snapshot.token_usage["codex"].total_tokens,
            "evaluator": self.snapshot.token_usage["evaluator"].total_tokens,
            "day_total": sum(usage.total_tokens for usage in self.snapshot.token_usage.values()),
        }
        value["profile_token_totals"] = {
            profile_id: usage.gross_input_tokens + usage.output_tokens
            for profile_id, usage in self.snapshot.profile_token_usage.items()
        }
        return value

    def start(self, plan_id: str, *, mode: DayExecutionMode = DayExecutionMode.SINGLE_STEP) -> dict[str, Any]:
        plan = self.plans.get(plan_id)
        if plan is None:
            return {"error_code": "PLAN_NOT_CONFIGURED", **self.view()}
        if mode == DayExecutionMode.CONTINUOUS and not plan.continuous_mode_supported:
            return {"error_code": "CONTINUOUS_MODE_NOT_SUPPORTED", **self.view()}
        if self.snapshot.state in {DayRunState.RUNNING, DayRunState.PAUSED, DayRunState.HUMAN_REVIEW}:
            return {"error_code": "ACTIVE_DAY_REQUIRES_RESUME_OR_REVIEW", **self.view()}
        missing = [task_id for task_id in plan.task_ids if self.tasks.get(task_id) is None]
        if missing:
            return {"error_code": "PLAN_TASK_NOT_CONFIGURED", "missing_task_ids": missing, **self.view()}
        self.snapshot = DayRunSnapshot(plan_id=plan_id, state=DayRunState.RUNNING, mode=mode, queue=[QueuedTask(task_id=task_id) for task_id in plan.task_ids])
        self._ensure_profile_usage()
        self._audit("SYSTEM", "DAY_PLAN_STARTED", {"plan_id": plan_id, "mode": mode.value, "task_ids": plan.task_ids})
        self._save()
        return self._advance(plan)

    def resume(self, *, mode: DayExecutionMode | None = None) -> dict[str, Any]:
        plan = self._plan()
        if plan is None:
            return {"error_code": "NO_DAY_PLAN", **self.view()}
        selected_mode = mode or self.snapshot.mode
        if selected_mode == DayExecutionMode.CONTINUOUS and not plan.continuous_mode_supported:
            return {"error_code": "CONTINUOUS_MODE_NOT_SUPPORTED", **self.view()}
        if self.snapshot.state not in {DayRunState.PAUSED, DayRunState.STOPPED}:
            return {"error_code": "DAY_NOT_RESUMABLE", **self.view()}
        self.snapshot.state, self.snapshot.stop_reason, self.snapshot.mode, self.snapshot.tasks_processed_this_run = DayRunState.RUNNING, None, selected_mode, 0
        self._save()
        return self._advance(plan)

    def stop(self) -> dict[str, Any]:
        if self.snapshot.state in {DayRunState.RUNNING, DayRunState.PAUSED}:
            self._stop("STOP_REQUESTED")
        return self.view()

    def _advance(self, plan: DayPlan) -> dict[str, Any]:
        while self.snapshot.state == DayRunState.RUNNING:
            if self.snapshot.tasks_processed_this_run >= plan.max_tasks_per_run:
                self._stop("MAX_TASKS_PER_RUN_REACHED")
                break
            self._process_one(plan)
            if self.snapshot.mode == DayExecutionMode.SINGLE_STEP and self.snapshot.state == DayRunState.RUNNING:
                if self._all_successful():
                    self._complete()
                else:
                    self.snapshot.state, self.snapshot.stop_reason = DayRunState.PAUSED, "SINGLE_STEP_COMPLETE"
                    self._save()
                break
        return self.view()

    def _process_one(self, plan: DayPlan) -> None:
        if self.snapshot.architect_calls >= plan.max_architect_calls:
            self._stop("MAX_ARCHITECT_CALLS_REACHED")
            return
        if not self._provider_precheck("architect", self.snapshot.architect_calls):
            return
        architect_context = self._architect_request(plan)
        routing = self._select_profile(
            plan, RoutingRole.ARCHITECT, task_type="day_planning", complexity=TaskComplexity.NORMAL,
            previous_attempt_count=0, previous_failure_type=None,
            context_size=len(json.dumps(architect_context, ensure_ascii=False, separators=(",", ":"))),
            execution_provider=plan.architect_provider,
        )
        if routing is None:
            return
        architect = self.architects.get(plan.architect_provider)
        if architect is None:
            self._human_review("SYSTEM", "ARCHITECT_PROVIDER_NOT_CONFIGURED", routing=routing, failure_type="provider_configuration")
            return
        self.snapshot.architect_calls += 1
        try:
            decision = architect.choose(architect_context, ProviderExecutionConfig.from_routing_decision(routing))
        except RuntimeError as exc:
            code = _provider_error_code(exc)
            category = _provider_escalation(code)
            if category == EscalationCategory.RETRYABLE and self.snapshot.auto_provider_retries < plan.max_auto_provider_retries:
                self.snapshot.auto_provider_retries += 1
                self._escalation("SYSTEM", category, code, "RETRY_ARCHITECT")
                self._save()
                return
            self._human_review(
                "SYSTEM", f"ARCHITECT_PROVIDER_ERROR:{code}",
                result={"human_review_reason": _provider_error_message(exc)}, routing=routing,
                failure_type="provider_error", provider_diagnostics=_provider_error_diagnostics(exc), escalation_category=category,
            )
            return
        self._add_usage("architect", decision.token_usage)
        self._add_profile_usage(routing, decision.token_usage, decision.diagnostics)
        self._provider_overage("architect")
        self._audit("SYSTEM", "DAY_ARCHITECT_DECISION", {"decision": decision.decision, "task_id": decision.task_id, "reason": decision.reason, "task_complexity": decision.task_complexity, "diagnostics": decision.diagnostics, "routing": routing.model_dump(mode="json")})
        if decision.decision == "STOP_DAY":
            self._stop("ARCHITECT_STOP_DAY")
            return
        if decision.decision == "DAY_COMPLETE":
            if self._all_successful():
                self._complete()
            else:
                self._request_replan(plan, "ARCHITECT_PREMATURE_DAY_COMPLETE", routing)
            return
        item = self._eligible_item(decision.task_id)
        if item is None:
            self._request_replan(plan, "ARCHITECT_INVALID_TASK", routing)
            return
        if decision.decision == "HUMAN_REVIEW":
            self._human_review(item.task_id, decision.reason, routing=routing, failure_type="architect_decision")
            return
        if decision.decision == "SKIP_TASK":
            item.state, item.final_result = QueueTaskState.SKIPPED, "SKIPPED"
            self.snapshot.tasks_processed_this_run += 1
            self._update_progress()
            self._audit(item.task_id, "DAY_TASK_RESULT", {"final_result": "SKIPPED", "reason": decision.reason})
            self._after_task(plan)
            return
        complexity = TaskComplexity(decision.task_complexity or "normal")
        self._execute_one(plan, item, task_complexity=complexity)

    def _execute_one(self, plan: DayPlan, item: QueuedTask, repair_instruction: str | None = None, task_complexity: TaskComplexity = TaskComplexity.NORMAL, previous_failure_type: FailureType | None = None) -> None:
        task = self.tasks.get(item.task_id)
        assert task is not None
        remaining_codex = plan.max_codex_calls - self.snapshot.codex_calls
        if task.requires_codex and remaining_codex <= 0:
            self._stop("MAX_CODEX_CALLS_REACHED")
            return
        codex_routes: list[RoutingDecision] = []

        def select_codex_profile(context_size: int, retry_number: int) -> RoutingDecision | None:
            routing = self._select_profile(
                plan, RoutingRole.CODEX, task_type=task.task_type.value, complexity=task_complexity,
                previous_attempt_count=max(item.attempts - 1, 0) + retry_number,
                previous_failure_type=previous_failure_type, context_size=context_size,
                execution_provider="codex",
            )
            if routing is not None:
                codex_routes.append(routing)
            return routing

        item.state, item.attempts, item.updated_at = QueueTaskState.RUNNING, item.attempts + 1, datetime.now(timezone.utc)
        self._save()
        result = self.execute_task(
            item.task_id, max_codex_attempts=remaining_codex, repair_instruction=repair_instruction,
            codex_routing_selector=select_codex_profile,
        )
        self.snapshot.codex_calls += len(result.get("codex_attempts", []))
        usage = TokenUsage(input_tokens=int(result.get("gross_input_tokens", 0)), cached_input_tokens=int(result.get("cached_input_tokens", 0)), output_tokens=int(result.get("output_tokens", 0)), available=bool(result.get("codex_invoked")))
        self._add_usage("codex", usage)
        if result.get("codex_invoked"):
            if not codex_routes:
                self._human_review(item.task_id, "CODEX_ROUTING_DECISION_MISSING", result=result, failure_type="routing_contract")
                return
            self._add_profile_usage(codex_routes[-1], usage, result.get("diagnostics", {}))
        else:
            self._record_deterministic_zero_usage(item.task_id)
        task_audit: dict[str, Any] = {"final_result": result.get("final_result"), "codex_invoked": result.get("codex_invoked", False), "error_code": result.get("error_code"), "result_reference": result.get("run_id")}
        if codex_routes:
            task_audit["routing"] = codex_routes[-1].model_dump(mode="json")
        self._audit(item.task_id, "DAY_TASK_RESULT", task_audit)
        final = result.get("final_result")
        if final == "COMPLETE_NO_CHANGE":
            item.state, item.final_result = QueueTaskState.COMPLETE_NO_CHANGE, final
        elif final == "COMPLETE" and task.independent_evaluator_required:
            self._evaluate_semantic(plan, item, task, result)
            return
        elif final == "COMPLETE":
            item.state, item.final_result = QueueTaskState.PASS, final
        elif final == "HUMAN_REVIEW":
            self._human_review(item.task_id, result.get("human_review_reason") or result.get("error_code") or "TASK_REQUIRES_REVIEW", result=result, routing=codex_routes[-1] if codex_routes else None, failure_type=result.get("error_code") or "task_execution")
            return
        else:
            item.state, item.final_result, item.last_error = QueueTaskState.FAILED, final or "FAILED", result.get("error_code")
            self.snapshot.failed_tasks += 1
        self.snapshot.tasks_processed_this_run += 1
        self._update_progress()
        self._after_task(plan)

    def _evaluate_semantic(self, plan: DayPlan, item: QueuedTask, task: ConfiguredTask, result: dict[str, Any]) -> None:
        if result.get("postcheck_result") == "FAIL" or result.get("scope_guard_result") == "FAIL":
            self._human_review(item.task_id, "DETERMINISTIC_SAFETY_FAILURE", result=result, failure_type="deterministic_safety")
            return
        if self.snapshot.evaluator_calls >= plan.max_evaluator_calls:
            self._stop("MAX_EVALUATOR_CALLS_REACHED")
            return
        if not self._provider_precheck("evaluator", self.snapshot.evaluator_calls):
            return
        routing = self._select_profile(
            plan, RoutingRole.EVALUATOR, task_type=task.task_type.value,
            complexity=TaskComplexity.NORMAL, previous_attempt_count=item.repair_loops,
            previous_failure_type=None, context_size=len(str(self._bounded_result(result))),
            execution_provider=plan.evaluator_provider,
        )
        if routing is None:
            return
        evaluator = self.evaluators.get(plan.evaluator_provider)
        if evaluator is None:
            self._human_review(item.task_id, "EVALUATOR_PROVIDER_NOT_CONFIGURED", result=result, routing=routing, failure_type="provider_configuration")
            return
        self.snapshot.evaluator_calls += 1
        item.evaluator_invoked = True
        try:
            evaluation = evaluator.evaluate(self._evaluator_request(task, item, result, routing), ProviderExecutionConfig.from_routing_decision(routing))
        except RuntimeError as exc:
            self._human_review(
                item.task_id, f"EVALUATOR_PROVIDER_ERROR:{_provider_error_code(exc)}",
                result={**result, "human_review_reason": _provider_error_message(exc)}, routing=routing,
                failure_type="provider_error", provider_diagnostics=_provider_error_diagnostics(exc),
            )
            return
        self._add_usage("evaluator", evaluation.token_usage)
        self._add_profile_usage(routing, evaluation.token_usage, evaluation.diagnostics)
        self._provider_overage("evaluator")
        self._audit(item.task_id, "DAY_EVALUATION_RESULT", {"decision": evaluation.decision, "metrics": evaluation.metrics, "blocking_issues": evaluation.blocking_issues, "diagnostics": evaluation.diagnostics, "routing": routing.model_dump(mode="json")})
        if evaluation.decision in {"PASS", "NOT_REQUIRED"}:
            item.state, item.final_result = QueueTaskState.PASS, "COMPLETE"
        elif evaluation.decision == "REPAIR" and item.repair_loops < plan.max_repair_loops_per_task:
            item.repair_loops += 1
            item.state = QueueTaskState.REPAIR_PENDING
            self._save()
            self._execute_one(plan, item, repair_instruction=evaluation.repair_instruction, previous_failure_type=FailureType.REASONING)
            return
        else:
            self._human_review(item.task_id, evaluation.reason or "SEMANTIC_EVALUATION_REQUIRES_REVIEW", result=result, routing=routing, failure_type="semantic_evaluation")
            return
        self.snapshot.tasks_processed_this_run += 1
        self._update_progress()
        self._after_task(plan)

    def _after_task(self, plan: DayPlan) -> None:
        if self.snapshot.failed_tasks > plan.max_failed_tasks:
            self._stop("MAX_FAILED_TASKS_REACHED")
        elif self._all_successful():
            self._complete()
        elif self.snapshot.mode == DayExecutionMode.CONTINUOUS:
            self._save()

    def _provider_precheck(self, role: str, call_count: int) -> bool:
        budget = self.provider_budgets[role]
        usage = self.snapshot.token_usage[role]
        if call_count >= budget.max_calls or usage.input_tokens >= budget.daily_input_tokens or usage.output_tokens >= budget.daily_output_tokens:
            self._stop(f"{role.upper()}_BUDGET_GUARD")
            return False
        return True

    def _provider_overage(self, role: str) -> None:
        budget, usage = self.provider_budgets[role], self.snapshot.token_usage[role]
        if usage.input_tokens > budget.daily_input_tokens or usage.output_tokens > budget.daily_output_tokens:
            warning = f"{role.upper()}_POST_RUN_BUDGET_OVERAGE"
            if warning not in self.snapshot.budget_warnings:
                self.snapshot.budget_warnings.append(warning)

    def _select_profile(
        self,
        plan: DayPlan,
        role: RoutingRole,
        *,
        task_type: str,
        complexity: TaskComplexity,
        previous_attempt_count: int,
        previous_failure_type: FailureType | None,
        context_size: int,
        execution_provider: str,
    ) -> RoutingDecision | None:
        if self.model_router is None:
            # Direct unit-test construction may omit the control component; the
            # production engine always injects it. This compatibility path does
            # not select a profile or grant any additional authority.
            return RoutingDecision(
                outcome="SELECTED", role=role, selection_reason="router not configured for direct test runner",
                provider=execution_provider, context_size=context_size,
            )
        role_budget = self.provider_budgets.get(role.value)
        role_usage = self.snapshot.token_usage.get(role.value, TokenUsage())
        day_input = sum(usage.input_tokens for usage in self.snapshot.token_usage.values())
        day_output = sum(usage.output_tokens for usage in self.snapshot.token_usage.values())
        remaining_role_input = max((role_budget.daily_input_tokens if role_budget else 0) - role_usage.input_tokens, 0)
        remaining_role_output = max((role_budget.daily_output_tokens if role_budget else 0) - role_usage.output_tokens, 0)
        # The Day budget is the sum of configured role budgets; the router cannot raise it.
        day_input_budget = sum(budget.daily_input_tokens for budget in self.provider_budgets.values())
        day_output_budget = sum(budget.daily_output_tokens for budget in self.provider_budgets.values())
        decision = self.model_router.select(RoutingRequest(
            role=role, execution_provider=execution_provider, task_type=task_type, task_complexity=complexity,
            previous_attempt_count=previous_attempt_count, previous_failure_type=previous_failure_type,
            context_size=context_size, remaining_role_input_tokens=remaining_role_input,
            remaining_role_output_tokens=remaining_role_output,
            remaining_day_input_tokens=max(day_input_budget - day_input, 0),
            remaining_day_output_tokens=max(day_output_budget - day_output, 0),
            plan_policy=RoutingPolicy(
                allowed_profile_ids=plan.allowed_profile_ids,
                allow_budget_downgrade=plan.allow_profile_budget_downgrade,
                max_escalation_level=plan.max_profile_escalation_level,
            ),
        ))
        self.snapshot.model_routing_decisions.append(decision)
        self._audit("SYSTEM", "DAY_MODEL_ROUTING", decision.model_dump(mode="json"))
        if decision.outcome != "SELECTED":
            self._human_review("SYSTEM", f"MODEL_ROUTER:{decision.selection_reason}", routing=decision, failure_type="model_routing")
            return None
        self._save()
        return decision

    @staticmethod
    def _empty_profile_usage():
        from backend.models.model_routing import ProfileTokenUsage
        return ProfileTokenUsage()

    def _ensure_profile_usage(self) -> None:
        if self.model_router:
            for profile_id in self.model_router.profiles.model_profiles:
                self.snapshot.profile_token_usage.setdefault(profile_id, self._empty_profile_usage())

    def _add_profile_usage(self, routing: RoutingDecision, usage: TokenUsage, diagnostics: dict[str, Any] | None) -> None:
        if not routing.profile_id:
            return
        current = self.snapshot.profile_token_usage.setdefault(routing.profile_id, self._empty_profile_usage())
        duration_ms = float((diagnostics or {}).get("duration_ms", 0) or 0)
        self.snapshot.profile_token_usage[routing.profile_id] = accumulate_profile_usage(current, usage, duration_ms)

    def _record_deterministic_zero_usage(self, task_id: str) -> None:
        if task_id not in self.snapshot.deterministic_zero_usage_task_ids:
            self.snapshot.deterministic_zero_usage_task_ids.append(task_id)
            self._audit(task_id, "DAY_DETERMINISTIC_NO_AI", {"token_usage": TokenUsage().model_dump(), "reason": "PRECHECK_PASS"})

    def _architect_request(self, plan: DayPlan) -> dict[str, Any]:
        eligible = [item for item in self.snapshot.queue if item.state in {QueueTaskState.PENDING, QueueTaskState.READY, QueueTaskState.REPAIR_PENDING}]
        terminal = {QueueTaskState.PASS, QueueTaskState.COMPLETE_NO_CHANGE, QueueTaskState.SKIPPED}
        return {
            "plan": plan.plan_id,
            "validation_day": plan.validation_day,
            "eligible_tasks": [
                {"task_id": item.task_id, "title": self.tasks.get(item.task_id).title, "task_type": self.tasks.get(item.task_id).task_type.value}
                for item in eligible
            ],
            "queue": [{"id": item.task_id, "state": item.state.value} for item in self.snapshot.queue],
            "prior_results": [
                {"id": item.task_id, "result": item.final_result}
                for item in self.snapshot.queue if item.state in terminal
            ],
            "remaining_calls": {
                "architect": plan.max_architect_calls - self.snapshot.architect_calls,
                "builder": plan.max_codex_calls - self.snapshot.codex_calls,
                "evaluator": plan.max_evaluator_calls - self.snapshot.evaluator_calls,
            },
            "stop_limits": {"tasks": plan.max_tasks_per_run, "failed": plan.max_failed_tasks},
            "progress": {"day": self.snapshot.day_progress, "overall": self.snapshot.overall_progress},
        }

    def _evaluator_request(self, task: ConfiguredTask, item: QueuedTask, result: dict[str, Any], routing: RoutingDecision) -> dict[str, Any]:
        return {"task_id": task.task_id, "task_goal": task.title, "acceptance_criteria": " ".join(task.postcheck.argv), "precheck": result.get("precheck_result"), "postcheck": result.get("postcheck_result"), "scope_guard": result.get("scope_guard_result"), "changed_files": result.get("changed_files", []), "evidence": self._bounded_result(result), "rubric": {"metrics": task.evaluation_metrics}, "repair_attempt": item.repair_loops, "routing": routing.model_dump(mode="json")}

    @staticmethod
    def _bounded_result(result: dict[str, Any]) -> dict[str, Any]:
        return {key: result.get(key) for key in ("task_id", "final_result", "precheck_result", "postcheck_result", "scope_guard_result", "changed_files", "error_code")}

    def _eligible_item(self, task_id: str | None) -> QueuedTask | None:
        return next((item for item in self.snapshot.queue if item.task_id == task_id and item.state in {QueueTaskState.PENDING, QueueTaskState.READY, QueueTaskState.REPAIR_PENDING}), None)

    def _all_successful(self) -> bool:
        return bool(self.snapshot.queue) and all(item.state in {QueueTaskState.PASS, QueueTaskState.COMPLETE_NO_CHANGE, QueueTaskState.SKIPPED} for item in self.snapshot.queue)

    def _update_progress(self) -> None:
        total = len(self.snapshot.queue)
        complete = sum(item.state in {QueueTaskState.PASS, QueueTaskState.COMPLETE_NO_CHANGE, QueueTaskState.SKIPPED} for item in self.snapshot.queue)
        progress = round(complete / total * 100, 2) if total else 0
        self.snapshot.day_progress = progress
        self.snapshot.overall_progress = progress
        self._save()

    def _complete(self) -> None:
        self.snapshot.state, self.snapshot.stop_reason = DayRunState.COMPLETE, "ALL_TASKS_TERMINAL"
        self._update_progress()
        self._audit("SYSTEM", "DAY_COMPLETE", {"plan_id": self.snapshot.plan_id, "day_progress": self.snapshot.day_progress})
        self._save()

    def _stop(self, reason: str) -> None:
        self.snapshot.state, self.snapshot.stop_reason = DayRunState.STOPPED, reason
        self._audit("SYSTEM", "DAY_STOPPED", {"reason": reason})
        self._save()

    def _human_review(
        self,
        task_id: str,
        reason: str,
        *,
        result: dict[str, Any] | None = None,
        routing: RoutingDecision | None = None,
        failure_type: str | None = None,
        provider_diagnostics: dict[str, object] | None = None,
        escalation_category: EscalationCategory = EscalationCategory.HUMAN_DECISION_REQUIRED,
    ) -> None:
        item = next((candidate for candidate in self.snapshot.queue if candidate.task_id == task_id), None)
        if item:
            item.state, item.last_error = QueueTaskState.HUMAN_REVIEW, reason
        result = result or {}
        review = HumanReviewItem(
            plan_id=self.snapshot.plan_id, task_id=task_id, reason=reason,
            summary=result.get("human_review_reason"), changed_files=result.get("changed_files", []),
            result_reference=result.get("run_id"), routing_profile_id=routing.profile_id if routing else None,
            failure_type=failure_type, provider_diagnostics=provider_diagnostics or {},
            escalation_category=escalation_category,
        )
        self.snapshot.human_review_queue.append(review)
        self.snapshot.state, self.snapshot.stop_reason = DayRunState.HUMAN_REVIEW, reason
        self._audit(task_id, "DAY_HUMAN_REVIEW", review.model_dump(mode="json"))
        self._save()

    def _request_replan(self, plan: DayPlan, reason: str, routing: RoutingDecision) -> None:
        if self.snapshot.auto_replans < plan.max_auto_replans:
            self.snapshot.auto_replans += 1
            self._escalation("SYSTEM", EscalationCategory.REPLAN_REQUIRED, reason, "REPLAN_ARCHITECT")
            self._save()
            return
        self._human_review("SYSTEM", reason, routing=routing, failure_type="architect_decision")

    def _escalation(self, task_id: str, category: EscalationCategory, reason: str, action: str) -> None:
        event = {"category": category.value, "reason": reason, "action": action}
        self.snapshot.escalation_events.append(event)
        self._audit(task_id, "DAY_ESCALATION", event)

    def _plan(self) -> DayPlan | None:
        return self.plans.get(self.snapshot.plan_id) if self.snapshot.plan_id else None

    def _add_usage(self, role: str, usage: TokenUsage) -> None:
        previous = self.snapshot.token_usage[role]
        self.snapshot.token_usage[role] = TokenUsage(input_tokens=previous.input_tokens + usage.input_tokens, cached_input_tokens=previous.cached_input_tokens + usage.cached_input_tokens, output_tokens=previous.output_tokens + usage.output_tokens, available=previous.available or usage.available)

    def _save(self) -> None:
        self.snapshot.updated_at = datetime.now(timezone.utc)
        if self.persist:
            self.persist(self.view())

    def _audit(self, task_id: str, event_type: str, details: dict[str, Any]) -> None:
        if self.audit:
            self.audit(task_id, event_type, details)


def _provider_error_code(exc: RuntimeError) -> str:
    value = getattr(exc, "code", None)
    return value if isinstance(value, str) and value else type(exc).__name__


def _provider_error_message(exc: RuntimeError) -> str:
    value = getattr(exc, "safe_message", None)
    return value if isinstance(value, str) and value else _provider_error_code(exc)


def _provider_error_diagnostics(exc: RuntimeError) -> dict[str, object]:
    value = getattr(exc, "diagnostics", None)
    return value if isinstance(value, dict) else {}


def _provider_escalation(code: str) -> EscalationCategory:
    if code in {"CODEX_TIMEOUT", "CODEX_TURN_FAILED", "CODEX_ROLE_FAILED", "CODEX_OUTPUT_INVALID", "OPENAI_TIMEOUT"}:
        return EscalationCategory.RETRYABLE
    if code in {"CODEX_NOT_FOUND", "CODEX_HOME_NOT_FOUND", "CODEX_SQLITE_HOME_NOT_FOUND", "OPENAI_CREDENTIALS_MISSING"}:
        return EscalationCategory.EXTERNAL_ACTION_REQUIRED
    return EscalationCategory.HUMAN_DECISION_REQUIRED
