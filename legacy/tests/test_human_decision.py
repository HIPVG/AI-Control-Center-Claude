from datetime import datetime, timezone

from backend.control.human_decision import (
    DecisionRequest,
    HumanDecisionControl,
    HumanResponse,
    SubjectType,
)


NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)
COMMIT = "a" * 40


def request(decision_id="D1", subject=SubjectType.REVIEWER_RECOMMENDATION, effect="apply fix A"):
    return DecisionRequest(decision_id=decision_id, revision=1, subject_type=subject,
                           decision_subject=f"Decide {decision_id}", target_commit=COMMIT,
                           requested_effect=effect)


def response(**changes):
    values = dict(exact_text="D1の修正提案を承認します", decider="広瀬剛", channel="codex_chat",
                  message_id="M1", source_class="RECORDED_DIRECT_CONVERSATION", received_at=NOW,
                  decision_id="D1", revision=1, subject_type=SubjectType.REVIEWER_RECOMMENDATION,
                  target_commit=COMMIT, decision_effect="apply fix A", subject_basis="explicit decision ID")
    values.update(changes)
    return HumanResponse(**values)


def confirmation(control, record_id, **changes):
    payload = control.records[record_id]["confirmation_payload"]
    values = {
        "IN_REPLY_TO": payload["REPORT_ID"], "RESULT": "CONTINUE", "RESPONSE_ID": "RR2",
        "CONFIRMS_REPORT_ID": payload["CONFIRMS_REPORT_ID"],
        "CONFIRMS_RESPONSE_ID": payload["CONFIRMS_RESPONSE_ID"],
        "DECISION_ID": payload["DECISION_ID"], "DECISION_REVISION": payload["DECISION_REVISION"],
        "REVIEWED_COMMIT": payload["REVIEWED_COMMIT"], "SUBJECT_TYPE": payload["SUBJECT_TYPE"],
        "DECISION_EFFECT": payload["DECISION_EFFECT"],
    }
    values.update(changes)
    return values


def pending(control, record_id):
    return control.build_confirmation(record_id, confirmation_report_id="R2", confirms_report_id="R1",
                                      confirms_response_id="RESP1", authority_record="docs/auth.md")


def test_h01_bare_approval_with_two_subjects_is_unclassified_and_closes_nothing():
    control = HumanDecisionControl()
    bare = response(decision_id=None, revision=None, subject_type=None, target_commit=None,
                    decision_effect=None, subject_basis=None, exact_text="承認します")
    result = control.receive(bare, [request(), request("D2", SubjectType.CODEX_ARTIFACT, "accept C")])
    assert result["state"] == "HUMAN_RESPONSE_UNCLASSIFIED"
    assert result["reason_code"] == "DECISION_SUBJECT_NOT_BOUND"
    assert result["exact_text"] == "承認します"
    assert result["effect_applied"] is False and result["gate_exit_complete"] is False


def test_h02_explicit_response_is_recorded_but_not_applied_before_confirmation():
    control = HumanDecisionControl()
    result = control.receive(response(), [request()])
    assert result["state"] == "HUMAN_DECISION_RECEIVED"
    assert result["effect_applied"] is False
    pending_result = pending(control, result["record_id"])
    assert pending_result["state"] == "REVIEW_CONFIRMATION_PENDING"
    assert pending_result["effect_applied"] is False
    not_verified = control.apply_confirmation(result["record_id"], confirmation(control, result["record_id"]),
                                              continuation_succeeded=False, effect_evidence_id=None)
    assert not_verified["reason_code"] == "CONFIRMATION_EFFECT_NOT_VERIFIED"
    assert not_verified["state"] == "REVIEW_CONFIRMATION_PENDING"
    assert not_verified["effect_applied"] is False


def test_h03_reviewer_proposal_confirmation_applies_only_named_fix():
    control = HumanDecisionControl()
    record_id = control.receive(response(), [request()])["record_id"]
    pending(control, record_id)
    result = control.apply_confirmation(record_id, confirmation(control, record_id),
                                        continuation_succeeded=True, effect_evidence_id="E-fix-A")
    assert result["state"] == "REVIEW_CONFIRMED"
    assert result["allowed_effect"] == "apply fix A"
    assert result["artifact_accepted"] is False and result["gate_exit_complete"] is False
    assert result["next_stage_authorized"] is False


def test_h04_artifact_acceptance_does_not_exit_gate_or_start_next_stage():
    control = HumanDecisionControl()
    req = request(subject=SubjectType.CODEX_ARTIFACT, effect="accept commit C")
    human = response(subject_type=SubjectType.CODEX_ARTIFACT, decision_effect="accept commit C")
    record_id = control.receive(human, [req])["record_id"]
    pending(control, record_id)
    result = control.apply_confirmation(record_id, confirmation(control, record_id),
                                        continuation_succeeded=True, effect_evidence_id="E-accept-C")
    assert result["artifact_accepted"] is True
    assert result["gate_exit_complete"] is False and result["next_stage_authorized"] is False


def test_h05_duplicate_input_has_one_confirmation_while_another_request_coexists():
    control = HumanDecisionControl()
    first = control.receive(response(), [request(), request("D2", SubjectType.GATE_EXIT, "exit G5")])
    assert pending(control, first["record_id"])["confirmation_report_id"] == "R2"
    duplicate = control.receive(response(), [request(), request("D2", SubjectType.GATE_EXIT, "exit G5")])
    suppressed = control.build_confirmation(duplicate["record_id"], confirmation_report_id="R3",
                                            confirms_report_id="R1", confirms_response_id="RESP1",
                                            authority_record="docs/auth.md")
    assert suppressed["reason_code"] == "DUPLICATE_CONFIRMATION_SUPPRESSED"
    assert suppressed["existing_confirmation_report_id"] == "R2"
    assert control.records[first["record_id"]]["state"].value == "REVIEW_CONFIRMATION_PENDING"


def test_h06_mismatched_version_or_commit_never_applies():
    control = HumanDecisionControl()
    record_id = control.receive(response(), [request()])["record_id"]
    pending(control, record_id)
    result = control.apply_confirmation(record_id, confirmation(control, record_id,
                                        REVIEWED_COMMIT="b" * 40), continuation_succeeded=True,
                                        effect_evidence_id="E-wrong")
    assert result["reason_code"] == "CONFIRMATION_REVIEWED_COMMIT_MISMATCH"
    assert result["state"] == "REVIEW_CONFIRMATION_PENDING"
    assert result["effect_applied"] is False


def test_h06_missing_confirmation_response_id_never_applies_or_records_effect():
    control = HumanDecisionControl()
    record_id = control.receive(response(), [request()])["record_id"]
    pending(control, record_id)
    reply = confirmation(control, record_id)
    del reply["RESPONSE_ID"]

    result = control.apply_confirmation(record_id, reply, continuation_succeeded=True,
                                        effect_evidence_id="E-must-not-apply")

    assert result["reason_code"] == "CONFIRMATION_RESPONSE_ID_MISSING"
    assert result["state"] == "REVIEW_CONFIRMATION_PENDING"
    assert result["confirmation_response_id"] is None
    assert result["allowed_effect"] is None
    assert result["effect_evidence_id"] is None
    assert result["effect_applied"] is False


def test_h07_proxy_and_delivery_failure_preserve_source_without_promoting_authority():
    control = HumanDecisionControl()
    result = control.receive(response(source_class="PROXY_RELAY", exact_text="人間が承認したとの転記"), [request()])
    assert result["state"] == "HUMAN_RESPONSE_UNCLASSIFIED"
    assert result["reason_code"] == "SOURCE_NOT_DIRECT_AUTHORITY"
    failed = control.mark_delivery_failure(result["record_id"], "REVIEW_RESPONSE_TIMEOUT")
    assert failed["state"] == "DELIVERY_FAILED"
    assert failed["source_class"] == "PROXY_RELAY"
    assert failed["exact_text"] == "人間が承認したとの転記"
    assert failed["effect_applied"] is False


def test_h08_gate_exit_confirmation_completes_only_that_gate():
    control = HumanDecisionControl()
    req = request(subject=SubjectType.GATE_EXIT, effect="complete G5 exit")
    human = response(subject_type=SubjectType.GATE_EXIT, decision_effect="complete G5 exit")
    record_id = control.receive(human, [req])["record_id"]
    pending(control, record_id)
    result = control.apply_confirmation(record_id, confirmation(control, record_id),
                                        continuation_succeeded=True, effect_evidence_id="E-gate-exit")
    assert result["gate_exit_complete"] is True
    assert result["next_stage_authorized"] is False
    assert result["pr_close_authorized"] is False and result["merge_authorized"] is False
    assert result["day_state_changed"] is False
