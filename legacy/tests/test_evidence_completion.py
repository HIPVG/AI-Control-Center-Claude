from datetime import datetime, timezone

from backend.control.evidence_completion import CompletionEvidenceEvaluator
from backend.control.local_llm_day_program import LocalLLMDayProgram
from backend.models.local_llm_day import RunIntent, RunLimits


FIXTURE_ROOT = __import__("pathlib").Path(__file__).parent / "fixtures" / "day-contract"


def _context(criterion_index=0):
    contract = LocalLLMDayProgram(FIXTURE_ROOT)._load_contracts()[6]
    intent = RunIntent(
        run_id="run-wc05-fixture",
        selected_day=6,
        go_at=datetime.now(timezone.utc),
        contract_fingerprint=LocalLLMDayProgram._contract_fingerprint(contract),
        policy_fingerprint="p" * 64,
        config_fingerprint="c" * 64,
        git_fingerprint="g" * 64,
        requested_limits=RunLimits(active_work_seconds=1800, max_attempts=2, max_cost=0, currency="JPY"),
    )
    return contract, intent, contract.completion_criteria[criterion_index]


def _result(intent, criterion, evidence_type, value, **updates):
    result = {
        "run_id": intent.run_id,
        "criterion_id": criterion.criterion_id,
        "evidence_type": evidence_type,
        "provider_id": "fixture-provider",
        "provider_version": "v1",
        "source_fingerprint": "s" * 64,
        "configuration_fingerprint": intent.config_fingerprint,
        "value": value,
    }
    result.update(updates)
    return result


def _value(evidence_type):
    values = {
        "schema_contract": {"schema_path": "schema.json", "schema_version": "v1"},
        "source_check": {"checked_paths": ["src/state.py"], "assertions": ["planned and actual are distinct"]},
        "provenance_test": {"exit_code": 0, "commands": ["pytest focused"], "passed": 1, "failed": 0},
    }
    return values[evidence_type]


def test_only_run_bound_validated_records_satisfy_the_criterion():
    contract, intent, criterion = _context()
    results = [_result(intent, criterion, name, _value(name)) for name in criterion.required_evidence]

    evaluation = CompletionEvidenceEvaluator().evaluate_criterion(
        intent=intent, contract=contract, criterion_id=criterion.criterion_id, results=results)

    assert evaluation["status"] == "CRITERION_COMPLETE"
    assert evaluation["criterion_satisfied"] is True
    assert set(evaluation["evidence_record_ids"]) == set(criterion.required_evidence)
    assert all(record["run_id"] == intent.run_id for record in evaluation["evidence_records"].values())
    assert all(record["criterion_id"] == criterion.criterion_id for record in evaluation["evidence_records"].values())
    assert all(record["validator_result"] is True for record in evaluation["evidence_records"].values())
    assert evaluation["day_execution_started"] is False


def test_type_mismatch_and_empty_output_fail_closed_without_records():
    contract, intent, criterion = _context()
    results = [
        _result(intent, criterion, "git_head", {"branch": "main", "head": "a", "is_commit": True}),
        _result(intent, criterion, criterion.required_evidence[0], {}),
    ]

    evaluation = CompletionEvidenceEvaluator().evaluate_criterion(
        intent=intent, contract=contract, criterion_id=criterion.criterion_id, results=results)

    assert evaluation["status"] == "INCOMPLETE"
    assert evaluation["evidence_records"] == {}
    assert {item["reason_code"] for item in evaluation["adapter_outcomes"]} == {
        "EVIDENCE_TYPE_NOT_REQUIRED", "EVIDENCE_VALIDATION_FAILED"}


def test_exit_zero_alone_does_not_become_test_evidence():
    contract, intent, criterion = _context(1)
    evidence_type = "provenance_test"

    evaluation = CompletionEvidenceEvaluator().evaluate_criterion(
        intent=intent,
        contract=contract,
        criterion_id=criterion.criterion_id,
        results=[_result(intent, criterion, evidence_type, {"exit_code": 0})],
    )

    assert evaluation["criterion_satisfied"] is False
    assert evaluation["evidence_records"] == {}
    assert evaluation["adapter_outcomes"][0]["reason_code"] == "EVIDENCE_VALIDATION_FAILED"


def test_run_mismatch_is_rejected_and_cannot_complete():
    contract, intent, criterion = _context()
    results = [
        _result(intent, criterion, name, _value(name), run_id="run-other")
        for name in criterion.required_evidence
    ]

    evaluation = CompletionEvidenceEvaluator().evaluate_criterion(
        intent=intent, contract=contract, criterion_id=criterion.criterion_id, results=results)

    assert evaluation["criterion_satisfied"] is False
    assert evaluation["evidence_records"] == {}
    assert all(item["reason_code"] == "RUN_ID_MISMATCH" for item in evaluation["adapter_outcomes"])
