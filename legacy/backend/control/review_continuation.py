"""WC-07A continuation observability contract; no process or Day execution."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum


class ContinuationState(str, Enum):
    RECEIVED_PENDING_APPLY = "RECEIVED_PENDING_APPLY"
    APPLYING = "APPLYING"
    APPLIED = "APPLIED"
    VERIFIED = "VERIFIED"
    CONTINUATION_FAILED = "CONTINUATION_FAILED"
    DELIVERY_FAILED = "DELIVERY_FAILED"


class ReviewContinuationControl:
    """Expose continuation progress without treating process exit as application."""

    def __init__(self, *, report_id: str, response_id: str, comment_id: str, received_at: datetime) -> None:
        self.report_id = report_id
        self.response_id = response_id
        self.comment_id = comment_id
        self.state = ContinuationState.RECEIVED_PENDING_APPLY
        self.received_at = self._aware(received_at)
        self.applying_at: datetime | None = None
        self.applied_at: datetime | None = None
        self.verified_at: datetime | None = None
        self.failed_at: datetime | None = None
        self.current_action = "Await bounded continuation start."
        self.exit_code: int | None = None
        self.envelope_valid: bool | None = None
        self.envelope_validation_reason: str | None = None
        self.downstream_effect_id: str | None = None
        self.failure_reason: str | None = None
        self.execution_actor: str | None = None
        self.auth_available: bool | None = None

    def begin(self, *, report_id: str, response_id: str, at: datetime, actor: str) -> dict[str, object]:
        if self.state != ContinuationState.RECEIVED_PENDING_APPLY:
            return self._view("REJECTED", "CONTINUATION_NOT_PENDING")
        if report_id != self.report_id or response_id != self.response_id:
            return self._view("REJECTED", "CONTINUATION_IDENTITY_MISMATCH")
        self.state = ContinuationState.APPLYING
        self.applying_at = self._aware(at)
        self.current_action = "Validate the bounded continuation envelope."
        self.execution_actor = actor
        return self._view("ACCEPTED", None)

    def finish(
        self,
        *,
        exit_code: int,
        envelope_valid: bool,
        envelope_validation_reason: str,
        downstream_effect_id: str | None,
        at: datetime,
    ) -> dict[str, object]:
        if self.state != ContinuationState.APPLYING:
            return self._view("REJECTED", "CONTINUATION_NOT_APPLYING")
        self.exit_code = exit_code
        self.envelope_valid = envelope_valid
        self.envelope_validation_reason = envelope_validation_reason
        if exit_code != 0:
            return self._continuation_fail("CONTINUATION_PROCESS_FAILED", at)
        if not envelope_valid:
            return self._continuation_fail("INVALID_CONTINUATION_ENVELOPE", at)
        if not downstream_effect_id:
            return self._continuation_fail("DOWNSTREAM_EFFECT_NOT_OBSERVED", at)
        self.downstream_effect_id = downstream_effect_id
        self.applied_at = self._aware(at)
        self.state = ContinuationState.APPLIED
        self.current_action = "Read back the recorded downstream effect before verification."
        return self._view("ACCEPTED", None)

    def verify(self, *, observed_effect_id: str, at: datetime) -> dict[str, object]:
        if self.state != ContinuationState.APPLIED:
            return self._view("REJECTED", "CONTINUATION_NOT_APPLIED")
        if not observed_effect_id or observed_effect_id != self.downstream_effect_id:
            return self._view("REJECTED", "DOWNSTREAM_EFFECT_MISMATCH")
        self.state = ContinuationState.VERIFIED
        self.verified_at = self._aware(at)
        self.current_action = "Continuation and downstream effect are verified."
        return self._view("ACCEPTED", None)

    def fail_auth_context(self, *, actor: str, auth_available: bool, at: datetime) -> dict[str, object]:
        self.execution_actor = actor
        self.auth_available = auth_available
        self.state = ContinuationState.DELIVERY_FAILED
        self.failure_reason = "AUTH_CONTEXT_MISMATCH"
        self.failed_at = self._aware(at)
        self.current_action = "Restore the authorized actor context without storing credentials."
        return self._view("FAILED", self.failure_reason)

    def _continuation_fail(self, reason: str, at: datetime) -> dict[str, object]:
        self.state = ContinuationState.CONTINUATION_FAILED
        self.failure_reason = reason
        self.failed_at = self._aware(at)
        self.current_action = "Preserve evidence and diagnose before any retry."
        return self._view("FAILED", reason)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("continuation timestamps must include timezone")
        return value.astimezone(timezone.utc)

    def _view(self, outcome: str, reason_code: str | None) -> dict[str, object]:
        return {
            "outcome": outcome,
            "reason_code": reason_code,
            "state": self.state.value,
            "report_id": self.report_id,
            "response_id": self.response_id,
            "comment_id": self.comment_id,
            "received_at": self.received_at.isoformat(),
            "applying_at": self.applying_at.isoformat() if self.applying_at else None,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "failed_at": self.failed_at.isoformat() if self.failed_at else None,
            "current_action": self.current_action,
            "exit_code": self.exit_code,
            "envelope_valid": self.envelope_valid,
            "envelope_validation_reason": self.envelope_validation_reason,
            "downstream_effect_id": self.downstream_effect_id,
            "failure_reason": self.failure_reason,
            "execution_actor": self.execution_actor,
            "auth_available": self.auth_available,
            "next_action": self.current_action,
            "day_state_changed": False,
        }
