"""Bounded, advisory External Reviewer transports for failed Day repairs.

The reviewer receives only a compact failure package and can never create an
edit, command, acceptance test, or expanded work order. Its output remains
advisory until the existing bounded Builder and deterministic verifier accept
it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(value: object) -> str:
    material = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _safe_text(value: object, limit: int = 2000) -> str:
    text = str(value or "").replace("\x00", "")
    text = re.sub(r"(?i)\b(api[_-]?key|access[_-]?token|password|secret|credential)\b\s*[:=]\s*(['\"]).*?\2", r"\1=<REDACTED>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "<REDACTED>", text)
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._-]{12,}\b", "Bearer <REDACTED>", text)
    text = re.sub(r"(?i)\bOPENAI_API_KEY\s*=\s*[^\s]+", "OPENAI_API_KEY=<REDACTED>", text)
    return text[:limit]


def _sanitize_value(value: object, source_root: Path, *, depth: int = 0) -> object:
    """Bound every outbound/persisted untyped value and redact unsafe paths."""
    if depth >= 4:
        return "<TRUNCATED_DEPTH>"
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        candidate = Path(value)
        if candidate.is_absolute():
            try:
                value = str(candidate.resolve().relative_to(source_root)).replace("\\", "/")
            except (OSError, ValueError):
                return "<REDACTED_PATH>"
        return _safe_text(value, 1000)
    if isinstance(value, dict):
        clean: dict[str, object] = {}
        for key, item in list(value.items())[:24]:
            name = _safe_text(key, 100)
            clean[name] = "<REDACTED>" if re.search(r"(?i)(api[_-]?key|token|password|secret|credential)", name) else _sanitize_value(item, source_root, depth=depth + 1)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_value(item, source_root, depth=depth + 1) for item in list(value)[:24]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(value, 1000)


class ExternalReviewConfig(BaseModel):
    """Administrator-owned transport policy; runtime input cannot select it."""

    transport: Literal["OPENAI_RESPONSES", "BROWSER_MANUAL", "FIXTURE"] = "OPENAI_RESPONSES"
    model: str = Field(default="gpt-5.6-sol", min_length=1, max_length=100)
    fallback_model: str | None = Field(default="gpt-5.6-terra", max_length=100)
    timeout_seconds: int = Field(default=45, ge=1, le=180)
    max_output_tokens: int = Field(default=1800, ge=200, le=4000)
    conversation_url: str | None = Field(default=None, max_length=1000)
    conversation_id: str | None = Field(default=None, max_length=160)


def load_external_review_config(path: Path) -> ExternalReviewConfig | None:
    """Read only administrator-owned JSON; no secret is accepted here."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("enabled") is not True:
        return None
    try:
        browser = value.get("browser_manual", value.get("target", {}))
        candidate = {
            "transport": value.get("transport", value.get("policy", {}).get("transport")),
            "model": value.get("model", "gpt-5.6-sol"),
            "fallback_model": value.get("fallback_model", "gpt-5.6-terra"),
            "timeout_seconds": value.get("timeout_seconds", 45),
            "max_output_tokens": value.get("max_output_tokens", 1800),
            "conversation_url": browser.get("conversation_url") if isinstance(browser, dict) else None,
            "conversation_id": browser.get("conversation_id") if isinstance(browser, dict) else None,
        }
        config = ExternalReviewConfig.model_validate(candidate)
        if config.transport != "OPENAI_RESPONSES" or config.model != "gpt-5.6-sol":
            return None
        if config.fallback_model not in {None, "gpt-5.6-terra"}:
            return None
        return config
    except (TypeError, ValueError):
        return None


class ReviewerContextPack(BaseModel):
    """Small server-owned repair policy, intentionally not repository history."""

    version: Literal["external-review-context-v1"] = "external-review-context-v1"
    principles: list[str] = Field(default_factory=list, max_length=16)
    fingerprint: str = ""

    def finalize(self) -> "ReviewerContextPack":
        self.fingerprint = _fingerprint(self.model_dump(exclude={"fingerprint"}, mode="json"))
        return self

    def instructions(self) -> str:
        return "\n".join([
            "You are an advisory external repair reviewer.",
            *self.principles,
            "Return only the required JSON review object.",
        ])


def build_reviewer_context_pack() -> ReviewerContextPack:
    return ReviewerContextPack(principles=[
        "The server owns the selected Day, contract, completion criteria, evidence validation, and state transitions.",
        "Provide diagnosis and bounded repair guidance only; you have no execution or acceptance authority.",
        "The existing DynamicDayWorkOrder is the sole authority for allowed files and acceptance tests.",
        "Never expand scope, modify protected paths, weaken acceptance tests, change Day criteria, or change research semantics.",
        "Repair is distinct from research; do not propose experiments, holdout changes, or model-condition changes.",
        "A Builder proposal is not final authority: independent deterministic verification and Evidence Store validation are mandatory.",
        "Terminal task status, retries, and narrative claims are not evidence; return only guidance that can be verified.",
        "If a product, research, authority, or scope decision is needed, return HUMAN_DECISION_REQUIRED.",
    ]).finalize()


class ReviewerConversationState(BaseModel):
    context_pack_version: str = Field(min_length=1, max_length=80)
    context_pack_fingerprint: str = Field(min_length=8, max_length=128)
    conversation_id: str | None = Field(default=None, max_length=160)
    previous_response_id: str | None = Field(default=None, max_length=160)
    updated_at: str = Field(default_factory=_utc_now)


class FailurePackage(BaseModel):
    package_id: str = Field(default_factory=lambda: uuid4().hex)
    fingerprint: str = ""
    selected_day: int = Field(ge=1, le=14)
    contract_version: str = Field(min_length=1, max_length=80)
    contract_fingerprint: str = Field(min_length=8, max_length=128)
    criterion_id: str = Field(min_length=1, max_length=100)
    evidence_type: str = Field(min_length=1, max_length=100)
    gap_diagnosis: dict[str, object]
    failed_action: dict[str, object]
    repair_scope: dict[str, list[str]]
    failure_excerpt: str = Field(max_length=2000)
    stdout_excerpt: str = Field(default="", max_length=2000)
    stderr_excerpt: str = Field(default="", max_length=2000)
    source_excerpts: dict[str, str] = Field(default_factory=dict)
    git_summary: dict[str, object] = Field(default_factory=dict)
    previous_attempts: list[dict[str, object]] = Field(default_factory=list)
    rejection_feedback: list[str] = Field(default_factory=list)
    catalog_matches: list[dict[str, object]] = Field(default_factory=list)
    expert_solver_outcome: str = Field(default="", max_length=160)
    deterministic_verification_failure: str = Field(default="", max_length=2000)
    request: str = Field(default="Diagnose this bounded failure and provide repair guidance only. Do not propose commands, broaden scope, alter acceptance tests, research semantics, or completion criteria.", max_length=1200)

    def finalize(self) -> "FailurePackage":
        self.fingerprint = _fingerprint(self.model_dump(exclude={"fingerprint", "package_id"}, mode="json"))
        return self


class CapturedExternalResponse(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=160)
    response_id: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=100)
    context_pack_fingerprint: str | None = Field(default=None, max_length=128)
    submitted_at: str
    response_received_at: str
    response_text: str = Field(min_length=1, max_length=12000)
    response_fingerprint: str = ""

    def finalize(self) -> "CapturedExternalResponse":
        self.response_fingerprint = _fingerprint(self.response_text)
        return self


class NormalizedExternalReview(BaseModel):
    status: Literal["REPAIR_GUIDANCE", "HUMAN_DECISION_REQUIRED", "REVIEW_FAILED"]
    diagnosis: str = Field(default="", max_length=1600)
    proposed_repair: str = Field(default="", max_length=2400)
    relevant_files: list[str] = Field(default_factory=list, max_length=8)
    expected_behavior: str = Field(default="", max_length=1600)
    suggested_verification: list[str] = Field(default_factory=list, max_length=5)
    cautions: list[str] = Field(default_factory=list, max_length=8)
    rejection_reason: str | None = Field(default=None, max_length=1200)


class ExternalReviewArtifact(BaseModel):
    package: FailurePackage
    context_pack_fingerprint: str | None = Field(default=None, max_length=128)
    transport: Literal["OPENAI_RESPONSES", "BROWSER_MANUAL", "FIXTURE"] | None = None
    captured_response: CapturedExternalResponse | None = None
    normalized: NormalizedExternalReview | None = None
    failure_code: str | None = Field(default=None, max_length=100)
    status: Literal["GUIDANCE_RECEIVED", "HUMAN_DECISION_REQUIRED", "REVIEW_FAILED"]
    artifact_path: str = ""


class ExternalReviewTransportError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExternalReviewTransport(Protocol):
    def submit(self, config: ExternalReviewConfig, context: ReviewerContextPack,
               package: FailurePackage, state: ReviewerConversationState | None) -> CapturedExternalResponse: ...


class DisabledBrowserReviewTransport:
    """Browser-manual fallback has no unattended submission bridge."""

    def submit(self, config: ExternalReviewConfig, context: ReviewerContextPack,
               package: FailurePackage, state: ReviewerConversationState | None) -> CapturedExternalResponse:
        raise ExternalReviewTransportError("BROWSER_MANUAL_TRANSPORT_UNAVAILABLE")


class ResponsesExternalReviewTransport:
    """Zero-touch Responses API transport with persisted, bounded response state."""

    def __init__(self, client_factory: object | None = None) -> None:
        self.client_factory = client_factory

    @staticmethod
    def _field(value: object, name: str, default: object = None) -> object:
        return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)

    @staticmethod
    def _error_code(exc: Exception) -> str:
        text = f"{type(exc).__name__} {getattr(exc, 'status_code', '')} {getattr(exc, 'code', '')} {exc}".lower()
        if "auth" in text or "permission" in text or "401" in text or "403" in text:
            return "OPENAI_AUTHENTICATION_FAILED"
        if "quota" in text or "rate" in text or "429" in text:
            return "OPENAI_QUOTA_OR_RATE_LIMIT"
        if "model_not_found" in text or "model access" in text or "model unavailable" in text:
            return "OPENAI_MODEL_ACCESS"
        if "timeout" in text:
            return "OPENAI_TIMEOUT"
        return "OPENAI_RESPONSES_UNAVAILABLE"

    @staticmethod
    def _review_schema() -> dict[str, object]:
        properties = {
            "status": {"type": "string", "enum": ["REPAIR_GUIDANCE", "HUMAN_DECISION_REQUIRED", "REVIEW_FAILED"]},
            "diagnosis": {"type": "string"}, "proposed_repair": {"type": "string"},
            "relevant_files": {"type": "array", "items": {"type": "string"}},
            "expected_behavior": {"type": "string"},
            "suggested_verification": {"type": "array", "items": {"type": "string"}},
            "cautions": {"type": "array", "items": {"type": "string"}},
            "rejection_reason": {"type": ["string", "null"]},
        }
        return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}

    def submit(self, config: ExternalReviewConfig, context: ReviewerContextPack,
               package: FailurePackage, state: ReviewerConversationState | None) -> CapturedExternalResponse:
        if not os.environ.get("OPENAI_API_KEY"):
            raise ExternalReviewTransportError("OPENAI_CREDENTIALS_MISSING")
        try:
            if self.client_factory is None:
                from openai import OpenAI
                client = OpenAI(timeout=config.timeout_seconds)
            else:
                client = self.client_factory()
            submitted_at = _utc_now()
            def create(model: str) -> object:
                request: dict[str, object] = {
                    "model": model,
                    "instructions": context.instructions(),
                    "input": json.dumps({"failure_package": package.model_dump(mode="json")}, ensure_ascii=False, sort_keys=True),
                    "max_output_tokens": config.max_output_tokens,
                    "store": True,
                    "text": {"format": {"type": "json_schema", "name": "bounded_external_repair_review", "strict": True, "schema": self._review_schema()}},
                }
                if state and state.previous_response_id:
                    request["previous_response_id"] = state.previous_response_id
                return client.responses.create(**request)
            selected_model = config.model
            try:
                response = create(selected_model)
            except Exception as first_error:
                if self._error_code(first_error) != "OPENAI_MODEL_ACCESS" or not config.fallback_model or config.fallback_model == config.model:
                    raise
                selected_model = config.fallback_model
                response = create(selected_model)
        except ExternalReviewTransportError:
            raise
        except Exception as exc:
            raise ExternalReviewTransportError(self._error_code(exc)) from None
        response_id = self._field(response, "id")
        text = self._field(response, "output_text", "")
        if not isinstance(response_id, str) or not response_id or not isinstance(text, str) or not text:
            raise ExternalReviewTransportError("OPENAI_RESPONSE_INVALID")
        conversation = self._field(response, "conversation")
        conversation_id = self._field(conversation, "id") if conversation is not None else None
        return CapturedExternalResponse(
            conversation_id=conversation_id if isinstance(conversation_id, str) and conversation_id else response_id,
            response_id=response_id, model=selected_model, context_pack_fingerprint=context.fingerprint,
            submitted_at=submitted_at, response_received_at=_utc_now(), response_text=_safe_text(text, 12000),
        )


class ExternalReviewStore:
    def __init__(self, state_root: Path) -> None:
        self.root = state_root.resolve()

    def save(self, artifact: ExternalReviewArtifact) -> ExternalReviewArtifact:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{artifact.package.fingerprint}.json"
        artifact.artifact_path = str(path)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
        return artifact

    def load_conversation(self) -> ReviewerConversationState | None:
        try:
            return ReviewerConversationState.model_validate_json((self.root / "reviewer-conversation.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None

    def load_artifact(self, path: str) -> ExternalReviewArtifact | None:
        try:
            candidate = Path(path).resolve()
            if not candidate.is_relative_to(self.root.resolve()):
                return None
            return ExternalReviewArtifact.model_validate_json(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None

    def save_conversation(self, state: ReviewerConversationState) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "reviewer-conversation.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)


class ExternalReviewCoordinator:
    """Build, persist, submit, capture, and scope-check advisory review."""

    def __init__(self, source_root: Path, state_root: Path | None = None, config: ExternalReviewConfig | None = None,
                 transport: ExternalReviewTransport | None = None) -> None:
        self.root = source_root.resolve()
        self.config = config
        self.context = build_reviewer_context_pack()
        self.store = ExternalReviewStore(state_root or self.root / "state" / "external-review")
        self.transport = transport or (
            ResponsesExternalReviewTransport()
            if config and config.transport == "OPENAI_RESPONSES"
            else DisabledBrowserReviewTransport()
        )

    def build_package(self, *, selected_day: int, contract_version: str, contract_fingerprint: str,
                      criterion_id: str, evidence_type: str, gap_diagnosis: dict[str, object],
                      failed_action: dict[str, object], allowed_files: list[str], context_files: list[str],
                      acceptance_test_files: list[str], failure_excerpt: object, stdout_excerpt: object,
                      stderr_excerpt: object, git_summary: dict[str, object], previous_attempts: list[dict[str, object]],
                      rejection_feedback: list[str], catalog_matches: list[dict[str, object]],
                      expert_solver_outcome: object, deterministic_verification_failure: object) -> FailurePackage:
        scope = {"allowed_files": list(allowed_files), "context_files": list(context_files), "acceptance_test_files": list(acceptance_test_files)}
        excerpts = {path: value for path in [*allowed_files, *context_files] if (value := self._read_allowed_excerpt(path))}
        return FailurePackage(
            selected_day=selected_day, contract_version=contract_version, contract_fingerprint=contract_fingerprint,
            criterion_id=criterion_id, evidence_type=evidence_type, gap_diagnosis=_sanitize_value(gap_diagnosis, self.root),
            failed_action=_sanitize_value(failed_action, self.root), repair_scope=_sanitize_value(scope, self.root), failure_excerpt=_safe_text(failure_excerpt),
            stdout_excerpt=_safe_text(stdout_excerpt), stderr_excerpt=_safe_text(stderr_excerpt), source_excerpts=excerpts,
            git_summary=_sanitize_value({key: git_summary.get(key) for key in ("branch", "head", "working_tree_clean", "status_count", "source_fingerprint")}, self.root),
            previous_attempts=_sanitize_value(previous_attempts[-3:], self.root), rejection_feedback=_sanitize_value(rejection_feedback[-3:], self.root),
            catalog_matches=_sanitize_value(catalog_matches[:10], self.root), expert_solver_outcome=_safe_text(expert_solver_outcome, 160),
            deterministic_verification_failure=_safe_text(deterministic_verification_failure),
        ).finalize()

    def request_review(self, package: FailurePackage) -> ExternalReviewArtifact:
        transport = self.config.transport if self.config else None
        if self.config is None:
            return self.store.save(ExternalReviewArtifact(
                package=package, context_pack_fingerprint=self.context.fingerprint,
                status="REVIEW_FAILED", failure_code="EXTERNAL_REVIEW_NOT_CONFIGURED",
            ))
        state = self.store.load_conversation()
        if state and state.context_pack_fingerprint != self.context.fingerprint:
            state = None
        try:
            captured = self.transport.submit(self.config, self.context, package, state).finalize()
        except ExternalReviewTransportError as exc:
            return self.store.save(ExternalReviewArtifact(
                package=package, context_pack_fingerprint=self.context.fingerprint, transport=transport,
                status="REVIEW_FAILED", failure_code=exc.code,
            ))
        except (OSError, RuntimeError, ValueError):
            return self.store.save(ExternalReviewArtifact(
                package=package, context_pack_fingerprint=self.context.fingerprint, transport=transport,
                status="REVIEW_FAILED", failure_code="EXTERNAL_REVIEW_TRANSPORT_FAILED",
            ))
        self.store.save_conversation(ReviewerConversationState(
            context_pack_version=self.context.version, context_pack_fingerprint=self.context.fingerprint,
            conversation_id=captured.conversation_id, previous_response_id=captured.response_id, updated_at=_utc_now(),
        ))
        normalized = self._normalize(captured.response_text, package.repair_scope)
        status = "GUIDANCE_RECEIVED" if normalized.status == "REPAIR_GUIDANCE" else "HUMAN_DECISION_REQUIRED" if normalized.status == "HUMAN_DECISION_REQUIRED" else "REVIEW_FAILED"
        return self.store.save(ExternalReviewArtifact(
            package=package, context_pack_fingerprint=self.context.fingerprint, transport=transport,
            captured_response=captured, normalized=normalized, status=status,
        ))

    def external_prerequisite_resolved(self, reason_code: str) -> bool:
        """Read-only resolution check; API errors never inherit Codex REAL mode."""
        if not self.config or self.config.transport != "OPENAI_RESPONSES":
            return False
        # A previously missing credential can be observed safely. Authentication,
        # quota, and model-access failures require a later successful provider
        # interaction or an explicit server-owned authority resolver.
        return reason_code == "OPENAI_CREDENTIALS_MISSING" and bool(os.environ.get("OPENAI_API_KEY"))

    def _read_allowed_excerpt(self, relative: str) -> str:
        candidate = (self.root / relative).resolve()
        if not candidate.is_relative_to(self.root) or not candidate.is_file() or candidate.suffix not in {".py", ".json", ".md", ".yaml", ".yml"}:
            return ""
        try:
            return _safe_text(candidate.read_text(encoding="utf-8", errors="replace"), 4000)
        except OSError:
            return ""

    @staticmethod
    def _normalize(text: str, scope: dict[str, list[str]]) -> NormalizedExternalReview:
        try:
            review = NormalizedExternalReview.model_validate(json.loads(text))
        except (TypeError, ValueError, json.JSONDecodeError):
            return NormalizedExternalReview(status="REVIEW_FAILED", rejection_reason="External response is not the required bounded JSON review object.")
        allowed = set(scope["allowed_files"])
        tests = set(scope["acceptance_test_files"])
        if review.status != "REPAIR_GUIDANCE":
            return review
        if not review.relevant_files or any(path not in allowed for path in review.relevant_files):
            return NormalizedExternalReview(status="HUMAN_DECISION_REQUIRED", rejection_reason="Reviewer proposed files outside the server-owned repair scope.")
        if any(test not in tests for test in review.suggested_verification):
            return NormalizedExternalReview(status="HUMAN_DECISION_REQUIRED", rejection_reason="Reviewer proposed verification outside the configured acceptance tests.")
        forbidden = ("acceptance criteria", "completion criteria", "research semantic", "experiment condition", "ignore test", "disable test", "protected path")
        material = " ".join([review.proposed_repair, review.expected_behavior]).lower()
        if any(token in material for token in forbidden):
            return NormalizedExternalReview(status="HUMAN_DECISION_REQUIRED", rejection_reason="Reviewer guidance attempts to change protected server-owned semantics.")
        return review
