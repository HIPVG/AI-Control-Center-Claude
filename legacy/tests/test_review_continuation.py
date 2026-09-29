from datetime import datetime, timedelta, timezone

from backend.control.review_continuation import ReviewContinuationControl


NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)


def _control():
    return ReviewContinuationControl(
        report_id="R-001", response_id="RESP-001", comment_id="C-001", received_at=NOW)


def test_received_and_applying_are_explicit_and_identity_bound():
    control = _control()
    assert control._view("UNCHANGED", None)["state"] == "RECEIVED_PENDING_APPLY"

    mismatch = control.begin(report_id="R-OTHER", response_id="RESP-001", at=NOW + timedelta(seconds=1), actor="codex")
    assert mismatch["reason_code"] == "CONTINUATION_IDENTITY_MISMATCH"
    assert mismatch["state"] == "RECEIVED_PENDING_APPLY"

    applying = control.begin(report_id="R-001", response_id="RESP-001", at=NOW + timedelta(seconds=2), actor="codex")
    assert applying["state"] == "APPLYING"
    assert applying["applying_at"]
    assert applying["execution_actor"] == "codex"


def test_invalid_envelope_fails_continuation_even_with_exit_zero():
    control = _control()
    control.begin(report_id="R-001", response_id="RESP-001", at=NOW, actor="codex")

    result = control.finish(exit_code=0, envelope_valid=False,
                            envelope_validation_reason="missing action", downstream_effect_id=None,
                            at=NOW + timedelta(seconds=1))

    assert result["state"] == "CONTINUATION_FAILED"
    assert result["reason_code"] == "INVALID_CONTINUATION_ENVELOPE"
    assert result["exit_code"] == 0
    assert result["downstream_effect_id"] is None
    assert result["failed_at"]


def test_exit_zero_without_downstream_effect_is_not_applied():
    control = _control()
    control.begin(report_id="R-001", response_id="RESP-001", at=NOW, actor="codex")

    result = control.finish(exit_code=0, envelope_valid=True,
                            envelope_validation_reason="valid", downstream_effect_id=None,
                            at=NOW + timedelta(seconds=1))

    assert result["state"] == "CONTINUATION_FAILED"
    assert result["reason_code"] == "DOWNSTREAM_EFFECT_NOT_OBSERVED"


def test_valid_envelope_is_applied_then_verified_only_after_matching_readback():
    control = _control()
    control.begin(report_id="R-001", response_id="RESP-001", at=NOW, actor="codex")
    applied = control.finish(exit_code=0, envelope_valid=True,
                             envelope_validation_reason="schema and identity valid",
                             downstream_effect_id="REPORT-R-002", at=NOW + timedelta(seconds=1))
    assert applied["state"] == "APPLIED"
    assert applied["verified_at"] is None

    mismatch = control.verify(observed_effect_id="REPORT-OTHER", at=NOW + timedelta(seconds=2))
    assert mismatch["state"] == "APPLIED"
    assert mismatch["reason_code"] == "DOWNSTREAM_EFFECT_MISMATCH"

    verified = control.verify(observed_effect_id="REPORT-R-002", at=NOW + timedelta(seconds=3))
    assert verified["state"] == "VERIFIED"
    assert verified["verified_at"]
    assert verified["day_state_changed"] is False


def test_auth_context_mismatch_records_availability_not_credentials():
    result = _control().fail_auth_context(actor="watcher-service", auth_available=False, at=NOW)

    assert result["state"] == "DELIVERY_FAILED"
    assert result["reason_code"] == "AUTH_CONTEXT_MISMATCH"
    assert result["execution_actor"] == "watcher-service"
    assert result["auth_available"] is False
    assert result["failed_at"]
    assert "token" not in result and "credential" not in result
