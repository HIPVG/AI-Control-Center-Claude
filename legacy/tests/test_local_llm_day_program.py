import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend.control.local_llm_day_program import LocalLLMDayProgram
from backend.control.evidence_registry import REGISTRY
from backend.control.day_action_registry import STRATEGIES, assert_coverage
from backend.control.day_git import GitSafetyError
from backend.control.retained_evidence import RetainedEvidenceResolver
from backend.control.solution_catalog import JsonSolutionCatalogStore, RepairEpisodeStore, SolutionCatalog
from backend.control.external_review import (
    CapturedExternalResponse,
    ExternalReviewConfig,
    ExternalReviewCoordinator,
    ResponsesExternalReviewTransport,
    load_external_review_config,
)
from backend.models.local_llm_day import ActionAttempt, AuthorityBlocker, DayIssueClassification, DynamicDayWorkOrder, GapDiagnosis, LocalLLMDayState, LocalLLMDayWorkItem, LocalLLMWorkItemState, RepairEpisode
from backend.models.audit import AuditEventType


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "day-contract"


def test_day_one_checkpoint_template_v2_is_a_new_action_identity():
    strategy = STRATEGIES[(1, "commit_ref")]
    assert strategy.template is not None
    assert strategy.template.template_id == "D1_BASELINE_CHECKPOINT_V2"
    assert strategy.template.template_id != "D1_BASELINE_CHECKPOINT"


def test_day_one_checkpoint_v2_bypasses_old_attempt_once_then_is_suppressed():
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(1)
    contract = runner.snapshot.contract
    inventory = runner._inventory(contract)
    diagnosis = next(item for item in runner._diagnose_gaps(contract, inventory) if item.evidence_type == "commit_ref")
    runner.snapshot.action_attempts.append(ActionAttempt(
        action_fingerprint="old-attempt", strategy_id="D1_COMMIT_REF", criterion_id="d1-regression_baseline",
        evidence_type="commit_ref", input_fingerprint=diagnosis.input_fingerprint,
        outcome="HUMAN_ACTION_REQUIRED", action_template_id="D1_BASELINE_CHECKPOINT",
        failure_reason="UNAPPROVED_SOURCE_PATHS"))
    assert runner._select_registered_action([next(item for item in runner._diagnose_gaps(contract, inventory) if item.evidence_type == "commit_ref")]).evidence_type == "commit_ref"
    runner.snapshot.action_attempts.append(ActionAttempt(
        action_fingerprint=diagnosis.action_fingerprint, strategy_id="D1_COMMIT_REF", criterion_id="d1-regression_baseline",
        evidence_type="commit_ref", input_fingerprint=diagnosis.input_fingerprint,
        outcome="HUMAN_ACTION_REQUIRED", action_template_id="D1_BASELINE_CHECKPOINT_V2",
        failure_reason="UNAPPROVED_SOURCE_PATHS"))
    assert runner._select_registered_action([next(item for item in runner._diagnose_gaps(contract, inventory) if item.evidence_type == "commit_ref")]) is None


def test_approved_scope_retry_is_not_eligible_after_v2_checkpoint_succeeds():
    runner = LocalLLMDayProgram(FIXTURE_ROOT, approved_day_one_snapshot_paths=frozenset({"conftest.py"}))
    runner.smoke(1)
    contract = runner.snapshot.contract
    inventory = runner._inventory(contract)
    diagnosis = next(item for item in runner._diagnose_gaps(contract, inventory) if item.evidence_type == "commit_ref")
    strategy = STRATEGIES[(1, "commit_ref")]
    runner.snapshot.action_attempts.append(ActionAttempt(
        action_fingerprint="scope-blocked", strategy_id="D1_COMMIT_REF", criterion_id=diagnosis.criterion_id,
        evidence_type="commit_ref", input_fingerprint=diagnosis.input_fingerprint,
        outcome="HUMAN_ACTION_REQUIRED", action_template_id="D1_BASELINE_CHECKPOINT_V2",
        failure_reason="UNAPPROVED_SOURCE_PATHS"))
    assert runner._authorized_day_one_scope_retry(diagnosis, strategy)
    runner.snapshot.action_attempts.append(ActionAttempt(
        action_fingerprint="scope-complete", strategy_id="D1_COMMIT_REF", criterion_id=diagnosis.criterion_id,
        evidence_type="commit_ref", input_fingerprint=diagnosis.input_fingerprint,
        outcome="COMPLETE", action_template_id="D1_BASELINE_CHECKPOINT_V2"))
    assert not runner._authorized_day_one_scope_retry(diagnosis, strategy)


def test_read_only_evidence_versions_identical_values_by_observation_fingerprint():
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(1)
    contract = runner.snapshot.contract
    evidence = {"git_head": _record("git_head", _value("git_head"))}

    runner._ingest_legacy_evidence(contract, evidence, provider_id="fixture", source_fingerprint="a" * 64)
    runner._ingest_legacy_evidence(contract, evidence, provider_id="fixture", source_fingerprint="b" * 64)

    records = [record for record in runner.snapshot.evidence_store.values() if record.evidence_type == "git_head"]
    assert len(records) == 2
    assert {record.observation_fingerprint for record in records} == {"a" * 64, "b" * 64}


def test_day_one_stale_read_only_evidence_unblocks_only_the_exact_versioning_repair():
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(1)
    contract = runner.snapshot.contract
    inventory = runner._inventory(contract)
    runner._ingest_legacy_evidence(contract, {"git_head": _record("git_head", _value("git_head"))},
                                    provider_id="fixture", source_fingerprint="a" * 64)
    runner.snapshot.authority_blocker = AuthorityBlocker(
        classification=DayIssueClassification.HUMAN_PRODUCT_DECISION_REQUIRED,
        reason_code="REPAIR_SCOPE_AUTHORITY_REQUIRED", message="stale observation",
        criterion_id="d1-repository_relationship", evidence_type="git_head",
        resolution_strategy="AUTHORIZED_SCOPE", action_template_id="READ_ONLY_COLLECT")
    assert runner._blocker_resolved()
    runner.snapshot.authority_blocker.action_template_id = "D1_BASELINE_CHECKPOINT_V2"
    assert not runner._blocker_resolved()


def _legacy_day_one_blocker_runner():
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(1)
    contract = runner.snapshot.contract
    required = sorted({name for criterion in contract.completion_criteria
                       for name in criterion.required_evidence if name != "commit_ref"})
    runner._ingest_legacy_evidence(contract, _valid_evidence(required), provider_id="fixture", source_fingerprint="a" * 64)
    inventory = runner._inventory(contract)
    runner._evaluate_contract(contract, inventory)
    diagnosis = next(item for item in runner._diagnose_gaps(contract, inventory) if item.evidence_type == "commit_ref")
    runner.snapshot.action_attempts.append(ActionAttempt(
        action_fingerprint="old-attempt", strategy_id="D1_COMMIT_REF", criterion_id=diagnosis.criterion_id,
        evidence_type="commit_ref", input_fingerprint=diagnosis.input_fingerprint,
        outcome="HUMAN_ACTION_REQUIRED", action_template_id="D1_BASELINE_CHECKPOINT",
        failure_reason="UNAPPROVED_SOURCE_PATHS"))
    runner.snapshot.authority_blocker = AuthorityBlocker(
        classification=DayIssueClassification.HUMAN_PRODUCT_DECISION_REQUIRED,
        reason_code="UNAPPROVED_SOURCE_PATHS", message="legacy", criterion_id=diagnosis.criterion_id,
        evidence_type="commit_ref", resolution_strategy="RETAINED_EVIDENCE",
        action_template_id="D1_BASELINE_CHECKPOINT")
    runner.snapshot.state = LocalLLMDayState.HUMAN_ACTION_REQUIRED
    runner.snapshot.stop_reason = "LEGACY_INSUFFICIENT_EVIDENCE_REQUIRES_RESUME"
    return runner


def test_legacy_v1_authority_blocker_routes_to_eligible_v2_once_without_mutating_v1():
    runner = _legacy_day_one_blocker_runner()
    saved = runner.view()
    restored = LocalLLMDayProgram(FIXTURE_ROOT, saved=saved)
    restored._create_day_one_baseline_checkpoint = lambda _contract: {
        "final_result": "COMPLETE", "evidence": {"commit_ref": _record("commit_ref", _value("commit_ref"))}}

    restored.resume()
    restored.join(3)

    attempts = restored.snapshot.action_attempts
    assert attempts[0].action_template_id == "D1_BASELINE_CHECKPOINT"
    assert len([item for item in attempts if item.action_template_id == "D1_BASELINE_CHECKPOINT_V2"]) == 1
    assert LocalLLMDayState.EXECUTING_DAY_WORK.value in restored.snapshot.state_history


@pytest.mark.parametrize("blocker_template, mutate", [
    ("D1_BASELINE_CHECKPOINT_V2", lambda _runner: None),
    ("D1_BASELINE_CHECKPOINT", lambda runner: runner.snapshot.action_attempts.append(ActionAttempt(
        action_fingerprint="v2-attempt", strategy_id="D1_COMMIT_REF", criterion_id="d1-regression_baseline",
        evidence_type="commit_ref", input_fingerprint=next(item for item in runner._diagnose_gaps(runner.snapshot.contract, runner._inventory(runner.snapshot.contract)) if item.evidence_type == "commit_ref").input_fingerprint,
        outcome="HUMAN_ACTION_REQUIRED", action_template_id="D1_BASELINE_CHECKPOINT_V2"))),
])
def test_legacy_blocker_bypass_requires_an_unattempted_replacement_identity(blocker_template, mutate):
    runner = _legacy_day_one_blocker_runner()
    runner.snapshot.authority_blocker.action_template_id = blocker_template
    mutate(runner)
    assert not runner._replacement_retry_is_eligible(runner.snapshot.authority_blocker, runner.snapshot.contract, runner._inventory(runner.snapshot.contract))


def _record(name, value=None, *, passed=True, verified=True):
    return {"evidence_type": name, "value": value if value is not None else {"proof": name}, "source": f"deterministic:{name}", "verified": verified, "validation": {"passed": passed, "validator": f"deterministic:{name}"}}


def _value(name):
    shapes = {
        "git_head": {"branch": "main", "head": "a" * 40, "is_commit": True},
        "commit_ref": {"branch": "main", "head": "a" * 40, "is_commit": True},
        "origin_ref": {"origin_url": "file:///origin.git", "upstream_ref": "origin/main", "upstream_sha": "b" * 40},
        "status_audit": {key: [] for key in ("staged_tracked_paths", "unstaged_tracked_paths", "untracked_paths", "relevant_dirty_paths", "generated_paths")},
        "staging_audit": {"staged_paths": [], "staged_generated_paths": [], "generated_artifacts_not_staged": True},
        "documentation_check": {"checked_files": ["docs/README.md"], "checks": [{"name": "fixture", "passed": True}], "failures": []},
        "test_result": {"commands": [["python", "-m", "pytest"]], "exit_code": 0, "passed": 1, "failed": 0, "deterministic_only": True},
        "source_check": {"checked_paths": ["src/example.py"], "assertions": ["required boundary"]},
        "deterministic_tests": {"commands": [["python", "-m", "pytest"]], "exit_code": 0, "passed": 1, "failed": 0},
        "architecture_check": {"checked_documents": ["docs/architecture.md"], "assertions": ["Python owns facts"]},
        "baseline_ref": {"baseline_sha": "a" * 40, "version": "v0.3.2"},
        "preservation_audit": {"protected_paths": ["results/"], "preserved": True},
        "schema_contract": {"schema_path": "schemas/temporal.json", "schema_version": "v1"},
        "provenance_test": {"commands": [["python", "-m", "pytest"]], "exit_code": 0, "passed": 1, "failed": 0},
    }
    return shapes.get(name, {"proof": name})


def _valid_evidence(required):
    return {name: _record(name, _value(name)) for name in required}


def _planner(contract, _inventory):
    return [LocalLLMDayWorkItem(item_id=f"task-{criterion.criterion_id}", title="bounded task", objective=criterion.statement, kind="ENGINE_WORK_ORDER", engine_task_id="SAFE-FIXTURE", criterion_ids=[criterion.criterion_id]) for criterion in contract.completion_criteria if criterion.criterion_id in contract.remaining_gaps][:3]


def _valid_executor(work_order):
    strategy = next(s for s in STRATEGIES.values() if s.strategy_id == work_order["strategy_id"])
    required = strategy.template.post_action_evidence_types
    return {"final_result": "COMPLETE", "evidence": _valid_evidence(required)}


def _write_retained_day_two_fixture(root):
    baseline_config = root / "config" / "decision-reasoning-prototype.json"
    baseline_config.parent.mkdir(parents=True)
    baseline_config.write_text('{"version":"0.3.2"}', encoding="utf-8")
    baseline_hash = hashlib.sha256(baseline_config.read_bytes()).hexdigest()
    (root / "config" / "decision-reasoning-v0.4.json").write_text(json.dumps({
        "version": "0.4", "baseline_config_sha256": baseline_hash,
        "action_gate": {"version": "FEASIBLE_RELEVANT_ACTION_GATE-v1", "require_feasible": True, "require_relevant_to_active_verified_issue": True},
    }), encoding="utf-8")
    baseline_manifest = root / "results" / "decision-reasoning-prototype" / "DRAP-baseline" / "manifest.json"
    baseline_manifest.parent.mkdir(parents=True)
    baseline_manifest.write_text(json.dumps({"run_id": "DRAP-baseline", "status": "COMPLETED", "dry_run": False, "config_version": "0.3.2", "config_sha256": baseline_hash}), encoding="utf-8")
    run = root / "results" / "decision-reasoning-v0.4" / "DRAP-retained"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({
        "run_id": "DRAP-retained", "status": "COMPLETED", "dry_run": False, "config_version": "0.4", "config_sha256": "c" * 64,
        "action_gate": {"version": "FEASIBLE_RELEVANT_ACTION_GATE-v1", "enforced": True},
    }), encoding="utf-8")
    (run / "summary.json").write_text(json.dumps({"action_gate": {"all_permitted_actions_feasible_and_relevant": True}}), encoding="utf-8")
    (run / "validation.jsonl").write_text(json.dumps({
        "effect_mapping_complete": True, "action_precondition_failure_count": 0,
        "hard_constraint_violation_count": 0, "fabricated_fact_count": 0,
        "provenance_error_count": 0,
    }) + "\n", encoding="utf-8")
    return run


def _write_retained_day_four_fixture(root):
    config = root / "config" / "decision-generalization-benchmark.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"version": "0.1", "architecture_version": "DRAP-v0.3.2"}), encoding="utf-8")
    config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
    run = root / "results" / "decision-generalization" / "DAGB-retained"
    (run / "freeze").mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps({
        "run_id": "DAGB-retained", "status": "COMPLETED", "dry_run": False,
        "benchmark_version": "DAGB-v0.1", "architecture_version": "DRAP-v0.3.2",
        "config_sha256": config_hash, "architecture_frozen": True,
        "architecture_freeze_verified": True,
    }), encoding="utf-8")
    (run / "summary.json").write_text(json.dumps({
        "flags": {name: True for name in ("HOLDOUT_PIPELINE_STABLE", "METAMORPHIC_INVARIANCE_STABLE", "NO_CASE_HARDCODING", "NO_ORACLE_LEAKAGE")},
        "architecture_freeze_verified": True, "holdout_case_count": 6, "metamorphic_pair_count": 3,
    }), encoding="utf-8")
    (run / "freeze" / "freeze-verification.json").write_text(json.dumps({"unchanged": True, "changed_files": []}), encoding="utf-8")
    (run / "freeze" / "frozen-file-sha256-before.json").write_text(json.dumps({"scripts/eval.py": "a" * 64}), encoding="utf-8")
    cases = ("GH-001", "GH-001-M", "GH-002", "GH-002-M", "GH-003", "GH-003-M")
    (run / "validation.jsonl").write_text("".join(json.dumps({"run_id": "DAGB-retained", "case_id": case, "failure_stage": "PLAN_VALIDATION"}) + "\n" for case in cases), encoding="utf-8")
    pairs = ("GH-001", "GH-002", "GH-003")
    (run / "metamorphic-definition-validation.json").write_text(json.dumps([{ "pair_id": pair, "valid": True} for pair in pairs]), encoding="utf-8")
    (run / "metamorphic-metrics.json").write_text(json.dumps([{ "pair_id": pair, "invariance_status": "STABLE"} for pair in pairs]), encoding="utf-8")
    (run / "oracle-leakage-scan.json").write_text(json.dumps({"status": "PASS", "finding_count": 0}), encoding="utf-8")
    (run / "case-hardcode-scan.json").write_text(json.dumps({"status": "PASS", "case_id_findings": [], "known_literal_findings": []}), encoding="utf-8")
    return run


def _write_retained_day_three_pair(root):
    """Create a minimal real-file pair; the collector must derive, not trust, it."""
    runs = {}
    for label, version in (("v032", "0.3.2"), ("v04", "0.4")):
        run = root / "results" / "day3-fixed-pair" / label / f"DRAP-{label}"
        case = run / "cases" / "DR-005"
        case.mkdir(parents=True)
        manifest = {
            "run_id": run.name, "status": "COMPLETED", "dry_run": False, "config_version": version,
            "planned_llm_calls": 2, "actual_llm_calls": 2,
            "models": {"semantic_abstractor": "fixture-model", "cross_functional_reasoner": "fixture-model"},
            "execution": {"temperature": 0, "seed": 42, "context_length": 4096, "retry": False,
                          "abstraction_max_output_tokens": 16, "reasoner_max_output_tokens": 16, "parallel": False},
            "automatic_retry": False,
        }
        if version == "0.4":
            manifest["action_gate"] = {"enforced": True}
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (case / "raw-case.json").write_text(json.dumps({"case": "fixed"}), encoding="utf-8")
        (case / "canonical-business-state.json").write_text(json.dumps({"facts": ["stable"]}), encoding="utf-8")
        validation = {"run_id": run.name, "case_id": "DR-005", "fact_status": "PASS", "fact_count": 1,
                      "valid_plan_count": 2 if version == "0.4" else 1, "invalid_plan_count": 0,
                      "actions_total": 2, "blocking_issue_coverage": 1.0, "mandatory_issue_coverage": 1.0,
                      "failure_stage": "NONE"}
        (run / "validation.jsonl").write_text(json.dumps(validation) + "\n", encoding="utf-8")
        metrics = [
            {"run_id": run.name, "stage": "abstraction", "elapsed_seconds": 1, "prompt_tokens": 2, "output_tokens": 3},
            {"run_id": run.name, "stage": "reasoner", "elapsed_seconds": 4, "prompt_tokens": 5, "output_tokens": 6},
        ]
        (run / "metrics.jsonl").write_text("".join(json.dumps(row) + "\n" for row in metrics), encoding="utf-8")
        runs[label] = run
    return runs


def test_retained_day_two_evidence_is_typed_complete_and_fails_closed(tmp_path):
    run = _write_retained_day_two_fixture(tmp_path)
    resolver = RetainedEvidenceResolver(tmp_path)
    evidence = resolver.resolve(2)
    assert set(evidence) == {"source_check", "deterministic_tests", "architecture_check", "test_result", "baseline_ref", "preservation_audit"}
    assert all(REGISTRY.validate(name, record) for name, record in evidence.items())
    assert resolver.resolve(4) == {}
    runner = LocalLLMDayProgram(tmp_path, retained_evidence_resolver=resolver)
    contract = runner._load_contracts()[2]
    runner._evaluate_contract(contract, runner._inventory(contract))
    assert not contract.remaining_gaps
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    manifest["status"] = "FAILED"
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert RetainedEvidenceResolver(tmp_path).resolve(2) == {}


def test_retained_day_four_cross_model_evidence_is_typed_complete_and_fails_closed(tmp_path):
    run = tmp_path / "results" / "day4-cross-model" / "EXP-20260923T151059-424c537a66"
    run.mkdir(parents=True)
    cases = ["PC-001-A", "PC-001-C", "PC-003-A", "PC-003-C"]
    inputs = {case: f"hash-{case}" for case in cases}
    profile = {"id": "week1-day4-cross-family", "cases": cases, "temperature": 0, "seed": 42,
               "context_length": 8192, "max_output_tokens": 1024, "reasoning_mode": "disabled", "repeat": 1}
    models = [{"id": "phi4-14b-q4", "runtime_model_name": "phi4:14b"},
              {"id": "qwen3-14b-q4", "runtime_model_name": "qwen3-14b-q4:latest"}]
    (run / "manifest.json").write_text(json.dumps({"run_id": run.name, "status": "completed", "dry_run": False,
        "profile": profile, "selected_models": models, "selected_case_ids": cases}), encoding="utf-8")
    comparison_models = []
    rows = []
    for index, model in enumerate(models):
        child = run / "runs" / f"PCSMOKE-20260923T151{index:03d}Z-fixture"
        child.mkdir(parents=True)
        (child / "manifest.json").write_text(json.dumps({"run_id": child.name, "model": model["runtime_model_name"],
            "status": "completed", "success_count": 4, "failed_count": 0, "input_sha256": inputs}), encoding="utf-8")
        comparison_models.append({"model_id": model["id"], "performance": {"average_elapsed_seconds": 1}, "success_count": 4})
        rows.extend({"model_id": model["id"], "case_id": case, "status": "success",
                     "raw_response_metadata": {"done_reason": "length" if model["id"] == "qwen3-14b-q4" and case == "PC-001-A" else "stop"}}
                    for case in cases)
    (run / "comparison.json").write_text(json.dumps({"models": comparison_models}), encoding="utf-8")
    (run / "responses.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    resolver = RetainedEvidenceResolver(tmp_path)
    evidence = resolver.resolve(4)
    assert set(evidence) == {"cross_model_baseline_artifact", "cross_model_comparison_artifact", "cross_model_condition", "cross_model_validation", "cross_model_metrics", "cross_model_quality_assessment"}
    assert all(REGISTRY.validate(name, record) for name, record in evidence.items())
    runner = LocalLLMDayProgram(tmp_path, retained_evidence_resolver=resolver)
    contract = runner._load_contracts()[4]
    runner._evaluate_contract(contract, runner._inventory(contract))
    assert not contract.remaining_gaps
    broken = json.loads((run / "runs" / "PCSMOKE-20260923T151000Z-fixture" / "manifest.json").read_text(encoding="utf-8"))
    broken["input_sha256"] = {"PC-001-A": "different"}
    (run / "runs" / "PCSMOKE-20260923T151000Z-fixture" / "manifest.json").write_text(json.dumps(broken), encoding="utf-8")
    assert RetainedEvidenceResolver(tmp_path).resolve(4) == {}


def test_retained_day_three_pair_registers_typed_evidence_without_mutation(tmp_path):
    runs = _write_retained_day_three_pair(tmp_path)
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
              for run in runs.values() for path in run.rglob("*") if path.is_file()}
    resolver = RetainedEvidenceResolver(tmp_path)
    evidence = resolver.resolve(3)
    assert set(evidence) == {"v032_artifact", "v04_artifact", "condition_record", "comparison_metrics", "failure_policy"}
    assert all(REGISTRY.validate(name, record) for name, record in evidence.items())
    condition = evidence["condition_record"]["value"]
    assert set(condition["raw_case_hashes"]) == set(condition["fact_layer_hashes"]) == {"DR-005"}
    assert before == {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                      for run in runs.values() for path in run.rglob("*") if path.is_file()}

    audit_events = []
    runner = LocalLLMDayProgram(
        FIXTURE_ROOT, retained_evidence_resolver=resolver,
        audit=lambda _task, event, details: audit_events.append((AuditEventType(event), details)),
    )
    runner.smoke(3)
    history_before = list(runner.snapshot.state_history)
    result = runner.register_retained_evidence()
    assert "error_code" not in result
    assert result["registered_evidence_types"] == sorted(evidence)
    assert not runner.snapshot.contract.remaining_gaps
    assert runner.snapshot.state_history == history_before
    assert len(runner.snapshot.evidence_store) == 5
    assert audit_events[-1][0] is AuditEventType.RETAINED_EVIDENCE_REGISTERED


def test_retained_day_three_pair_rejects_mismatched_provenance(tmp_path):
    runs = _write_retained_day_three_pair(tmp_path)
    raw = runs["v04"] / "cases" / "DR-005" / "raw-case.json"
    raw.write_text(json.dumps({"case": "different"}), encoding="utf-8")
    assert RetainedEvidenceResolver(tmp_path).resolve(3) == {}


def test_day_one_test_evidence_cache_reuses_only_a_successful_unchanged_key(tmp_path, monkeypatch):
    test_path = tmp_path / "tests" / "test_process_consistency_smoke.py"
    test_path.parent.mkdir()
    test_path.write_text("def test_fixture(): pass\n", encoding="utf-8")
    calls = []
    def successful_run(*_args, **_kwargs):
        calls.append("run")
        return subprocess.CompletedProcess([], 0, "1 passed in 0.01s", "")
    monkeypatch.setattr("backend.control.local_llm_day_program.subprocess.run", successful_run)
    runner = LocalLLMDayProgram(tmp_path)
    first = runner._day_one_test_result(cache_key="unchanged")
    second = runner._day_one_test_result(cache_key="unchanged")
    assert first["cache_hit"] is False and second["cache_hit"] is True
    assert calls == ["run"]
    assert runner._day_one_test_result(cache_key="changed")["cache_hit"] is False
    assert calls == ["run", "run"]


def test_pytest_temporary_directory_is_repository_owned(tmp_path):
    assert tmp_path.is_relative_to(Path(__file__).parents[1] / ".pytest-tmp")


@pytest.mark.parametrize("day", [6])
def test_generic_contract_requires_explicit_validated_evidence(day):
    runner = LocalLLMDayProgram(FIXTURE_ROOT, planner=_planner, work_order_executor=_valid_executor)
    runner.start(day)
    runner.join(3)
    assert runner.view()["state"] == LocalLLMDayState.COMPLETE.value


def test_completed_task_with_engine_tokens_does_not_satisfy_criterion():
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(6)
    criterion = runner.snapshot.contract.completion_criteria[0]
    criterion.evidence = {"engine_task_id": "PC-001-A", "run_id": "run-1", "postcheck_result": "PASS"}
    runner._evaluate_contract(runner.snapshot.contract, {})
    assert not criterion.satisfied


def test_partial_unrelated_empty_and_unverified_evidence_fail_closed():
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(2)
    criterion = next(value for value in runner.snapshot.contract.completion_criteria if value.criterion_id == "d2-deterministic_validation")
    criterion.evidence = {"test_result": _record("test_result", _value("test_result"))}
    runner._evaluate_contract(runner.snapshot.contract, {})
    assert not criterion.satisfied
    criterion.evidence = {"git_head": _record("git_head", _value("git_head")), "origin_ref": _record("origin_ref", _value("origin_ref"))}
    runner._evaluate_contract(runner.snapshot.contract, {})
    assert not criterion.satisfied
    criterion.evidence = {"architecture_check": _record("architecture_check", {}), "test_result": _record("test_result", _value("test_result"), verified=False)}
    runner._evaluate_contract(runner.snapshot.contract, {})
    assert not criterion.satisfied


def test_complete_typed_evidence_satisfies_only_matching_criterion():
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(6)
    criterion = runner.snapshot.contract.completion_criteria[0]
    runner._ingest_legacy_evidence(runner.snapshot.contract, _valid_evidence(criterion.required_evidence), provider_id="test-adapter", source_fingerprint="a" * 64)
    runner._evaluate_contract(runner.snapshot.contract, {})
    assert criterion.satisfied
    assert set(criterion.required_evidence).issubset(criterion.evidence)


@pytest.mark.parametrize("repair_produces_evidence", [True, False])
def test_missing_evidence_is_diagnosed_before_an_unchanged_action_fails_safe(repair_produces_evidence):
    calls = []
    executions = []

    def planner(contract, _inventory):
        calls.append(contract.remaining_gaps[:])
        return _planner(contract, _inventory)

    class NoProposal:
        def propose(self, **_kwargs):
            return None

    def executor(order):
        executions.append(order["kind"])
        if order["kind"] == "DAY_ACTION_TEMPLATE":
            return {"final_result": "COMPLETE", "evidence": {}}
        assert order["kind"] == "CODEX_EXPERT_SOLVER"
        names = [name for criterion in runner.snapshot.contract.completion_criteria for name in criterion.required_evidence]
        return {"final_result": "COMPLETE", "verification_passed": True,
                "evidence": _valid_evidence(names) if repair_produces_evidence else {}}

    runner = LocalLLMDayProgram(FIXTURE_ROOT, planner=planner, work_order_executor=executor, repair_builder=NoProposal())
    runner._bounded_repair_files = lambda _item: {"scripts/eval/temporal_state.py": "before"}
    runner.start(6)
    runner.join(3)
    result = runner.view()
    assert result["state"] == ("COMPLETE" if repair_produces_evidence else "HUMAN_ACTION_REQUIRED")
    assert result["report"]["result"] != "DAY_NO_SAFE_ACTION"
    assert executions == ["DAY_ACTION_TEMPLATE", "CODEX_EXPERT_SOLVER"]
    assert result["gap_diagnoses"]
    assert all(item["classification"] == "ENGINEERING_REPAIR" for item in result["gap_diagnoses"])
    if not repair_produces_evidence:
        assert result["authority_blocker"]["reason_code"] == "REPAIR_SCOPE_AUTHORITY_REQUIRED"
    assert calls == []
    assert len(result["action_attempts"]) == 1


def test_planner_cannot_expand_authority():
    def unsafe_planner(_contract, _inventory):
        return [LocalLLMDayWorkItem(item_id="unsafe", title="unsafe", objective="unsafe", kind="SHELL", criterion_ids=["not-a-criterion"])]

    runner = LocalLLMDayProgram(FIXTURE_ROOT, planner=unsafe_planner)
    runner.start(2)
    runner.join(3)
    assert runner.view()["report"]["result"] == "PREREQUISITE_DAY_REQUIRED"


def test_model_quality_finding_is_preserved_and_never_offers_repair(tmp_path):
    test_production_research_uses_admitted_condition_and_retains_outcome(tmp_path, "MODEL_QUALITY_FINDING")


def test_static_engine_failure_cannot_offer_repair_and_go_without_an_interrupted_episode():
    class Proposal:
        diagnosis = "isolated harness defect"
        edits = ()

    class Builder:
        def propose(self, **_kwargs):
            return Proposal()

    def defect_executor(work_order):
        if work_order["kind"] == "ENGINE_WORK_ORDER":
            return {"final_result": "FAILED", "issue_classification": "IMPLEMENTATION_DEFECT", "failure_excerpt": "safe fixture failed"}
        assert work_order["kind"] == "LOCAL_LLM_COUNTERMEASURE"
        return {"final_result": "COMPLETE", "evidence": {}}

    runner = LocalLLMDayProgram(FIXTURE_ROOT, planner=_planner, work_order_executor=defect_executor, repair_builder=Builder())
    runner._bounded_repair_files = lambda _item: {"tests/safe.py": "assert False"}
    runner.start(2)
    runner.join(3)
    result = runner.view()
    assert result["recommended_action"]["action_id"] in {"SHOW_FAILURE", "SHOW_REQUIRED_ACTION"}
    assert runner.repair_and_go()["error_code"] == "AUTONOMOUS_REPAIR_NOT_AVAILABLE"


def test_local_rejection_runs_expert_and_teaches_next_episode(tmp_path, monkeypatch):
    engine, root, writer, builder = _repair_engine(tmp_path)
    runner = engine.local_llm_day_program
    catalog_path = tmp_path / "verified-catalog.json"
    runner.solution_catalog = SolutionCatalog(JsonSolutionCatalogStore(catalog_path))
    accepted = []
    accept = runner._accept_repair
    def observe_accept(item, value, episode, **kwargs):
        accept(item, value, episode, **kwargs)
        accepted.append({"item_id": item.item_id, "result_keys": list(value),
                         "adapter_evidence": sorted(value.get("evidence", {})),
                         "persisted_evidence": sorted(item.evidence.get("evidence", {})),
                         "repair_diagnoses": [d.model_dump(mode="json") for d in runner.snapshot.gap_diagnoses
                                              if d.classification == DayIssueClassification.ENGINEERING_REPAIR]})
    monkeypatch.setattr(runner, "_accept_repair", observe_accept)
    engine.start_local_llm_day(6)
    engine.local_llm_day_program.join(120)
    result = engine.local_llm_day_status()
    (tmp_path / "repair-e2e.json").write_text(json.dumps({"snapshot": result, "roles": writer.roles,
                                                        "accepted": accepted}, indent=2), encoding="utf-8")
    assert result["state"] == "COMPLETE", (result["report"], result["work_items"], writer.roles)
    episode = engine.local_llm_day_program.repair_episode_store.get(result["repair_episode_ids"][0])
    assert len(episode.proposal_attempts) == 3
    assert episode.final_outcome == "CODEX_VERIFIED"
    assert all(p.outcome == "REJECTED" for p in episode.proposal_attempts)
    assert episode.verification_result == "PASS"
    assert writer.roles == ["normal", "local", "local", "local", "expert"]
    expected_evidence = {"schema_contract", "source_check", "provenance_test", "deterministic_tests", "architecture_check"}
    assert len(accepted) == 1
    assert set(accepted[0]["adapter_evidence"]) == expected_evidence
    assert set(accepted[0]["persisted_evidence"]) == expected_evidence
    assert accepted[0]["result_keys"].index("evidence") >= 20
    assert accepted[0]["repair_diagnoses"]
    assert set(r.evidence_type for r in runner.snapshot.evidence_store.values()) == expected_evidence
    assert all(runner._evidence_record_valid(r.evidence_type, r) for r in runner.snapshot.evidence_store.values())
    assert all(c.satisfied and c.evidence_record_ids for c in runner.snapshot.contract.completion_criteria)
    assert result["authority_blocker"] is None
    assert result["state_history"].count("REPAIR_SUPERVISOR") == 1
    catalog = engine.local_llm_day_program.solution_catalog
    assert catalog.entries()[0].source == "CODEX_VERIFIED"
    assert (root / "scripts/eval/temporal_state.py").read_text() == "def valid_time(): return False\n"
    second, _, writer2, builder2 = _repair_engine(tmp_path / "second", root=root)
    second.local_llm_day_program.solution_catalog = SolutionCatalog(JsonSolutionCatalogStore(catalog_path))
    second.start_local_llm_day(6)
    second.local_llm_day_program.join(120)
    assert second.local_llm_day_status()["state"] == "COMPLETE", second.local_llm_day_status()
    assert builder2.requests[0]["repair_knowledge"]
    assert writer2.roles == ["normal", "local", "local", "local", "expert"]
    assert len(builder2.requests) == 3
    assert second.local_llm_day_program.solution_catalog.entries()[0].uses > 0
    (tmp_path / "repair-e2e.json").write_text(json.dumps({"snapshot": result, "roles": writer.roles,
        "accepted": accepted, "second_snapshot": second.local_llm_day_status(), "second_roles": writer2.roles,
        "second_repair_knowledge": builder2.requests[0]["repair_knowledge"]}, indent=2), encoding="utf-8")


def test_repair_deadline_escalates_without_waiting(tmp_path):
    from backend.models.local_llm_day import RepairEpisode
    engine, root, writer, builder = _repair_engine(tmp_path)
    runner = engine.local_llm_day_program
    runner.smoke(6)
    template = STRATEGIES[(6, "source_check")].template
    order = DynamicDayWorkOrder(task_id="day-6-d6-temporal-state-design",
        allowed_files=list(template.allowed_output_scope), context_files=list(template.context_scope),
        acceptance_test_files=["tests/test_temporal_state.py"])
    item = LocalLLMDayWorkItem(item_id="repair-D6_SOURCE_CHECK", title="interrupted repair",
        objective="repair deterministic failure", kind="DYNAMIC_ENGINEERING_WORK", dynamic_work_order=order,
        criterion_ids=[runner.snapshot.contract.completion_criteria[0].criterion_id],
        contract_day=6, contract_version=runner.snapshot.contract.version, state=LocalLLMWorkItemState.FAILED,
        evidence={"failure_excerpt": "fixed temporal assertion"})
    runner.snapshot.work_items = [item]
    episode = RepairEpisode(episode_id="persisted-deadline", project_id="local_llm_lab", day=6,
        work_item_id=item.item_id, failure_class=DayIssueClassification.ENGINEERING_REPAIR,
        failure_fingerprint=runner._failure_fingerprint(item, "fixed temporal assertion"),
        failure_excerpt="fixed temporal assertion", started_at_epoch=0, repair_deadline_epoch=300,
        contract_version=runner.snapshot.contract.version,
        scope_fingerprint=runner._text_fingerprint(order.model_dump_json()))
    path = tmp_path / "episodes.json"
    runner.repair_episode_store = RepairEpisodeStore(path)
    runner.repair_episode_store.save(episode)
    runner.snapshot.repair_episode_ids = [episode.episode_id]
    runner.snapshot.state = LocalLLMDayState.REPAIR_SUPERVISOR
    runner.snapshot.contract_fingerprint = runner._contract_fingerprint(runner.snapshot.contract)
    restored = LocalLLMDayProgram(root, saved=runner.view(),
        repair_episode_store=RepairEpisodeStore(path), repair_builder=builder,
        work_order_executor=engine._execute_local_llm_day_work_order, clock=lambda: 301.0)
    engine.local_llm_day_program = restored
    assert restored.view()["enabled_controls"]["repair_and_go"]
    restored.repair_and_go()
    restored.join(120)
    assert restored.view()["state"] == "COMPLETE", restored.view()
    assert not builder.requests and writer.roles == ["expert"]
    retained = restored.repair_episode_store.get(episode.episode_id)
    assert retained.repair_deadline_epoch == 300
    assert retained.final_outcome == "CODEX_VERIFIED"


class _NoExternalLocalProposal:
    def propose(self, **_kwargs):
        return None


class _FixtureExternalReviewer:
    def __init__(self, response):
        self.response = response
        self.submissions = []

    def submit(self, config, context, package, state):
        self.submissions.append({
            "transport": config.transport,
            "context_fingerprint": context.fingerprint,
            "previous_response_id": state.previous_response_id if state else None,
            "package": package.model_dump(mode="json"),
        })
        return CapturedExternalResponse(
            conversation_id="fixture-review", response_id=f"fixture-{len(self.submissions)}",
            submitted_at="2026-09-23T00:00:00+00:00",
            response_received_at="2026-09-23T00:00:01+00:00", response_text=self.response,
        )


class _FakeResponsesClient:
    def __init__(self, responses):
        self.responses = self
        self.output = list(responses)
        self.calls = []

    def create(self, **request):
        self.calls.append(request)
        result = self.output.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _AuthenticationError(Exception):
    pass


class _ModelAccessError(Exception):
    pass


class _QuotaError(Exception):
    status_code = 429


def _external_repair_item(runner):
    template = STRATEGIES[(6, "source_check")].template
    order = DynamicDayWorkOrder(task_id="day-6-d6-temporal-state-design",
        allowed_files=list(template.allowed_output_scope), context_files=list(template.context_scope),
        acceptance_test_files=["tests/test_temporal_state.py"])
    diagnosis = GapDiagnosis(criterion_id=runner.snapshot.contract.completion_criteria[0].criterion_id,
        classification=DayIssueClassification.ENGINEERING_REPAIR,
        reason="Controlled adapter repair failed.", failure_reason="FIXTURE_ADAPTER_FAILURE",
        required_evidence=["source_check"], input_fingerprint="a" * 64,
        action_fingerprint="b" * 64, evidence_type="source_check", strategy_id="D6_SOURCE_CHECK")
    item = LocalLLMDayWorkItem(item_id="repair-D6_SOURCE_CHECK", title="controlled external repair",
        objective=diagnosis.reason, kind="DYNAMIC_ENGINEERING_WORK", dynamic_work_order=order,
        criterion_ids=[diagnosis.criterion_id], contract_day=6, contract_version=runner.snapshot.contract.version,
        state=LocalLLMWorkItemState.FAILED, evidence={"failure_excerpt": "adapter token='sk-abcdefghijklmnop' failed"})
    runner.snapshot.gap_diagnoses = [diagnosis]
    return item, diagnosis


def test_external_review_guidance_runs_only_the_existing_bounded_builder(tmp_path):
    engine, root, _writer, _builder = _repair_engine(tmp_path)
    runner = engine.local_llm_day_program
    runner.smoke(6)
    item, diagnosis = _external_repair_item(runner)
    response = json.dumps({"status": "REPAIR_GUIDANCE", "diagnosis": "Adapter omitted typed evidence.",
                           "proposed_repair": "Repair only the temporal adapter and preserve current evidence contract.",
                           "relevant_files": ["scripts/eval/temporal_state.py"],
                           "expected_behavior": "The configured temporal test passes and typed evidence is returned.",
                           "suggested_verification": ["tests/test_temporal_state.py"], "cautions": ["Do not modify acceptance criteria."]})
    transport = _FixtureExternalReviewer(response)
    runner.external_review = ExternalReviewCoordinator(root,
        config=ExternalReviewConfig(transport="FIXTURE"), transport=transport)
    runner.repair_builder = _NoExternalLocalProposal()
    calls = []
    def executor(order):
        calls.append(order)
        if order["kind"] == "CODEX_EXPERT_SOLVER":
            return {"final_result": "FAILED", "error_code": "EXPERT_FIXTURE_FAILED", "stderr": "expert failed"}
        assert order["kind"] == "EXTERNAL_REVIEW_BUILDER"
        assert order["dynamic_work_order"]["allowed_files"] == item.dynamic_work_order.allowed_files
        assert order["external_review"]["relevant_files"] == ["scripts/eval/temporal_state.py"]
        (root / "scripts/eval/temporal_state.py").write_text("def valid_time(): return True\n", encoding="utf-8")
        completed = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_temporal_state.py"], cwd=root,
                                   capture_output=True, text=True, encoding="utf-8")
        assert completed.returncode == 0, completed.stdout + completed.stderr
        names = [name for criterion in runner.snapshot.contract.completion_criteria for name in criterion.required_evidence]
        return {"final_result": "COMPLETE", "verification_passed": True, "evidence": _valid_evidence(names)}
    runner.work_order_executor = executor

    assert runner._supervise_repair(item, runner.snapshot.contract)
    episode = runner.repair_episode_store.get(runner.snapshot.repair_episode_ids[-1])
    assert episode.final_outcome == "EXTERNAL_REVIEW_VERIFIED"
    assert episode.external_review_outcome == "GUIDANCE_RECEIVED"
    assert [call["kind"] for call in calls] == ["CODEX_EXPERT_SOLVER", "EXTERNAL_REVIEW_BUILDER"]
    assert transport.submissions[0]["transport"] == "FIXTURE"
    artifact = Path(episode.external_review_artifact)
    persisted = json.loads(artifact.read_text(encoding="utf-8"))
    assert persisted["package"]["fingerprint"] == episode.external_review_fingerprint
    assert "sk-abcdefghijklmnop" not in json.dumps(persisted)
    assert (root / "scripts/eval/temporal_state.py").read_text(encoding="utf-8") == "def valid_time(): return True\n"
    runner._ingest_legacy_evidence(runner.snapshot.contract, item.evidence["evidence"], provider_id="external-review", source_fingerprint=diagnosis.input_fingerprint)
    runner._evaluate_contract(runner.snapshot.contract, runner._inventory(runner.snapshot.contract))
    assert not runner.snapshot.contract.remaining_gaps


def test_external_review_scope_expansion_never_invokes_builder(tmp_path):
    engine, root, _writer, _builder = _repair_engine(tmp_path)
    runner = engine.local_llm_day_program
    runner.smoke(6)
    item, _diagnosis = _external_repair_item(runner)
    response = json.dumps({"status": "REPAIR_GUIDANCE", "diagnosis": "Try another subsystem.",
                           "proposed_repair": "Change a non-approved file.", "relevant_files": ["backend/app.py"],
                           "expected_behavior": "Expanded behavior.", "suggested_verification": ["tests/test_temporal_state.py"], "cautions": []})
    transport = _FixtureExternalReviewer(response)
    runner.external_review = ExternalReviewCoordinator(root,
        config=ExternalReviewConfig(transport="FIXTURE"), transport=transport)
    runner.repair_builder = _NoExternalLocalProposal()
    calls = []
    runner.work_order_executor = lambda order: calls.append(order) or {"final_result": "FAILED", "error_code": "EXPERT_FIXTURE_FAILED"}

    assert not runner._supervise_repair(item, runner.snapshot.contract)
    episode = runner.repair_episode_store.get(runner.snapshot.repair_episode_ids[-1])
    assert episode.external_review_outcome == "HUMAN_DECISION_REQUIRED"
    assert [call["kind"] for call in calls] == ["CODEX_EXPERT_SOLVER"]


def _external_review_package(runner, coordinator, item, diagnosis, *, excerpt="controlled failure"):
    order = item.dynamic_work_order
    return coordinator.build_package(
        selected_day=runner.snapshot.contract.day, contract_version=runner.snapshot.contract.version,
        contract_fingerprint=runner.snapshot.contract_fingerprint or runner._contract_fingerprint(runner.snapshot.contract),
        criterion_id=diagnosis.criterion_id, evidence_type=diagnosis.evidence_type,
        gap_diagnosis=diagnosis.model_dump(mode="json"), failed_action={"task_id": order.task_id},
        allowed_files=order.allowed_files, context_files=order.context_files,
        acceptance_test_files=order.acceptance_test_files, failure_excerpt=excerpt,
        stdout_excerpt="", stderr_excerpt="", git_summary=runner._inventory(runner.snapshot.contract),
        previous_attempts=[], rejection_feedback=[], catalog_matches=[],
        expert_solver_outcome="FAILED", deterministic_verification_failure="fixture failure",
    )


def _review_json(*, files=None):
    return json.dumps({
        "status": "REPAIR_GUIDANCE", "diagnosis": "Typed evidence adapter omitted a record.",
        "proposed_repair": "Repair only the in-scope temporal adapter.",
        "relevant_files": files or ["scripts/eval/temporal_state.py"],
        "expected_behavior": "The configured temporal test returns typed evidence.",
        "suggested_verification": ["tests/test_temporal_state.py"], "cautions": [],
        "rejection_reason": None,
    })


def test_responses_transport_reuses_persisted_context_and_conversation(tmp_path, monkeypatch):
    engine, root, _writer, _builder = _repair_engine(tmp_path)
    runner = engine.local_llm_day_program
    runner.smoke(6)
    item, diagnosis = _external_repair_item(runner)
    client = _FakeResponsesClient([
        {"id": "resp-1", "conversation": {"id": "conv-1"}, "output_text": _review_json()},
        {"id": "resp-2", "conversation": {"id": "conv-1"}, "output_text": _review_json()},
    ])
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    coordinator = ExternalReviewCoordinator(
        root, config=ExternalReviewConfig(transport="OPENAI_RESPONSES"),
        transport=ResponsesExternalReviewTransport(client_factory=lambda: client),
    )
    first = coordinator.request_review(_external_review_package(runner, coordinator, item, diagnosis))
    second = coordinator.request_review(_external_review_package(runner, coordinator, item, diagnosis, excerpt="next controlled failure"))

    assert first.status == second.status == "GUIDANCE_RECEIVED"
    assert first.context_pack_fingerprint == second.context_pack_fingerprint == coordinator.context.fingerprint
    assert len(client.calls) == 2, runner.view()
    assert "previous_response_id" not in client.calls[0]
    assert client.calls[1]["previous_response_id"] == "resp-1"
    assert "Evidence Store validation" in client.calls[0]["instructions"]
    assert client.calls[1]["instructions"] == client.calls[0]["instructions"]
    assert client.calls[0]["text"]["format"]["strict"] is True
    state = json.loads((root / "state/external-review/reviewer-conversation.json").read_text(encoding="utf-8"))
    assert state["conversation_id"] == "conv-1"
    assert state["previous_response_id"] == "resp-2"


def test_responses_transport_uses_configured_fallback_only_for_model_access(tmp_path, monkeypatch):
    engine, root, _writer, _builder = _repair_engine(tmp_path)
    runner = engine.local_llm_day_program
    runner.smoke(6)
    item, diagnosis = _external_repair_item(runner)
    client = _FakeResponsesClient([
        _ModelAccessError("model unavailable"),
        {"id": "resp-fallback", "conversation": {"id": "conv-1"}, "output_text": _review_json()},
    ])
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    coordinator = ExternalReviewCoordinator(
        root, config=ExternalReviewConfig(transport="OPENAI_RESPONSES", fallback_model="gpt-5.6-terra"),
        transport=ResponsesExternalReviewTransport(client_factory=lambda: client),
    )
    artifact = coordinator.request_review(_external_review_package(runner, coordinator, item, diagnosis))

    assert artifact.status == "GUIDANCE_RECEIVED"
    assert artifact.captured_response.model == "gpt-5.6-terra"
    assert [call["model"] for call in client.calls] == ["gpt-5.6-sol", "gpt-5.6-terra"]


def test_responses_auth_failure_allows_one_operator_resume_without_source_change(tmp_path, monkeypatch):
    engine, root, _writer, _builder = _repair_engine(tmp_path)
    runner = engine.local_llm_day_program
    runner.smoke(6)
    item, _diagnosis = _external_repair_item(runner)
    allowed_path = root / "scripts/eval/temporal_state.py"
    before_source = allowed_path.read_text(encoding="utf-8")
    client = _FakeResponsesClient([
        _AuthenticationError("not recorded"),
        {"id": "resp-retry", "conversation": {"id": "conv-1"}, "output_text": _review_json()},
    ])
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    runner.external_review = ExternalReviewCoordinator(
        root, config=ExternalReviewConfig(transport="OPENAI_RESPONSES"),
        transport=ResponsesExternalReviewTransport(client_factory=lambda: client),
    )
    runner.repair_builder = _NoExternalLocalProposal()
    # The production call originates in an active repair episode, not IDLE.
    runner.snapshot.state = LocalLLMDayState.REPAIR_SUPERVISOR
    calls = []
    runner.work_order_executor = lambda order: calls.append(order) or {
        "final_result": "FAILED", "error_code": "EXPERT_FIXTURE_FAILED",
    }

    assert not runner._supervise_repair(item, runner.snapshot.contract)
    episode = runner.repair_episode_store.get(runner.snapshot.repair_episode_ids[-1])
    assert episode.external_review_failure_code == "OPENAI_AUTHENTICATION_FAILED"
    assert [call["kind"] for call in calls] == ["CODEX_EXPERT_SOLVER"]
    runner._external_repair_blocker(runner.snapshot.contract, runner._inventory(runner.snapshot.contract), item)
    assert runner.view()["state"] == "EXTERNAL_ACTION_REQUIRED"
    assert runner.view()["blocker"]["reason_code"] == "OPENAI_AUTHENTICATION_FAILED"
    assert runner.view()["blocker"]["resolution_strategy"] == "EXTERNAL_REVIEW_PREREQUISITE"
    runner.snapshot.work_items = [item]
    assert runner.view()["enabled_controls"]["resume"] is True
    assert allowed_path.read_text(encoding="utf-8") == before_source
    runner.work_order_executor = lambda order: calls.append(order) or (
        {"final_result": "COMPLETE", "verification_passed": True,
         "evidence": _valid_evidence([name for criterion in runner.snapshot.contract.completion_criteria for name in criterion.required_evidence])}
        if order["kind"] == "EXTERNAL_REVIEW_BUILDER"
        else {"final_result": "FAILED", "error_code": "EXPERT_FIXTURE_FAILED"}
    )
    assert "error_code" not in runner.resume()
    runner.join(5)
    resumed = runner.repair_episode_store.get(runner.snapshot.repair_episode_ids[-1])
    assert len(client.calls) == 2, runner.view()
    assert [call["kind"] for call in calls] == ["CODEX_EXPERT_SOLVER", "EXTERNAL_REVIEW_BUILDER"]
    assert resumed.external_review_resume_attempts == 1


@pytest.mark.parametrize(("provider_failure", "reason_code"), [
    (TimeoutError("fixture timeout"), "OPENAI_TIMEOUT"),
    (_QuotaError("fixture quota"), "OPENAI_QUOTA_OR_RATE_LIMIT"),
])
def test_recoverable_external_provider_failure_retries_once_then_reblocks(tmp_path, monkeypatch, provider_failure, reason_code):
    engine, root, _writer, _builder = _repair_engine(tmp_path)
    runner = engine.local_llm_day_program
    runner.smoke(6)
    item, _diagnosis = _external_repair_item(runner)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    client = _FakeResponsesClient([provider_failure, provider_failure])
    runner.external_review = ExternalReviewCoordinator(
        root, config=ExternalReviewConfig(transport="OPENAI_RESPONSES"),
        transport=ResponsesExternalReviewTransport(client_factory=lambda: client),
    )
    runner.repair_builder = _NoExternalLocalProposal()
    runner.snapshot.state = LocalLLMDayState.REPAIR_SUPERVISOR
    runner.work_order_executor = lambda _order: {"final_result": "FAILED", "error_code": "EXPERT_FIXTURE_FAILED"}

    assert not runner._supervise_repair(item, runner.snapshot.contract)
    runner._external_repair_blocker(runner.snapshot.contract, runner._inventory(runner.snapshot.contract), item)
    runner.snapshot.work_items = [item]
    assert runner.view()["blocker"]["reason_code"] == reason_code
    assert runner.view()["enabled_controls"]["resume"] is True
    assert "error_code" not in runner.resume()
    runner.join(5)
    episode = runner.repair_episode_store.get(runner.snapshot.repair_episode_ids[-1])
    assert len(client.calls) == 2
    assert episode.external_review_resume_attempts == 1
    assert runner.view()["state"] == "EXTERNAL_ACTION_REQUIRED"
    assert runner.view()["blocker"]["reason_code"] == reason_code
    assert runner.view()["enabled_controls"]["resume"] is False


def test_uncertain_external_submission_cannot_be_resubmitted_by_operator_resume(tmp_path):
    engine, root, _writer, _builder = _repair_engine(tmp_path)
    runner = engine.local_llm_day_program
    runner.smoke(6)
    item, _diagnosis = _external_repair_item(runner)
    transport = _FixtureExternalReviewer(_review_json())
    runner.external_review = ExternalReviewCoordinator(root, config=ExternalReviewConfig(transport="FIXTURE"), transport=transport)
    runner.repair_builder = _NoExternalLocalProposal()
    now = runner.clock()
    episode = RepairEpisode(
        episode_id="uncertain-external-episode", project_id=runner.project_id, day=6,
        work_item_id=item.item_id, failure_class=DayIssueClassification.IMPLEMENTATION_DEFECT,
        failure_fingerprint=runner._failure_fingerprint(item, item.evidence["failure_excerpt"]),
        failure_excerpt=item.evidence["failure_excerpt"], component=item.item_id,
        started_at_epoch=now, repair_deadline_epoch=now + 300,
        expert_solver_outcome="EXPERT_FIXTURE_FAILED", external_review_phase="SUBMISSION_STARTED",
        contract_version=runner.snapshot.contract.version,
        scope_fingerprint=runner._text_fingerprint(item.dynamic_work_order.model_dump_json()),
    )
    runner.repair_episode_store.save(episode)
    runner.snapshot.repair_episode_ids = [episode.episode_id]
    runner.snapshot.work_items = [item]
    runner.snapshot.state = LocalLLMDayState.REPAIR_SUPERVISOR

    assert not runner._supervise_repair(item, runner.snapshot.contract)
    runner._external_repair_blocker(runner.snapshot.contract, runner._inventory(runner.snapshot.contract), item)
    assert runner.view()["blocker"]["reason_code"] == "EXTERNAL_REVIEW_SUBMISSION_UNCERTAIN"
    assert runner.view()["enabled_controls"]["resume"] is False
    assert "error_code" not in runner.resume()
    runner.join(5)
    assert runner.view()["blocker"]["reason_code"] == "EXTERNAL_REVIEW_SUBMISSION_UNCERTAIN"
    assert transport.submissions == []


def test_restart_reuses_captured_external_review_without_repeating_expert_or_submission(tmp_path):
    engine, root, _writer, _builder = _repair_engine(tmp_path)
    runner = engine.local_llm_day_program
    runner.smoke(6)
    item, diagnosis = _external_repair_item(runner)
    response = _review_json()
    transport = _FixtureExternalReviewer(response)
    coordinator = ExternalReviewCoordinator(root, config=ExternalReviewConfig(transport="FIXTURE"), transport=transport)
    order = item.dynamic_work_order
    package = coordinator.build_package(
        selected_day=runner.snapshot.contract.day, contract_version=runner.snapshot.contract.version,
        contract_fingerprint=runner.snapshot.contract_fingerprint or runner._contract_fingerprint(runner.snapshot.contract),
        criterion_id=diagnosis.criterion_id, evidence_type=diagnosis.evidence_type,
        gap_diagnosis=diagnosis.model_dump(mode="json"),
        failed_action=dict(runner.snapshot.last_action or {"task_id": order.task_id}),
        allowed_files=order.allowed_files, context_files=order.context_files, acceptance_test_files=order.acceptance_test_files,
        failure_excerpt=item.evidence["failure_excerpt"], stdout_excerpt="", stderr_excerpt="",
        git_summary=runner._external_review_git_summary(runner._inventory(runner.snapshot.contract)),
        previous_attempts=[], rejection_feedback=[], catalog_matches=[], expert_solver_outcome="EXPERT_FIXTURE_FAILED",
        deterministic_verification_failure="EXPERT_FIXTURE_FAILED")
    artifact = coordinator.request_review(package)
    now = runner.clock()
    episode = RepairEpisode(
        episode_id="restart-external-episode", project_id=runner.project_id, day=6,
        work_item_id=item.item_id, failure_class=DayIssueClassification.IMPLEMENTATION_DEFECT,
        failure_fingerprint=runner._failure_fingerprint(item, item.evidence["failure_excerpt"]),
        failure_excerpt=item.evidence["failure_excerpt"], component=item.item_id,
        started_at_epoch=now, repair_deadline_epoch=now + 300,
        expert_solver_outcome="EXPERT_FIXTURE_FAILED", external_review_fingerprint=package.fingerprint,
        external_review_artifact=artifact.artifact_path, external_review_outcome="GUIDANCE_RECEIVED",
        external_review_phase="ARTIFACT_RECORDED",
        contract_version=runner.snapshot.contract.version,
        scope_fingerprint=runner._text_fingerprint(item.dynamic_work_order.model_dump_json()),
    )
    runner.repair_episode_store.save(episode)
    runner.snapshot.repair_episode_ids = [episode.episode_id]
    runner.snapshot.work_items = [item]
    runner.snapshot.state = LocalLLMDayState.REPAIR_SUPERVISOR
    restored = LocalLLMDayProgram(root, saved=runner.view(), repair_episode_store=runner.repair_episode_store,
        external_review=coordinator, repair_builder=_NoExternalLocalProposal())
    calls = []
    restored.work_order_executor = lambda order: calls.append(order) or {
        "final_result": "COMPLETE", "verification_passed": True,
        "evidence": _valid_evidence([name for criterion in restored.snapshot.contract.completion_criteria for name in criterion.required_evidence]),
    }

    assert restored._supervise_repair(restored.snapshot.work_items[0], restored.snapshot.contract)
    assert [call["kind"] for call in calls] == ["EXTERNAL_REVIEW_BUILDER"]
    assert len(transport.submissions) == 1


def test_external_review_uses_source_root_and_separate_state_root(tmp_path):
    source_root = tmp_path / "local-llm-lab"
    state_root = tmp_path / "control-center" / "state" / "external-review"
    (source_root / "scripts").mkdir(parents=True)
    (source_root / "scripts" / "adapter.py").write_text("def adapter(): return True\n", encoding="utf-8")
    transport = _FixtureExternalReviewer(_review_json(files=["scripts/adapter.py"]))
    coordinator = ExternalReviewCoordinator(source_root, state_root=state_root,
        config=ExternalReviewConfig(transport="FIXTURE"), transport=transport)
    package = coordinator.build_package(selected_day=6, contract_version="fixture", contract_fingerprint="a" * 64,
        criterion_id="criterion", evidence_type="source_check", gap_diagnosis={}, failed_action={},
        allowed_files=["scripts/adapter.py"], context_files=[], acceptance_test_files=["tests/test_adapter.py"],
        failure_excerpt="failure", stdout_excerpt="", stderr_excerpt="", git_summary={}, previous_attempts=[],
        rejection_feedback=[], catalog_matches=[], expert_solver_outcome="FAILED", deterministic_verification_failure="failure")
    artifact = coordinator.request_review(package)
    assert artifact.artifact_path.startswith(str(state_root))
    assert package.source_excerpts["scripts/adapter.py"] == "def adapter(): return True\n"
    assert not (source_root / "state" / "external-review").exists()


def test_external_review_sanitizes_nested_values_and_external_paths(tmp_path):
    root = tmp_path / "local-llm-lab"
    root.mkdir()
    coordinator = ExternalReviewCoordinator(root, config=ExternalReviewConfig(transport="FIXTURE"),
        transport=_FixtureExternalReviewer(_review_json()))
    package = coordinator.build_package(selected_day=6, contract_version="fixture", contract_fingerprint="a" * 64,
        criterion_id="criterion", evidence_type="source_check",
        gap_diagnosis={"token": "sk-abcdefghijklmnop", "nested": {"password": "nope"}},
        failed_action={"path": "C:/Users/test/private.txt"}, allowed_files=[], context_files=[], acceptance_test_files=[],
        failure_excerpt="Bearer abcdefghijklmnop OPENAI_API_KEY=secret", stdout_excerpt="", stderr_excerpt="",
        git_summary={"retained_evidence": {"token": "sk-abcdefghijklmnop"}},
        previous_attempts=[{"secret": "nope"}], rejection_feedback=[], catalog_matches=[],
        expert_solver_outcome="FAILED", deterministic_verification_failure="failure")
    serialized = package.model_dump_json()
    assert "abcdefghijklmnop" not in serialized and "private.txt" not in serialized and "nope" not in serialized
    assert "retained_evidence" not in serialized


def test_external_review_rejects_acceptance_test_as_editable_scope(tmp_path):
    engine, _root, writer, _builder = _repair_engine(tmp_path)
    runner = engine.local_llm_day_program
    runner.smoke(6)
    item, _diagnosis = _external_repair_item(runner)
    dynamic = item.dynamic_work_order.model_copy(update={"allowed_files": [*item.dynamic_work_order.allowed_files, "tests/test_temporal_state.py"]})
    result = engine._execute_local_llm_day_work_order({"kind": "EXTERNAL_REVIEW_BUILDER", "task_id": item.item_id,
        "dynamic_work_order": dynamic.model_dump(mode="json"), "external_review": json.loads(_review_json(files=["tests/test_temporal_state.py"]))})
    assert result["error_code"] == "EXTERNAL_REVIEW_BUILDER_REJECTED"
    assert writer.roles == []


def test_external_review_config_and_sdk_error_mapping_fail_closed(tmp_path):
    config_path = tmp_path / "external-review.json"
    base = {"enabled": True, "transport": "OPENAI_RESPONSES", "model": "gpt-5.6-sol", "fallback_model": "gpt-5.6-terra"}
    config_path.write_text(json.dumps(base), encoding="utf-8")
    assert load_external_review_config(config_path) is not None
    for key, value in (("enabled", False), ("transport", "FIXTURE"), ("model", "unknown"), ("fallback_model", "unknown")):
        invalid = {**base, key: value}
        config_path.write_text(json.dumps(invalid), encoding="utf-8")
        assert load_external_review_config(config_path) is None
    config_path.write_text("{", encoding="utf-8")
    assert load_external_review_config(config_path) is None
    class SdkError(Exception):
        def __init__(self, status_code, code):
            self.status_code, self.code = status_code, code
            super().__init__(code)
    assert ResponsesExternalReviewTransport._error_code(SdkError(401, "authentication_error")) == "OPENAI_AUTHENTICATION_FAILED"
    assert ResponsesExternalReviewTransport._error_code(SdkError(403, "permission_denied")) == "OPENAI_AUTHENTICATION_FAILED"
    assert ResponsesExternalReviewTransport._error_code(SdkError(429, "rate_limit")) == "OPENAI_QUOTA_OR_RATE_LIMIT"
    assert ResponsesExternalReviewTransport._error_code(SdkError(404, "model_not_found")) == "OPENAI_MODEL_ACCESS"


def test_static_process_tasks_cannot_supply_day_one_evidence():
    calls = []

    def unsafe_day_one_planner(*_args):
        calls.append("planner")
        return [LocalLLMDayWorkItem(item_id="pc", title="PC", objective="unsafe", kind="ENGINE_WORK_ORDER", engine_task_id="PC-001-A", criterion_ids=["d1-repository_relationship"])]

    runner = LocalLLMDayProgram(FIXTURE_ROOT, planner=unsafe_day_one_planner, work_order_executor=lambda _order: {"final_result": "COMPLETE", "evidence": {"git_head": _record("git_head", _value("git_head"))}})
    runner.start(1)
    runner.join(3)
    result = runner.view()
    assert not calls
    assert result["state"] != LocalLLMDayState.COMPLETE.value
    assert "d1-regression_baseline" in result["contract"]["remaining_gaps"]


def test_new_day_drops_stale_work_and_old_contract_state():
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(2)
    runner.snapshot.work_items = [LocalLLMDayWorkItem(item_id="stale", title="stale", objective="stale", criterion_ids=["d3-fixed_comparison"], contract_day=3, contract_version=runner.snapshot.contract.version, state=LocalLLMWorkItemState.COMPLETE)]
    runner.snapshot.replan_count = 2
    runner.smoke(1)
    assert runner.view()["work_items"] == []
    assert runner.view()["replan_count"] == 0


def test_old_false_complete_persistence_is_invalidated():
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(1)
    saved = runner.view()
    saved["state"] = "COMPLETE"
    saved["contract"]["version"] = "2026-09-22-v1"
    for criterion in saved["contract"]["completion_criteria"]:
        criterion["satisfied"] = True
        criterion["evidence"] = {"run_id": "old-run"}
    restored = LocalLLMDayProgram(FIXTURE_ROOT, saved=saved)
    assert restored.view()["state"] != LocalLLMDayState.COMPLETE.value
    assert len(restored.view()["contract"]["remaining_gaps"]) == 4


def test_legacy_insufficient_evidence_failure_restores_to_paused_and_revalidates_on_resume():
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(6)
    contract = runner.snapshot.contract
    required = sorted({name for criterion in contract.completion_criteria for name in criterion.required_evidence})
    runner._ingest_legacy_evidence(contract, _valid_evidence(required),
                                   provider_id="fixture-adapter", source_fingerprint="a" * 64)
    runner._evaluate_contract(contract, runner._inventory(contract))
    runner.snapshot.contract_fingerprint = runner._contract_fingerprint(contract)
    runner.snapshot.state = LocalLLMDayState.FAILED
    runner.snapshot.report = runner.snapshot.report.model_copy(update={"result": "DAY_INSUFFICIENT_EVIDENCE"}) if runner.snapshot.report else None
    if runner.snapshot.report is None:
        from backend.models.local_llm_day import LocalLLMDayReport
        runner.snapshot.report = LocalLLMDayReport(day=6, objective=contract.objective,
                                                    result="DAY_INSUFFICIENT_EVIDENCE", summary="legacy", evidence={})
    saved = runner.view()

    restored = LocalLLMDayProgram(FIXTURE_ROOT, saved=saved)
    before = restored.view()
    assert before["state"] == "PAUSED"
    assert before["selected_day"] == 6
    assert before["stop_reason"] == "LEGACY_INSUFFICIENT_EVIDENCE_REQUIRES_RESUME"
    assert before["report"]["result"] == "DAY_INSUFFICIENT_EVIDENCE"
    assert before["enabled_controls"]["resume"] is True
    assert before["evidence_store"] == saved["evidence_store"]

    restored.resume()
    restored.join(5)
    after = restored.view()
    assert "PREFLIGHT" in after["state_history"]
    assert after["state"] == "COMPLETE"
    assert after["report"]["result"] == "DAY_COMPLETE"


def test_failed_unrecoverable_snapshot_remains_non_resumable():
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(6)
    runner.snapshot.contract_fingerprint = runner._contract_fingerprint(runner.snapshot.contract)
    runner.snapshot.state = LocalLLMDayState.FAILED_UNRECOVERABLE
    saved = runner.view()

    restored = LocalLLMDayProgram(FIXTURE_ROOT, saved=saved)
    assert restored.view()["state"] == "FAILED_UNRECOVERABLE"
    assert restored.view()["enabled_controls"]["resume"] is False
    assert restored.resume()["error_code"] == "DAY_NOT_RESUMABLE"


def test_exact_day_one_audit_enum_failure_recovers_without_mutating_v1_history_or_evidence():
    runner = _legacy_day_one_blocker_runner()
    runner.snapshot.state = LocalLLMDayState.FAILED_UNRECOVERABLE
    runner.snapshot.phase = LocalLLMDayState.FAILED_UNRECOVERABLE.value
    runner.snapshot.activity = runner.KNOWN_INFRASTRUCTURE_RECOVERY_FAILURE
    before_attempts = [item.model_dump(mode="json") for item in runner.snapshot.action_attempts]
    before_evidence = {key: value.model_dump(mode="json") for key, value in runner.snapshot.evidence_store.items()}
    audit = []
    runner.audit = lambda task_id, event, details: audit.append((task_id, event, details))
    runner._execute = lambda: None

    result = runner.resume()
    runner.join(3)

    assert result["state"] == LocalLLMDayState.PREFLIGHT.value
    assert runner.snapshot.state == LocalLLMDayState.PREFLIGHT
    assert [item.model_dump(mode="json") for item in runner.snapshot.action_attempts] == before_attempts
    assert {key: value.model_dump(mode="json") for key, value in runner.snapshot.evidence_store.items()} == before_evidence
    assert audit[-1][1] == "LOCAL_LLM_DAY_STARTED"
    assert audit[-1][2]["resume_mode"] == "AUTHORIZED_INFRASTRUCTURE_RECOVERY"


def test_unknown_failed_unrecoverable_remains_non_resumable():
    runner = _legacy_day_one_blocker_runner()
    runner.snapshot.state = LocalLLMDayState.FAILED_UNRECOVERABLE
    runner.snapshot.activity = "Controller cannot safely continue: ValueError: unknown"

    assert runner._known_infrastructure_recovery_available() is False
    assert runner.resume()["error_code"] == "DAY_NOT_RESUMABLE"


def test_same_day_valid_incomplete_evidence_survives_restart():
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(6)
    criterion = runner.snapshot.contract.completion_criteria[0]
    runner._ingest_legacy_evidence(runner.snapshot.contract, _valid_evidence(criterion.required_evidence),
                                   provider_id="fixture-adapter", source_fingerprint="a" * 64)
    runner._evaluate_contract(runner.snapshot.contract, {})
    saved = runner.view()
    saved["state"] = "PAUSED"
    restored = LocalLLMDayProgram(FIXTURE_ROOT, saved=saved)
    assert restored.view()["contract"]["completion_criteria"][0]["satisfied"] is True
    assert restored.view()["state"] == LocalLLMDayState.PAUSED.value
    assert restored.view()["evidence_store"] == saved["evidence_store"]


@pytest.mark.parametrize("operation", ["restart", "resume"])
def test_identical_contract_identity_survives_restart_and_resume(operation):
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(6)
    contract = runner.snapshot.contract
    all_required = sorted({evidence for criterion in contract.completion_criteria for evidence in criterion.required_evidence})
    runner._ingest_legacy_evidence(contract, _valid_evidence(all_required),
                                   provider_id="fixture-adapter", source_fingerprint="a" * 64)
    runner._evaluate_contract(contract, {})
    assert not contract.remaining_gaps
    runner.snapshot.contract_fingerprint = runner._contract_fingerprint(contract)
    runner.snapshot.state = LocalLLMDayState.PAUSED
    saved = runner.view()
    reordered = contract.model_copy(deep=True)
    reordered.completion_criteria.reverse()
    for criterion in reordered.completion_criteria:
        criterion.required_evidence.reverse()
    reordered.constraints.reverse()
    reordered.authoritative_sources.reverse()
    assert runner._contract_fingerprint(reordered) == saved["contract_fingerprint"]
    if operation == "restart":
        runner = LocalLLMDayProgram(FIXTURE_ROOT, saved=saved)
    else:
        runner.resume()
        runner.join(5)
    result = runner.view()
    assert result["contract_fingerprint"] == saved["contract_fingerprint"]
    assert result["evidence_store"] == saved["evidence_store"]
    assert result["contract"]["remaining_gaps"] == []
    if operation == "resume":
        assert result["state"] == "COMPLETE", result
    else:
        assert result["state"] == "PAUSED", result


@pytest.mark.parametrize("operation", ["restart", "resume"])
@pytest.mark.parametrize("changed_field", ["statement", "required_evidence", "objective", "constraints", "authoritative_sources"])
def test_same_version_contract_content_change_fails_closed(tmp_path, monkeypatch, operation, changed_field):
    import yaml
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(6)
    criterion = runner.snapshot.contract.completion_criteria[0]
    runner._ingest_legacy_evidence(runner.snapshot.contract, _valid_evidence(criterion.required_evidence),
                                   provider_id="fixture-adapter", source_fingerprint="a" * 64)
    runner.snapshot.contract_fingerprint = runner._contract_fingerprint(runner.snapshot.contract)
    runner.snapshot.state = LocalLLMDayState.PAUSED
    saved = runner.view()
    document = yaml.safe_load(runner.PROGRAM_PATH.read_text(encoding="utf-8"))
    definition = next(d for d in document["days"] if d["day"] == 6)
    if changed_field == "statement":
        definition["completion_criteria"][0]["statement"] += " changed semantics"
    elif changed_field == "required_evidence":
        # Move two existing registered evidence types between criteria.  Registry
        # coverage remains valid while the criterion semantics change.
        first = definition["completion_criteria"][0]["evidence"]
        second = definition["completion_criteria"][1]["evidence"]
        first[1], second[1] = second[1], first[1]
    elif changed_field == "objective":
        definition["objective"] += " changed semantics"
    else:
        # Sources/constraints are program-level in the server-owned YAML.
        key = "shared_constraints" if changed_field == "constraints" else changed_field
        document[key] = [*document[key], "changed semantics"]
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setattr(LocalLLMDayProgram, "PROGRAM_PATH", path)
    if operation == "restart":
        runner = LocalLLMDayProgram(FIXTURE_ROOT, saved=saved)
    else:
        runner.resume()
        runner.join(5)
    result = runner.view()
    assert result["state"] == "FAILED_UNRECOVERABLE", result
    assert result["report"]["result"] == "CONTRACT_VERSION_CONTENT_MISMATCH"
    assert set(result["evidence_store"]) == set(saved["evidence_store"])
    assert all(not record["compatibility_result"] for record in result["evidence_store"].values())
    assert not result["action_attempts"]


@pytest.mark.parametrize("operation", ["restart", "resume"])
def test_changed_contract_version_does_not_reuse_saved_evidence(tmp_path, monkeypatch, operation):
    import yaml
    runner = LocalLLMDayProgram(FIXTURE_ROOT)
    runner.smoke(6)
    criterion = runner.snapshot.contract.completion_criteria[0]
    runner._ingest_legacy_evidence(runner.snapshot.contract, _valid_evidence(criterion.required_evidence),
                                   provider_id="fixture-adapter", source_fingerprint="a" * 64)
    runner.snapshot.contract_fingerprint = runner._contract_fingerprint(runner.snapshot.contract)
    runner.snapshot.state = LocalLLMDayState.PAUSED
    saved = runner.view()
    document = yaml.safe_load(runner.PROGRAM_PATH.read_text(encoding="utf-8"))
    document["version"] += "-next"
    path = tmp_path / "contract-version.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setattr(LocalLLMDayProgram, "PROGRAM_PATH", path)
    if operation == "restart":
        runner = LocalLLMDayProgram(FIXTURE_ROOT, saved=saved)
    else:
        runner.resume()
        runner.join(5)
    result = runner.view()
    assert result["state"] == "FAILED_UNRECOVERABLE", result
    assert result["report"]["result"] == "CONTRACT_VERSION_CHANGED"
    assert set(result["evidence_store"]) == set(saved["evidence_store"])
    assert all(not record["compatibility_result"] for record in result["evidence_store"].values())
    assert not result["action_attempts"]


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def _day1_repo(tmp_path):
    root = tmp_path / "local-llm"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "day1@example.test")
    _git(root, "config", "user.name", "Day One")
    files = {
        "docs/runbooks/work-plan-day1-14.md": "# Day 1-14\n",
        "docs/README.md": "| Current decision architecture | architecture/decision-reasoning-architecture.md |\n| Current execution sequence | runbooks/work-plan-day1-14.md |\n| Resume context | handoff/handoff-2026-09-18.md |\n",
        "docs/architecture/decision-reasoning-architecture.md": "# Architecture\n",
        "docs/handoff/handoff-2026-09-18.md": "# Handoff\n",
        "tests/test_process_consistency_smoke.py": "def test_baseline():\n    assert True\n",
        "tests/test_process_consistency_review_set.py": "def test_review_set():\n    assert True\n",
    }
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture baseline")
    _git(root, "remote", "add", "origin", str(root))
    head = _git(root, "rev-parse", "HEAD")
    _git(root, "config", "branch.main.remote", "origin")
    _git(root, "config", "branch.main.merge", "refs/heads/main")
    _git(root, "update-ref", "refs/remotes/origin/main", head)
    (root / "artifacts").mkdir()
    (root / "artifacts" / "retained.txt").write_text("preserve", encoding="utf-8")
    return root


def _run_day1(root):
    runner = LocalLLMDayProgram(root)
    runner.start(1)
    runner.join(240)
    return runner.view()


def test_day_one_disposable_git_integration_collects_real_evidence(tmp_path):
    root = _day1_repo(tmp_path)
    result = _run_day1(root)
    assert result["state"] == LocalLLMDayState.COMPLETE.value
    criteria = {item["criterion_id"]: {name: result["evidence_store"][record_id] for name, record_id in item["evidence_record_ids"].items()} for item in result["contract"]["completion_criteria"]}
    head = _git(root, "rev-parse", "HEAD")
    assert criteria["d1-repository_relationship"]["git_head"]["value"]["head"] == head
    assert criteria["d1-repository_relationship"]["origin_ref"]["value"]["origin_url"] == str(root)
    assert "artifacts/retained.txt" in criteria["d1-preservation_audit"]["status_audit"]["value"]["untracked_paths"]
    assert criteria["d1-regression_baseline"]["test_result"]["value"]["exit_code"] == 0
    checkpoint = criteria["d1-regression_baseline"]["commit_ref"]["value"]
    assert checkpoint["head"] != head
    assert checkpoint["parent_head"] == head
    assert checkpoint["checkpoint_ref"].startswith("refs/heads/ai-control-center/day1-baseline-")
    assert _git(root, "rev-parse", "HEAD") == head
    assert _git(root, "diff", "--cached", "--name-only") == ""


def test_frozen_registry_covers_each_configured_day_evidence_pair():
    document = __import__("yaml").safe_load(LocalLLMDayProgram.PROGRAM_PATH.read_text(encoding="utf-8"))
    pairs = {(day["day"], evidence) for day in document["days"] for criterion in day["completion_criteria"] for evidence in criterion["evidence"]}
    assert len(REGISTRY.names) == 52
    assert len(pairs) == 63
    assert_coverage(pairs, REGISTRY.names)
    assert set(STRATEGIES) == pairs


def test_day_one_commit_ref_is_not_satisfied_by_read_only_head_observation(tmp_path):
    root = _day1_repo(tmp_path)
    runner = LocalLLMDayProgram(root)
    contract = runner._load_contracts()[1]
    runner._ingest_action_result(contract, runner._diagnose_gaps(contract, runner._inventory(contract))[0], runner._collect_day_one_evidence(contract))
    runner._evaluate_contract(contract, runner._inventory(contract))
    assert "d1-regression_baseline" in contract.remaining_gaps
    assert not contract.completion_criteria[-1].evidence_record_ids.get("commit_ref")


def test_day_one_integration_rejects_staged_generated_artifact(tmp_path):
    root = _day1_repo(tmp_path)
    path = root / "results" / "generated.json"
    path.parent.mkdir()
    path.write_text("{}", encoding="utf-8")
    _git(root, "add", "results/generated.json")
    result = _run_day1(root)
    assert result["state"] != LocalLLMDayState.COMPLETE.value
    assert "d1-preservation_audit" in result["contract"]["remaining_gaps"]


def test_day_one_integration_rejects_broken_authoritative_documentation(tmp_path):
    root = _day1_repo(tmp_path)
    (root / "docs" / "README.md").write_text("| Current execution sequence | Week1.md |\n", encoding="utf-8")
    result = _run_day1(root)
    assert result["state"] != LocalLLMDayState.COMPLETE.value
    assert "d1-documentation_consistency" in result["contract"]["remaining_gaps"]


def test_day_one_integration_rejects_failed_regression(tmp_path):
    root = _day1_repo(tmp_path)
    (root / "tests" / "test_process_consistency_smoke.py").write_text("def test_baseline():\n    assert False\n", encoding="utf-8")
    result = _run_day1(root)
    assert result["state"] != LocalLLMDayState.COMPLETE.value
    assert "d1-regression_baseline" in result["contract"]["remaining_gaps"]


def _production_engine(root, tmp_path):
    from backend.control.projects import ConfiguredProject, ProjectRegistry
    from backend.orchestrator.engine import ControlCenterEngine
    from backend.models.runtime import RuntimeConfig
    projects = ProjectRegistry(projects={"local_llm_lab": ConfiguredProject(
        name="Disposable Day project", path=root, default_branch="main")})
    engine = ControlCenterEngine(project_registry=projects, runtime_config=RuntimeConfig(),
                                 worktree_root=tmp_path / "worktrees", smoke_root=tmp_path / "smoke")
    engine.local_llm_day_program.solution_catalog = SolutionCatalog()
    engine.local_llm_day_program.repair_episode_store = RepairEpisodeStore()
    return engine


def _repair_engine(tmp_path, root=None):
    from backend.control.local_ollama_repair import RepairEdit, RepairProposal
    from backend.models.result import ExecutionResult, ProcessDiagnostics, TokenUsage
    from backend.models.runtime import CodexMode
    tmp_path.mkdir(parents=True, exist_ok=True)
    if root is None:
        root = _day1_repo(tmp_path)
        contents = {
            "scripts/eval/temporal_state.py": "def valid_time(): return False\n",
            "schemas/temporal-state.json": '{"type":"object","properties":{"time":{"type":"string"}}}',
            "docs/architecture/temporal-state.md": "# Fixed temporal contract\n",
            "tests/test_temporal_state.py": "import importlib.util\nfrom pathlib import Path\ndef test_time():\n    path = Path(__file__).parents[1] / 'scripts/eval/temporal_state.py'\n    spec = importlib.util.spec_from_file_location('temporal', path)\n    module = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(module)\n    assert module.valid_time() is True\n",
            ".gitignore": "__pycache__/\n.pytest_cache/\n",
        }
        for relative, value in contents.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
        _git(root, "add", "--", *contents)
        _git(root, "commit", "-m", "fixture temporal defect")
    engine = _production_engine(root, tmp_path)
    engine.runtime.codex.mode = CodexMode.REAL
    class Writer:
        def __init__(self):
            self.roles = []
        def run_worktree_task(self, working_directory, prompt):
            role = "external" if "external-review bounded repair" in prompt else "expert" if "Codex Expert Solver repair" in prompt else "local" if "LOCAL LLM COUNTERMEASURE" in prompt else "normal"
            self.roles.append(role)
            if role in {"expert", "external"}:
                (working_directory / "scripts/eval/temporal_state.py").write_text("def valid_time(): return True\n", encoding="utf-8")
            return ExecutionResult(status="completed", test_result="pending", summary="fixture provider", exit_code=0,
                token_usage=TokenUsage(input_tokens=1, output_tokens=1, available=True),
                diagnostics=ProcessDiagnostics(thread_started=True, turn_started=True, turn_completed=True))
    class Proposals:
        def __init__(self):
            self.requests = []
        def propose(self, **kwargs):
            self.requests.append(kwargs)
            return RepairProposal(f"fixture proposal {len(self.requests)}", (RepairEdit("scripts/eval/temporal_state.py", "False", "False"),))
    writer, builder = Writer(), Proposals()
    engine.real_runner = writer
    engine.local_llm_day_program.repair_builder = builder
    return engine, root, writer, builder


def test_external_review_builder_uses_production_engine_worktree_and_adapter(tmp_path):
    engine, root, writer, _builder = _repair_engine(tmp_path)
    assert engine.local_llm_day_program.external_review.root == root.resolve()
    assert engine.local_llm_day_program.external_review.store.root == (engine.project_root / "state" / "external-review").resolve()
    runner = engine.local_llm_day_program
    runner.smoke(6)
    item, diagnosis = _external_repair_item(runner)
    result = engine._execute_local_llm_day_work_order({
        "kind": "EXTERNAL_REVIEW_BUILDER", "task_id": item.item_id,
        "dynamic_work_order": item.dynamic_work_order.model_dump(mode="json"),
        "external_review": json.loads(_review_json()),
    })

    assert result["final_result"] == "COMPLETE"
    assert result["verification_passed"] is True
    assert result["evidence"]
    assert writer.roles == ["external"]
    assert (root / "scripts/eval/temporal_state.py").read_text(encoding="utf-8") == "def valid_time(): return False\n"
    assert Path(result["worktree_path"]).is_relative_to(engine.worktree_root)
    assert diagnosis.evidence_type == "source_check"


def test_unapproved_research_condition_is_configuration_boundary(tmp_path):
    engine = _production_engine(_day1_repo(tmp_path), tmp_path)
    engine.day_action_executor.research_runner.run_command = lambda *_args, **_kwargs: pytest.fail("Unapproved research must not run")
    engine.start_local_llm_day(10)
    engine.local_llm_day_program.join(10)
    result = engine.local_llm_day_status()
    assert result["state"] == "HUMAN_ACTION_REQUIRED", result
    assert result["blocker"]["reason_code"] == "RESEARCH_CONDITION_NOT_APPROVED"
    assert result["blocker"]["resolution_strategy"] == "RESEARCH_CONDITION"
    assert not result["repair_episode_ids"]


@pytest.mark.parametrize("outcome", ["OBSERVED", "MODEL_QUALITY_FINDING"])
def test_production_research_uses_admitted_condition_and_retains_outcome(tmp_path, outcome):
    root = _day1_repo(tmp_path)
    inputs = {"scripts/research.py": "# Deterministic fixture model boundary\n",
              "config/research.json": json.dumps({"version": "v1", "research_kind": "performance", "entrypoint": "scripts/research.py",
                                                 "inputs": ["config/input.json"], "model": "fixture-model", "execution": {"retry": False}}),
              "config/model-matrix.json": json.dumps({"models": [{"runtime_model_name": "fixture-model", "enabled": True}]}),
              "config/input.json": '{"case":"fixed"}'}
    for relative, value in inputs.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    engine = _production_engine(root, tmp_path)
    calls = []
    def model_boundary(argv, **kwargs):
        calls.append(argv)
        assert Path(kwargs["cwd"]) != root
        output = Path(argv[argv.index("--output-root") + 1])
        values = {
            "performance_artifact": {"artifact_path": str(output), "prompt_tokens": 7, "output_tokens": 3,
                                     "elapsed_seconds": 1, "vram_mb": 1, "cpu_percent": 1, "ram_mb": 1},
            "condition_record": {"condition_fingerprint": "fixture-fixed-v1", "model": "fixture-model", "configuration_version": "v1"},
            "limitation_record": {"limitations": [outcome], "scope": "fixture only"},
        }
        (output / "evidence.json").write_text(json.dumps({"values": values, "outcome": outcome}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)
    engine.day_action_executor.research_runner.run_command = model_boundary
    runner = engine.local_llm_day_program
    runner._supervise_repair = lambda *_args: pytest.fail("Research quality must not enter repair")
    engine.start_local_llm_day(10)
    runner.join(10)
    result = engine.local_llm_day_status()
    assert result["state"] == "COMPLETE", result
    assert len(calls) == 1
    assert all(c["satisfied"] for c in result["contract"]["completion_criteria"])
    assert all(g["classification"] == "PRODUCE_DAY_EVIDENCE" for g in result["gap_diagnoses"])
    assert result["action_attempts"][0]["action_template_id"] == "D10_PERFORMANCE_RUN"
    if outcome == "MODEL_QUALITY_FINDING":
        assert result["issue_classification"] == outcome
    for record in result["evidence_store"].values():
        assert runner._evidence_record_valid(record["evidence_type"], runner.snapshot.evidence_store[record["record_id"]])
    assert all((root / relative).read_text(encoding="utf-8") == value for relative, value in inputs.items())
    plan = result["research_execution_plans"]["D10_PERFORMANCE_RUN"]
    assert plan["script_path"] == "scripts/research.py"
    assert plan["config_paths"] == ["config/research.json"]
    condition = engine.day_action_executor.research_plan(STRATEGIES[(10, "condition_record")].template)
    retained = engine.day_action_executor.research_runner.execute(condition)
    assert retained["issue_classification"] == outcome and len(calls) == 1


def _runtime_research_fixture(root, *, script_path="scripts/runtime-discovered.py",
                              config_path="config/runtime-discovered.json",
                              input_path="datasets/runtime-input.json", script_text="# fixture research script\n"):
    values = {
        "performance_artifact": {"artifact_path": "fixture", "prompt_tokens": 7, "output_tokens": 3,
                                 "elapsed_seconds": 1, "vram_mb": 1, "cpu_percent": 1, "ram_mb": 1},
        "condition_record": {"condition_fingerprint": "fixture-fixed-v1", "model": "fixture-model", "configuration_version": "v1"},
        "limitation_record": {"limitations": ["fixture"], "scope": "fixture only"},
    }
    contents = {
        script_path: script_text,
        config_path: json.dumps({"version": "v1", "research_kind": "performance", "entrypoint": script_path,
                                 "inputs": [input_path], "model": "fixture-model", "execution": {"retry": False}}),
        "config/model-matrix.json": json.dumps({"models": [{"runtime_model_name": "fixture-model", "enabled": True}]}),
        input_path: '{"case":"fixed"}',
    }
    for relative, value in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    return values


def _write_research_artifact(argv, values, outcome="OBSERVED"):
    output = Path(argv[argv.index("--output-root") + 1])
    (output / "evidence.json").write_text(json.dumps({"values": values, "outcome": outcome}), encoding="utf-8")
    return subprocess.CompletedProcess(argv, 0)


def test_runtime_research_plan_is_discovered_without_static_day_path_mapping(tmp_path):
    root = _day1_repo(tmp_path)
    values = _runtime_research_fixture(
        root, script_path="scripts/discovered_performance_boundary.py",
        config_path="config/discovered-performance-condition.json", input_path="datasets/discovered-input.json")
    engine = _production_engine(root, tmp_path)
    runner = engine.local_llm_day_program
    calls = []
    def boundary(argv, **_kwargs):
        calls.append(argv)
        return _write_research_artifact(argv, values)
    engine.day_action_executor.research_runner.run_command = boundary
    engine.start_local_llm_day(10)
    engine.local_llm_day_program.join(10)
    result = engine.local_llm_day_status()
    assert result["state"] == "COMPLETE", result
    assert len(calls) == 1
    plan = result["research_execution_plans"]["D10_PERFORMANCE_RUN"]
    assert plan["day"] == 10
    assert plan["script_path"] == "scripts/discovered_performance_boundary.py"
    assert plan["config_paths"] == ["config/discovered-performance-condition.json"]
    assert plan["input_paths"] == ["datasets/discovered-input.json"]
    assert plan["condition_fingerprint"]
    required = {"performance_artifact", "condition_record", "limitation_record"}
    stored = [record for record in runner.snapshot.evidence_store.values() if record.evidence_type in required]
    assert {record.evidence_type for record in stored} == required
    assert all(runner._evidence_record_valid(record.evidence_type, record) for record in stored)


@pytest.mark.parametrize(("unsafe", "expected_code"), [
    ("out_of_scope", "RESEARCH_PATH_OUTSIDE_TRUSTED_SCOPE"),
    ("wrong_day", "RESEARCH_DAY_AUTHORITY_MISMATCH"),
])
def test_unsafe_runtime_research_plan_fails_closed_before_execution(tmp_path, unsafe, expected_code):
    root = _day1_repo(tmp_path)
    _runtime_research_fixture(root)
    engine = _production_engine(root, tmp_path)
    calls = []
    engine.day_action_executor.research_runner.run_command = lambda *args, **kwargs: calls.append((args, kwargs))
    def unsafe_planner(request):
        proposal = dict(request["candidates"][0])
        if unsafe == "out_of_scope":
            proposal["script_path"] = "../outside.py"
        else:
            proposal["day"] = 9
        return proposal
    engine.local_llm_contract_planner.plan_research = unsafe_planner
    engine.start_local_llm_day(10)
    engine.local_llm_day_program.join(10)
    result = engine.local_llm_day_status()
    assert result["state"] == "HUMAN_ACTION_REQUIRED", result
    assert result["blocker"]["reason_code"] == expected_code
    assert calls == []
    assert not result["research_execution_plans"]


def test_ambiguous_runtime_research_condition_requires_human_decision(tmp_path):
    root = _day1_repo(tmp_path)
    _runtime_research_fixture(root, script_path="scripts/condition_a.py", config_path="config/condition_a.json", input_path="datasets/a.json")
    _runtime_research_fixture(root, script_path="scripts/condition_b.py", config_path="config/condition_b.json", input_path="datasets/b.json")
    engine = _production_engine(root, tmp_path)
    engine.day_action_executor.research_runner.run_command = lambda *_args, **_kwargs: pytest.fail("Ambiguous research must not execute")
    engine.start_local_llm_day(10)
    engine.local_llm_day_program.join(10)
    result = engine.local_llm_day_status()
    assert result["state"] == "HUMAN_ACTION_REQUIRED", result
    assert result["blocker"]["reason_code"] == "RESEARCH_CONDITION_CHOICE_REQUIRED"
    assert result["issue_classification"] == "HUMAN_PRODUCT_DECISION_REQUIRED"
    assert not result["research_execution_plans"]


@pytest.mark.parametrize("mutation", ["source", "outside_output"])
def test_research_run_rejects_source_or_output_escape(tmp_path, mutation):
    root = _day1_repo(tmp_path)
    protected = root / "scripts/protected_source.py"
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text("VALUE = 'original'\n", encoding="utf-8")
    values = {
        "performance_artifact": {"artifact_path": "fixture", "prompt_tokens": 1, "output_tokens": 1,
                                 "elapsed_seconds": 1, "vram_mb": 1, "cpu_percent": 1, "ram_mb": 1},
        "condition_record": {"condition_fingerprint": "fixture", "model": "fixture-model", "configuration_version": "v1"},
        "limitation_record": {"limitations": ["fixture"], "scope": "fixture"},
    }
    mutation_statement = (
        f"Path({str(protected)!r}).write_text(\"VALUE = 'mutated'\\n\", encoding='utf-8')"
        if mutation == "source" else "(Path.cwd().parent / 'outside-approved-output.json').write_text('escape', encoding='utf-8')"
    )
    payload = json.dumps({"values": values, "outcome": "OBSERVED"})
    script = (
        "import argparse\nimport json\nfrom pathlib import Path\n"
        "parser = argparse.ArgumentParser()\nparser.add_argument('--config')\nparser.add_argument('--output-root')\nargs = parser.parse_args()\n"
        f"{mutation_statement}\n"
        "output = Path(args.output_root)\noutput.mkdir(parents=True, exist_ok=True)\n"
        f"(output / 'evidence.json').write_text({payload!r}, encoding='utf-8')\n"
    )
    _runtime_research_fixture(root, script_text=script)
    engine = _production_engine(root, tmp_path)
    runner = engine.local_llm_day_program
    runner.smoke(10)
    condition = engine.day_action_executor.research_plan(STRATEGIES[(10, "condition_record")].template)
    with pytest.raises(GitSafetyError, match="RESEARCH_SOURCE_MUTATION"):
        engine.day_action_executor.research_runner.execute(condition)
    terminal = next(engine.day_action_executor.research_runner.storage.rglob("terminal.json"))
    assert json.loads(terminal.read_text(encoding="utf-8"))["safety"] == "SOURCE_MUTATION"
    if mutation == "source":
        assert protected.read_text(encoding="utf-8") == "VALUE = 'mutated'\n"


@pytest.mark.parametrize("authority", ["human", "external"])
def test_production_api_resumes_same_day_after_authority_resolution(tmp_path, monkeypatch, authority):
    from fastapi.testclient import TestClient
    import backend.app as app
    root = _day1_repo(tmp_path)
    missing = root / "docs/runbooks/work-plan-day1-14.md"
    if authority == "external":
        preserved = missing.read_text(encoding="utf-8")
        missing.rename(root / "docs/runbooks/preserved-runbook.md")
    engine = _production_engine(root, tmp_path)
    monkeypatch.setattr(app, "engine", engine)
    client = TestClient(app.app)
    day = 14 if authority == "human" else 1
    expected = "HUMAN_ACTION_REQUIRED" if authority == "human" else "EXTERNAL_ACTION_REQUIRED"
    client.post(f"/api/local-llm/day/{day}/start")
    engine.local_llm_day_program.join(30)
    before = client.get("/api/local-llm/day/status").json()
    assert before["state"] == expected, before
    assert before["blocker"]["resolution_strategy"] == ("RETAINED_EVIDENCE" if authority == "human" else "AUTHORITATIVE_SOURCES")
    assert not before["enabled_controls"]["resume"]
    selected_day, contract_fingerprint = before["selected_day"], before["contract_fingerprint"]
    blocker = before["blocker"]
    compatible_evidence = {record_id for record_id, record in before["evidence_store"].items()
                           if record["compatibility_result"]}
    client.post("/api/local-llm/day/resume")
    engine.local_llm_day_program.join(30)
    unresolved = client.get("/api/local-llm/day/status").json()
    assert unresolved["state"] == expected and unresolved["selected_day"] == day
    assert len(unresolved["action_attempts"]) == len(before["action_attempts"])
    assert unresolved["contract_fingerprint"] == contract_fingerprint
    assert unresolved["blocker"] == blocker
    assert compatible_evidence.issubset(unresolved["evidence_store"])
    if authority == "human":
        marker = root / "docs/reviews/day14-human-review.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"day": 14, "project_id": "local_llm_lab", "authority": "human",
                                     "approved": True, "marker_id": "fixture-human-review"}), encoding="utf-8")
    else:
        missing.write_text(preserved, encoding="utf-8")
    assert client.get("/api/local-llm/day/status").json()["enabled_controls"]["resume"]
    client.post("/api/local-llm/day/resume")
    engine.local_llm_day_program.join(60)
    result = client.get("/api/local-llm/day/status").json()
    assert result["state"] == "COMPLETE", result
    assert result["selected_day"] == day and result["blocker"] is None
    assert result["contract_fingerprint"] == contract_fingerprint
    assert compatible_evidence.issubset(result["evidence_store"])
    assert result["state_history"].count("PREFLIGHT") == 3
