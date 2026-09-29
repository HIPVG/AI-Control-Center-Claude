"""Isolated, typed Architect and Evaluator providers for Day orchestration."""

import json
import os
import re
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Protocol

from pydantic import BaseModel

from backend.models.day import (
    ArchitectDecision, ArchitectProviderOutput, CodexReview, EvaluatorProviderOutput,
    ReviewerProviderOutput, SemanticEvaluation,
)
from backend.models.model_routing import ProviderExecutionConfig
from backend.models.orchestration import ProviderSettings
from backend.models.result import TokenUsage
from backend.runners.codex import RealCodexRunner, StructuredCodexResult
from backend.models.local_llm_day import DynamicDayWorkOrder, LocalLLMDayContract, LocalLLMDayWorkItem


class ProviderConfigurationError(RuntimeError):
    pass


class ProviderRequestError(RuntimeError):
    """A sanitized provider failure safe to persist in Human Review."""

    def __init__(self, code: str, message: str, *, diagnostics: dict[str, object] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.safe_message = message
        self.diagnostics = diagnostics or {}


class ProviderTimeoutError(ProviderRequestError):
    pass


class DayArchitect(Protocol):
    def choose(self, request: dict[str, Any], execution: ProviderExecutionConfig) -> ArchitectDecision: ...


class DayEvaluator(Protocol):
    def evaluate(self, request: dict[str, Any], execution: ProviderExecutionConfig) -> SemanticEvaluation: ...


class DayContractPlanner(Protocol):
    """Creates bounded Day work items; it cannot alter the Day contract."""

    def plan(self, contract: LocalLLMDayContract, inventory: dict[str, object]) -> list[LocalLLMDayWorkItem]: ...


class MockDayContractPlanner:
    """Safe no-provider fallback used when Codex is configured in mock mode."""

    def plan_research(self, request):
        candidates = request.get("candidates", [])
        if len(candidates) != 1:
            raise ValueError("RESEARCH_CONDITION_CHOICE_REQUIRED" if candidates else "RESEARCH_CONDITION_NOT_APPROVED")
        return candidates[0]

    def plan(self, contract: LocalLLMDayContract, _inventory: dict[str, object]) -> list[LocalLLMDayWorkItem]:
        return [
            LocalLLMDayWorkItem(
                item_id=f"evidence-{contract.day}-{criterion_id}", title="Collect trusted completion evidence",
                objective=next(item.statement for item in contract.completion_criteria if item.criterion_id == criterion_id),
                kind="EVIDENCE_CHECK", criterion_ids=[criterion_id],
            )
            for criterion_id in contract.remaining_gaps[:3]
        ]


class CodexDayContractPlanner:
    """Read-only planner that returns data-only work items for a Day Contract."""

    def __init__(self, runner: RealCodexRunner, workspace_root: Path, trusted_task_ids: set[str]) -> None:
        self.runner, self.workspace_root = runner, workspace_root.resolve()
        self.trusted_task_ids = trusted_task_ids

    def _workspace(self, role: str) -> Path:
        workspace = (self.workspace_root / role).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def plan_research(self, request):
        from backend.control.day_research import ResearchExecutionPlan
        workspace = self._workspace("research-execution-planner")
        schema_path = workspace / "output-schema.json"
        schema_path.write_text(json.dumps(strict_provider_schema(ResearchExecutionPlan)), encoding="utf-8")
        prompt = ("You are the read-only Research Architect. Construct a bounded execution plan from the supplied existing repository context. "
                  "Do not run commands, change files, invent conditions, models, inputs, evidence or acceptance rules. "
                  "Preserve the Day objective, fixed configuration, holdout freeze and authority. Return a data-only plan; Python independently validates it. "
                  "Do not choose between materially different research conditions. Use only supplied file content and references.\n"
                  + json.dumps(request, ensure_ascii=False))
        result = self.runner.run_readonly_structured(workspace, prompt, schema_path)
        if result.status != "completed" or not result.output_text:
            raise _codex_role_error("RESEARCH_EXECUTION_PLANNER", result)
        return ResearchExecutionPlan.model_validate_json(result.output_text)

    def plan(self, contract: LocalLLMDayContract, inventory: dict[str, object]) -> list[LocalLLMDayWorkItem]:
        schema = {
            "type": "object", "additionalProperties": False, "required": ["tasks"],
            "properties": {"tasks": {"type": "array", "minItems": 1, "maxItems": 3, "items": {
                "type": "object", "additionalProperties": False,
                "required": ["item_id", "title", "objective", "kind", "criterion_ids", "engine_task_id", "dynamic_work_order"],
                "properties": {
                    "item_id": {"type": "string"}, "title": {"type": "string"}, "objective": {"type": "string"},
                    "kind": {"type": "string", "enum": ["EVIDENCE_CHECK", "ENGINE_WORK_ORDER", "DYNAMIC_ENGINEERING_WORK"]},
                    "criterion_ids": {"type": "array", "items": {"type": "string"}},
                    "engine_task_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "dynamic_work_order": {"anyOf": [
                        {"type": "null"},
                        {"type": "object", "additionalProperties": False,
                         "required": ["task_id", "project_id", "task_type", "allowed_files", "context_files", "acceptance_test_files"],
                         "properties": {
                            "task_id": {"type": "string"}, "project_id": {"type": "string"}, "task_type": {"type": "string", "enum": ["code_fix", "config", "plan"]},
                            "allowed_files": {"type": "array", "items": {"type": "string"}},
                            "context_files": {"type": "array", "items": {"type": "string"}},
                            "acceptance_test_files": {"type": "array", "items": {"type": "string"}}
                         }}]},
                },
            }}},
        }
        workspace = self._workspace("day-contract-planner")
        schema_path = workspace / "output-schema.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        request = {
            "contract": contract.model_dump(mode="json"), "inventory": inventory,
            "trusted_engine_task_ids": sorted(self.trusted_task_ids),
            "dynamic_work_order_policy": {
                "project_id": "local_llm_lab", "task_id_format": "day-N-lowercase-slug",
                "safe_paths_only": True, "acceptance_tests_must_be": "tests/*.py",
                "forbidden_path_prefixes": ["results/", "artifacts/", "models/", "datasets/", ".env"],
            },
        }
        prompt = (
            "You are the read-only Codex Architect for one governed Day Contract. Return JSON only. "
            "Apply the role-specific boundaries of Working Rules WR-01, WR-04, WR-05, WR-07, WR-10, and WR-13. "
            "Create at most three tasks for the remaining criteria. Inspect only the bounded tracked_context_files and source-presence inventory supplied by the server; do not run commands or modify files. "
            "Use ENGINE_WORK_ORDER only with a listed trusted_engine_task_id. You may use DYNAMIC_ENGINEERING_WORK only with a fully populated policy-conforming dynamic_work_order; it is data only and the server derives commands. "
            "criterion_ids identify the objective being worked on. They do not grant evidence capability. Only server-owned deterministic evidence collectors or adapters can satisfy required_evidence. "
            "Do not create arbitrary commands, criteria, or authority.\n" + json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        )
        result = self.runner.run_readonly_structured(workspace, prompt, schema_path)
        if result.status != "completed" or not result.output_text:
            raise _codex_role_error("DAY_CONTRACT_PLANNER", result)
        try:
            output = json.loads(result.output_text)
            tasks = output["tasks"] if isinstance(output, dict) else None
            if not isinstance(tasks, list):
                raise ValueError("tasks missing")
            return [LocalLLMDayWorkItem.model_validate(item) for item in tasks]
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise ProviderRequestError("CODEX_DAY_PLAN_OUTPUT_INVALID", "Codex Architect returned an invalid Day task plan.", diagnostics={"provider_error_type": type(exc).__name__, "request_stage": "structured_output"}) from exc


class MockDayArchitect:
    def choose(self, request: dict[str, Any], execution: ProviderExecutionConfig) -> ArchitectDecision:
        eligible = request.get("eligible_tasks", [])
        task_id = eligible[0]["task_id"] if eligible else None
        return ArchitectDecision(
            decision="RUN_TASK" if task_id else "DAY_COMPLETE", task_id=task_id,
            reason="first trusted eligible queue item", diagnostics={"provider": "mock", "execution": execution.model_dump(mode="json")},
        )


class MockCodexArchitectProvider:
    """Test/runtime-mock stand-in preserving the Codex Core provider identity."""

    def choose(self, request: dict[str, Any], execution: ProviderExecutionConfig) -> ArchitectDecision:
        eligible = request.get("eligible_tasks", [])
        task_id = eligible[0]["task_id"] if eligible else None
        return ArchitectDecision(
            decision="RUN_TASK" if task_id else "DAY_COMPLETE", task_id=task_id,
            reason="first trusted eligible queue item", diagnostics={
                "provider": "codex", "execution_mode": "mock", "role": "architect",
                "execution": execution.model_dump(mode="json"),
            },
        )


class CodexArchitectProvider:
    """Read-only Codex Architect constrained to a trusted Day queue."""

    def __init__(self, runner: RealCodexRunner, workspace_root: Path) -> None:
        self.runner, self.workspace_root = runner, workspace_root.resolve()

    def choose(self, request: dict[str, Any], execution: ProviderExecutionConfig) -> ArchitectDecision:
        result = self.runner.run_readonly_structured(
            self._workspace("architect"), self._prompt(request), self._write_schema("architect", ArchitectProviderOutput),
        )
        if result.status != "completed" or not result.output_text:
            raise _codex_role_error("ARCHITECT", result)
        try:
            output = ArchitectProviderOutput.model_validate(json.loads(result.output_text))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderRequestError(
                "CODEX_ARCHITECT_OUTPUT_INVALID", "Codex Architect returned invalid structured output.",
                diagnostics={"provider_error_type": type(exc).__name__, "request_stage": "structured_output"},
            ) from exc
        _validate_architect_selection(output, request)
        return ArchitectDecision.model_validate({
            **output.model_dump(), "token_usage": result.token_usage,
            "diagnostics": self._diagnostics(result, execution, "architect"),
        })

    def _workspace(self, role: str) -> Path:
        workspace = (self.workspace_root / role).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _write_schema(self, role: str, output_type: type[BaseModel]) -> Path:
        workspace = self._workspace(role)
        schema = strict_provider_schema(output_type)
        path = workspace / "output-schema.json"
        path.write_text(json.dumps(schema, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        return path

    @staticmethod
    def _prompt(request: dict[str, Any]) -> str:
        return (
            "You are the read-only Codex Architect for a governed Day plan. "
            "Apply the role-specific boundaries of Working Rules WR-01, WR-04, WR-05, WR-10, and WR-13. "
            "Return only JSON matching the supplied schema. Select only one configured eligible task ID, "
            "or return DAY_COMPLETE/STOP_DAY when justified. Do not inspect files, run commands, modify files, "
            "change budgets, commands, acceptance criteria, retry limits, or allowed files.\n"
            "Trusted bounded orchestration context follows:\n"
            + json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        )

    @staticmethod
    def _diagnostics(result: StructuredCodexResult, execution: ProviderExecutionConfig, role: str) -> dict[str, object]:
        return {
            "provider": "codex", "role": role, "execution": execution.model_dump(mode="json"),
            "duration_ms": result.duration_ms, "outcome": result.status,
            "thread_started": bool(result.diagnostics and result.diagnostics.thread_started),
            "turn_completed": bool(result.diagnostics and result.diagnostics.turn_completed),
        }


class CodexReviewerProvider(CodexArchitectProvider):
    """Optional, read-only first reviewer. It is not an independent evaluator."""

    def review(self, request: dict[str, Any], execution: ProviderExecutionConfig) -> CodexReview:
        result = self.runner.run_readonly_structured(
            self._workspace("reviewer"), self._review_prompt(request), self._write_schema("reviewer", ReviewerProviderOutput),
        )
        if result.status != "completed" or not result.output_text:
            raise _codex_role_error("REVIEWER", result)
        try:
            output = ReviewerProviderOutput.model_validate(json.loads(result.output_text))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderRequestError("CODEX_REVIEWER_OUTPUT_INVALID", "Codex Reviewer returned invalid structured output.", diagnostics={"provider_error_type": type(exc).__name__, "request_stage": "structured_output"}) from exc
        return CodexReview.model_validate({
            **output.model_dump(), "token_usage": result.token_usage,
            "diagnostics": self._diagnostics(result, execution, "reviewer"),
        })

    @staticmethod
    def _review_prompt(request: dict[str, Any]) -> str:
        return (
            "You are a read-only Codex first reviewer. Return only JSON matching the supplied schema. "
            "Apply the role-specific boundaries of Working Rules WR-07, WR-08, WR-10, and WR-13. "
            "Inspect only the bounded evidence below. Do not run commands, modify files, or grant any authority.\n"
            + json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        )


class MockSemanticEvaluator:
    """Deterministic fake used only by tests and explicit mock plans."""

    def __init__(self, decisions: dict[str, SemanticEvaluation] | None = None) -> None:
        self.decisions = decisions or {}

    def evaluate(self, request: dict[str, Any], execution: ProviderExecutionConfig) -> SemanticEvaluation:
        task_id = str(request["task_id"])
        metrics = request.get("rubric", {}).get("metrics", [])
        result = self.decisions.get(task_id, SemanticEvaluation(decision="PASS", reason="mock semantic pass", metrics={metric: 5.0 for metric in metrics}))
        return result.model_copy(update={"diagnostics": {"provider": "mock", "execution": execution.model_dump(mode="json")}})


class _OpenAIStructuredProvider:
    """Lazy SDK client: app startup remains safe without credentials or SDK use."""

    provider_name = "openai"

    def __init__(self, settings: ProviderSettings | None = None, *, client_factory: Callable[..., Any] | None = None) -> None:
        self.settings = settings or ProviderSettings(provider="openai")
        self.client_factory = client_factory

    def _client(self, execution: ProviderExecutionConfig) -> Any:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key or not execution.model or not execution.reasoning_effort or not execution.timeout_seconds:
            raise ProviderConfigurationError("OPENAI_API_KEY and a routed OpenAI execution configuration are required")
        if execution.provider != self.provider_name:
            raise ProviderConfigurationError("routed provider does not match OpenAI provider")
        if self.client_factory:
            return self.client_factory(api_key=api_key, timeout=execution.timeout_seconds, max_retries=0)
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ProviderConfigurationError("OpenAI Python SDK is not installed") from exc
        return OpenAI(api_key=api_key, timeout=execution.timeout_seconds, max_retries=0)

    def _request(self, *, instructions: str, payload: dict[str, Any], output_type: type[BaseModel], execution: ProviderExecutionConfig) -> tuple[BaseModel, TokenUsage, dict[str, object]]:
        started = monotonic()
        client = self._client(execution)
        last_error: Exception | None = None
        schema = strict_provider_schema(output_type)
        for attempt in range(self.settings.max_transient_retries + 1):
            try:
                response = client.responses.create(
                    model=execution.model,
                    reasoning={"effort": execution.reasoning_effort},
                    max_output_tokens=execution.max_output_tokens,
                    store=False,
                    instructions=instructions,
                    input=json.dumps(payload, ensure_ascii=False),
                    text={"format": {"type": "json_schema", "name": output_type.__name__.lower(), "strict": True, "schema": schema}},
                )
                output_text = _read(response, "output_text")
                if not isinstance(output_text, str):
                    raise ProviderRequestError("OPENAI_INVALID_STRUCTURED_OUTPUT", "provider returned no structured output", diagnostics={"provider_error_type": "MissingOutput", "request_stage": "structured_output"})
                parsed = output_type.model_validate(json.loads(output_text))
                usage = _token_usage(_read(response, "usage"))
                return parsed, usage, {
                    "provider": self.provider_name, "profile_id": execution.profile_id,
                    "model": execution.model, "reasoning_effort": execution.reasoning_effort,
                    "timeout_seconds": execution.timeout_seconds, "max_output_tokens": execution.max_output_tokens,
                    "duration_ms": round((monotonic() - started) * 1000, 2), "attempts": attempt + 1,
                    "success": True, "request_stage": "responses.create",
                }
            except (json.JSONDecodeError, ValueError) as exc:
                raise ProviderRequestError(
                    "OPENAI_INVALID_STRUCTURED_OUTPUT", "provider returned invalid structured output",
                    diagnostics={"provider_error_type": type(exc).__name__, "request_stage": "structured_output"},
                ) from exc
            except ProviderRequestError:
                raise
            except Exception as exc:  # SDK exceptions are normalized at this boundary.
                last_error = exc
                if not _is_transient(exc) or attempt >= self.settings.max_transient_retries:
                    error = _sanitize_openai_error(exc, stage="responses.create")
                    if error.code == "OPENAI_TIMEOUT":
                        raise ProviderTimeoutError(error.code, error.safe_message, diagnostics=error.diagnostics) from exc
                    raise error from exc
        raise ProviderRequestError("OPENAI_REQUEST_FAILED", "provider request failed") from last_error


class OpenAIDayArchitect(_OpenAIStructuredProvider):
    def choose(self, request: dict[str, Any], execution: ProviderExecutionConfig) -> ArchitectDecision:
        output, usage, diagnostics = self._request(
            instructions="Choose only from eligible configured task IDs. Do not create tasks, commands, paths, budgets, or acceptance criteria.",
            payload=request, output_type=ArchitectProviderOutput, execution=execution,
        )
        return ArchitectDecision.model_validate({**output.model_dump(), "token_usage": usage, "diagnostics": diagnostics})


class OpenAISemanticEvaluator(_OpenAIStructuredProvider):
    def evaluate(self, request: dict[str, Any], execution: ProviderExecutionConfig) -> SemanticEvaluation:
        output, usage, diagnostics = self._request(
            instructions="Evaluate only the supplied bounded evidence and trusted rubric. Return no commands, paths, or replacement acceptance criteria.",
            payload=request, output_type=EvaluatorProviderOutput, execution=execution,
        )
        assert isinstance(output, EvaluatorProviderOutput)
        allowed_metrics = set(request.get("rubric", {}).get("metrics", []))
        metrics = {metric.name: metric.score for metric in output.metrics}
        if len(metrics) != len(output.metrics) or not set(metrics).issubset(allowed_metrics):
            raise ProviderRequestError(
                "OPENAI_INVALID_STRUCTURED_OUTPUT", "provider returned unsupported evaluation metrics",
                diagnostics={"provider_error_type": "MetricValidation", "request_stage": "structured_output"},
            )
        result = SemanticEvaluation.model_validate({
            **output.model_dump(exclude={"metrics"}), "metrics": metrics,
            "token_usage": usage, "diagnostics": diagnostics,
        })
        if result.repair_instruction and (len(result.repair_instruction) > 1000 or "\n" in result.repair_instruction or re.search(r"(?i)(\\\\|/|--|\\b(?:python|powershell|cmd|git)\\b|\\.py\\b)", result.repair_instruction)):
            raise ProviderRequestError(
                "OPENAI_INVALID_STRUCTURED_OUTPUT",
                "repair instruction exceeds bounded plain-text policy",
                diagnostics={
                    "provider_error_type": "RepairInstructionPolicy",
                    "request_stage": "structured_output",
                },
            )
        return result


def _read(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _token_usage(usage: Any) -> TokenUsage:
    details = _read(usage, "input_tokens_details") or {}
    cached = _read(details, "cached_tokens") or 0
    return TokenUsage(input_tokens=int(_read(usage, "input_tokens") or 0), cached_input_tokens=int(cached), output_tokens=int(_read(usage, "output_tokens") or 0), available=usage is not None)


def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    return any(token in name for token in ("timeout", "connection", "rate", "internalserver"))


def strict_provider_schema(output_type: type[BaseModel]) -> dict[str, Any]:
    """Return an API-safe strict Structured Outputs schema or fail before I/O."""
    schema = output_type.model_json_schema()
    validate_strict_provider_schema(schema)
    return schema


def validate_strict_provider_schema(schema: dict[str, Any]) -> None:
    """Validate the OpenAI strict-schema subset relied on by provider DTOs."""
    if schema.get("type") != "object" or "anyOf" in schema:
        raise ValueError("strict provider schema root must be an object without anyOf")
    definitions = schema.get("$defs", {})
    if not isinstance(definitions, dict):
        raise ValueError("strict provider schema definitions must be an object")
    _validate_schema_node(schema, definitions, "$")
    for name, definition in definitions.items():
        if not isinstance(definition, dict):
            raise ValueError(f"strict provider schema definition {name} must be an object")
        _validate_schema_node(definition, definitions, f"$defs.{name}")


def _validate_schema_node(node: dict[str, Any], definitions: dict[str, Any], path: str) -> None:
    supported = {"$ref", "additionalProperties", "anyOf", "description", "enum", "items", "properties", "required", "title", "type", "$defs"}
    unsupported = set(node) - supported
    if unsupported:
        raise ValueError(f"strict provider schema has unsupported keywords at {path}: {', '.join(sorted(unsupported))}")
    if "$ref" in node:
        reference = node["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/$defs/") or reference.removeprefix("#/$defs/") not in definitions:
            raise ValueError(f"strict provider schema has unsupported reference at {path}")
        return
    if node.get("type") == "object" or "properties" in node:
        properties = node.get("properties")
        if not isinstance(properties, dict) or node.get("additionalProperties") is not False:
            raise ValueError(f"strict provider schema object must forbid additional properties at {path}")
        if set(node.get("required", [])) != set(properties):
            raise ValueError(f"strict provider schema requires every property at {path}")
        for name, child in properties.items():
            if not isinstance(child, dict):
                raise ValueError(f"strict provider schema property must be a schema at {path}.{name}")
            _validate_schema_node(child, definitions, f"{path}.{name}")
    if node.get("type") == "array":
        items = node.get("items")
        if not isinstance(items, dict):
            raise ValueError(f"strict provider schema array items must be a schema at {path}")
        _validate_schema_node(items, definitions, f"{path}[]")
    if "anyOf" in node:
        variants = node["anyOf"]
        if not isinstance(variants, list) or not variants:
            raise ValueError(f"strict provider schema anyOf must be non-empty at {path}")
        for index, variant in enumerate(variants):
            if not isinstance(variant, dict):
                raise ValueError(f"strict provider schema anyOf member must be a schema at {path}[{index}]")
            _validate_schema_node(variant, definitions, f"{path}[{index}]")


def _sanitize_openai_error(exc: Exception, *, stage: str) -> ProviderRequestError:
    status = getattr(exc, "status_code", getattr(exc, "status", None))
    api_code = getattr(exc, "code", None)
    error_type = type(exc).__name__
    raw_message = (str(exc).splitlines() or [""])[0][:240]
    lowered = f"{error_type} {api_code or ''} {raw_message}".lower()
    if "timeout" in lowered:
        code, message = "OPENAI_TIMEOUT", "OpenAI request timed out"
    elif "quota" in lowered or "insufficient_quota" in lowered:
        code, message = "OPENAI_QUOTA", "OpenAI quota is unavailable"
    elif "rate" in lowered or status == 429:
        code, message = "OPENAI_RATE_LIMIT", "OpenAI rate limit reached"
    elif "schema" in lowered or "json_schema" in lowered or "invalid_json" in lowered:
        code, message = "OPENAI_SCHEMA_INVALID", "OpenAI rejected the structured output schema"
    elif "permission" in lowered or status == 403 or "model" in lowered and status in {400, 404}:
        code, message = "OPENAI_MODEL_ACCESS", "OpenAI model access was rejected"
    elif status == 400 or "badrequest" in lowered:
        code, message = "OPENAI_BAD_REQUEST", "OpenAI rejected the request"
    else:
        code, message = "OPENAI_REQUEST_FAILED", "OpenAI request failed"
    diagnostics: dict[str, object] = {"provider_error_type": error_type, "request_stage": stage}
    if isinstance(status, int):
        diagnostics["http_status"] = status
    if isinstance(api_code, str) and api_code:
        diagnostics["api_error_code"] = api_code[:80]
    if raw_message:
        redacted = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", raw_message)
        diagnostics["safe_message"] = re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)\S+", r"\1[redacted]", redacted)
    return ProviderRequestError(code, message, diagnostics=diagnostics)


def _validate_architect_selection(output: ArchitectProviderOutput, request: dict[str, Any]) -> None:
    eligible = {
        item.get("task_id") for item in request.get("eligible_tasks", [])
        if isinstance(item, dict) and isinstance(item.get("task_id"), str)
    }
    if output.decision in {"RUN_TASK", "SKIP_TASK", "HUMAN_REVIEW"} and output.task_id not in eligible:
        raise ProviderRequestError(
            "CODEX_ARCHITECT_TASK_NOT_ELIGIBLE", "Codex Architect selected a task outside the trusted eligible queue.",
            diagnostics={"provider_error_type": "ArchitectSelection", "request_stage": "structured_output"},
        )
    if output.decision in {"STOP_DAY", "DAY_COMPLETE"} and output.task_id is not None:
        raise ProviderRequestError(
            "CODEX_ARCHITECT_OUTPUT_INVALID", "Codex Architect returned a task ID for a terminal Day decision.",
            diagnostics={"provider_error_type": "ArchitectSelection", "request_stage": "structured_output"},
        )


def _codex_role_error(role: str, result: StructuredCodexResult) -> ProviderRequestError:
    diagnostics = {
        "provider_error_type": result.error_code or "CodexRoleFailure",
        "request_stage": "codex_exec",
        "duration_ms": result.duration_ms,
    }
    if result.diagnostics:
        diagnostics.update({
            "exit_code": result.diagnostics.exit_code,
            "thread_started": result.diagnostics.thread_started,
            "turn_started": result.diagnostics.turn_started,
            "turn_completed": result.diagnostics.turn_completed,
        })
    return ProviderRequestError(
        f"CODEX_{role}_{result.error_code or 'FAILED'}", result.safe_message or f"Codex {role.title()} failed.", diagnostics=diagnostics,
    )
