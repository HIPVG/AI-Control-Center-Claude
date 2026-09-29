"""WC-07B human-decision subject binding; no transport or Day execution."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class SubjectType(str, Enum):
    REVIEWER_RECOMMENDATION = "REVIEWER_RECOMMENDATION"
    CODEX_ARTIFACT = "CODEX_ARTIFACT"
    GATE_EXIT = "GATE_EXIT"
    EXECUTION_AUTHORITY = "EXECUTION_AUTHORITY"


class DecisionState(str, Enum):
    HUMAN_RESPONSE_UNCLASSIFIED = "HUMAN_RESPONSE_UNCLASSIFIED"
    HUMAN_DECISION_RECEIVED = "HUMAN_DECISION_RECEIVED"
    REVIEW_CONFIRMATION_PENDING = "REVIEW_CONFIRMATION_PENDING"
    REVIEW_CONFIRMED = "REVIEW_CONFIRMED"
    REJECTED = "REJECTED"
    DELIVERY_FAILED = "DELIVERY_FAILED"


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    decision_id: str = Field(min_length=1, max_length=160)
    revision: int = Field(ge=1)
    subject_type: SubjectType
    decision_subject: str = Field(min_length=1)
    target_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    requested_effect: str = Field(min_length=1)


class HumanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    exact_text: str = Field(min_length=1)
    decider: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    source_class: str = Field(min_length=1)
    received_at: datetime
    decision_id: str | None = None
    revision: int | None = Field(default=None, ge=1)
    subject_type: SubjectType | None = None
    target_commit: str | None = None
    decision_effect: str | None = None
    subject_basis: str | None = None


class HumanDecisionControl:
    """Record human words, then require an exactly bound reviewer confirmation."""

    DIRECT_SOURCE = "RECORDED_DIRECT_CONVERSATION"
    POSITIVE_RESULTS = {"CONTINUE", "ACCEPT_COMPLETE"}

    def __init__(self) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.confirmations_by_source: dict[tuple[str, str, int], str] = {}

    def receive(self, response: HumanResponse, requests: list[DecisionRequest]) -> dict[str, object]:
        received_at = self._aware(response.received_at)
        candidates = {request.decision_id: request for request in requests}
        request = candidates.get(response.decision_id or "")
        reason = self._binding_reason(response, request)
        record_id = f"HD-{len(self.records) + 1:04d}"
        state = DecisionState.HUMAN_DECISION_RECEIVED if reason is None else DecisionState.HUMAN_RESPONSE_UNCLASSIFIED
        self.records[record_id] = {
            "record_id": record_id,
            "state": state,
            "reason_code": reason,
            "exact_text": response.exact_text,
            "decider": response.decider,
            "channel": response.channel,
            "message_id": response.message_id,
            "source_class": response.source_class,
            "received_at": received_at,
            "decision_id": request.decision_id if request else response.decision_id,
            "revision": request.revision if request else response.revision,
            "subject_type": request.subject_type if request else response.subject_type,
            "target_commit": request.target_commit if request else response.target_commit,
            "decision_effect": request.requested_effect if request else response.decision_effect,
            "subject_basis": response.subject_basis,
            "confirmation_report_id": None,
            "confirmation_payload": None,
            "confirmation_response_id": None,
            "allowed_effect": None,
            "effect_evidence_id": None,
        }
        return self.view(record_id)

    def build_confirmation(
        self,
        record_id: str,
        *,
        confirmation_report_id: str,
        confirms_report_id: str,
        confirms_response_id: str,
        authority_record: str,
    ) -> dict[str, object]:
        record = self.records[record_id]
        if record["state"] != DecisionState.HUMAN_DECISION_RECEIVED:
            return self._result(record_id, "REJECTED", "HUMAN_DECISION_NOT_CLASSIFIED")
        key = (str(record["message_id"]), str(record["decision_id"]), int(record["revision"]))
        existing = self.confirmations_by_source.get(key)
        if existing:
            result = self._result(record_id, "IGNORED", "DUPLICATE_CONFIRMATION_SUPPRESSED")
            result["existing_confirmation_report_id"] = existing
            return result
        payload = {
            "REPORT_ID": confirmation_report_id,
            "CONFIRMS_REPORT_ID": confirms_report_id,
            "CONFIRMS_RESPONSE_ID": confirms_response_id,
            "DECISION_ID": record["decision_id"],
            "DECISION_REVISION": record["revision"],
            "AUTHORITY_RECORD": authority_record,
            "REVIEWED_COMMIT": record["target_commit"],
            "SUBJECT_TYPE": record["subject_type"].value,
            "DECISION_EFFECT": record["decision_effect"],
        }
        record["confirmation_report_id"] = confirmation_report_id
        record["confirmation_payload"] = payload
        record["state"] = DecisionState.REVIEW_CONFIRMATION_PENDING
        self.confirmations_by_source[key] = confirmation_report_id
        return self._result(record_id, "ACCEPTED", None)

    def apply_confirmation(
        self,
        record_id: str,
        response: dict[str, object],
        *,
        continuation_succeeded: bool,
        effect_evidence_id: str | None,
    ) -> dict[str, object]:
        record = self.records[record_id]
        if record["state"] != DecisionState.REVIEW_CONFIRMATION_PENDING:
            return self._result(record_id, "REJECTED", "CONFIRMATION_NOT_PENDING")
        payload = record["confirmation_payload"]
        assert isinstance(payload, dict)
        response_id = response.get("RESPONSE_ID")
        if not isinstance(response_id, str) or not response_id.strip():
            return self._result(record_id, "REJECTED", "CONFIRMATION_RESPONSE_ID_MISSING")
        expected = {
            "IN_REPLY_TO": payload["REPORT_ID"],
            "CONFIRMS_REPORT_ID": payload["CONFIRMS_REPORT_ID"],
            "CONFIRMS_RESPONSE_ID": payload["CONFIRMS_RESPONSE_ID"],
            "DECISION_ID": payload["DECISION_ID"],
            "DECISION_REVISION": payload["DECISION_REVISION"],
            "REVIEWED_COMMIT": payload["REVIEWED_COMMIT"],
            "SUBJECT_TYPE": payload["SUBJECT_TYPE"],
            "DECISION_EFFECT": payload["DECISION_EFFECT"],
        }
        for field, value in expected.items():
            if response.get(field) != value:
                return self._result(record_id, "REJECTED", f"CONFIRMATION_{field}_MISMATCH")
        if response.get("RESULT") not in self.POSITIVE_RESULTS:
            record["state"] = DecisionState.REJECTED
            record["reason_code"] = "REVIEWER_DID_NOT_CONFIRM"
            return self._result(record_id, "REJECTED", "REVIEWER_DID_NOT_CONFIRM")
        if not continuation_succeeded or not effect_evidence_id:
            return self._result(record_id, "REJECTED", "CONFIRMATION_EFFECT_NOT_VERIFIED")
        record["state"] = DecisionState.REVIEW_CONFIRMED
        record["reason_code"] = None
        record["confirmation_response_id"] = response_id
        record["allowed_effect"] = record["decision_effect"]
        record["effect_evidence_id"] = effect_evidence_id
        return self._result(record_id, "ACCEPTED", None)

    def mark_delivery_failure(self, record_id: str, reason: str) -> dict[str, object]:
        record = self.records[record_id]
        record["state"] = DecisionState.DELIVERY_FAILED
        record["reason_code"] = reason
        return self._result(record_id, "FAILED", reason)

    def view(self, record_id: str) -> dict[str, object]:
        record = self.records[record_id]
        subject_type = record["subject_type"]
        confirmed = record["state"] == DecisionState.REVIEW_CONFIRMED
        return {
            "record_id": record_id,
            "state": record["state"].value,
            "reason_code": record["reason_code"],
            "exact_text": record["exact_text"],
            "decider": record["decider"],
            "channel": record["channel"],
            "message_id": record["message_id"],
            "source_class": record["source_class"],
            "received_at": record["received_at"].isoformat(),
            "decision_id": record["decision_id"],
            "revision": record["revision"],
            "subject_type": subject_type.value if isinstance(subject_type, SubjectType) else subject_type,
            "target_commit": record["target_commit"],
            "decision_effect": record["decision_effect"],
            "subject_basis": record["subject_basis"],
            "confirmation_report_id": record["confirmation_report_id"],
            "confirmation_payload": record["confirmation_payload"],
            "confirmation_response_id": record["confirmation_response_id"],
            "allowed_effect": record["allowed_effect"],
            "effect_evidence_id": record["effect_evidence_id"],
            "effect_applied": confirmed,
            "artifact_accepted": confirmed and subject_type == SubjectType.CODEX_ARTIFACT,
            "gate_exit_complete": confirmed and subject_type == SubjectType.GATE_EXIT,
            "implementation_authorized": confirmed and subject_type == SubjectType.EXECUTION_AUTHORITY,
            "next_stage_authorized": False,
            "pr_close_authorized": False,
            "merge_authorized": False,
            "day_state_changed": False,
        }

    def _result(self, record_id: str, outcome: str, reason: str | None) -> dict[str, object]:
        result = self.view(record_id)
        result["outcome"] = outcome
        result["reason_code"] = reason
        return result

    def _binding_reason(self, response: HumanResponse, request: DecisionRequest | None) -> str | None:
        if response.source_class != self.DIRECT_SOURCE:
            return "SOURCE_NOT_DIRECT_AUTHORITY"
        if request is None:
            return "DECISION_SUBJECT_NOT_BOUND"
        if response.revision != request.revision:
            return "DECISION_REVISION_MISMATCH"
        if response.subject_type != request.subject_type:
            return "SUBJECT_TYPE_MISMATCH"
        if response.target_commit != request.target_commit:
            return "TARGET_COMMIT_MISMATCH"
        if response.decision_effect != request.requested_effect:
            return "DECISION_EFFECT_MISMATCH"
        if not response.subject_basis:
            return "SUBJECT_BASIS_MISSING"
        return None

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("decision timestamps must include timezone")
        return value.astimezone(timezone.utc)
