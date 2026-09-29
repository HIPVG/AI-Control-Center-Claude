"""Generic, evidence-first Day 1-14 orchestration for LocalLLM-Lab.

The runbook supplies the Day contract (what must be true).  A bounded planner
supplies work items (how to collect or create evidence).  This module never
derives a shell command, source scope, or completion decision from browser
input or an LLM response.
"""

from __future__ import annotations

import subprocess
import sys
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Callable
from uuid import uuid4

import yaml

from backend.control.local_ollama_repair import LocalOllamaRepairBuilder
from backend.control.evidence_registry import REGISTRY, EvidenceRegistry
from backend.control.day_action_registry import ExecutionMode, STRATEGIES, assert_coverage
from backend.control.day_state_machine import ACTIVE, AUTHORITY, validate_transition
from backend.control.day_git import (
    checkpoint,
    day_admission,
    fingerprint as git_fingerprint,
    unsafe_paths,
    GitSafetyError,
    MAX_ACTIVE_WORK_SECONDS,
    MAX_ATTEMPTS,
)
from backend.control.retained_evidence import RetainedEvidenceResolver
from backend.control.solution_catalog import RepairEpisodeStore, SolutionCatalog, SolutionCatalogEntry
from backend.control.external_review import ExternalReviewCoordinator
from backend.models.local_llm_day import (
    DayCriterion,
    DayIssueClassification,
    GapDiagnosis,
    EvidenceRecord,
    ActionAttempt,
    AuthorityBlocker,
    LocalLLMDayContract,
    LocalLLMDayReport,
    LocalLLMDaySnapshot,
    LocalLLMDayState,
    LocalLLMDayWorkItem,
    LocalLLMRepairCard,
    RepairEpisode,
    RepairProposalAttempt,
    RunIntent,
    RunLimits,
    LocalLLMWorkItemState,
)
from backend.models.audit import AuditEventType


Planner = Callable[[LocalLLMDayContract, dict[str, object]], list[LocalLLMDayWorkItem]]
WorkOrderExecutor = Callable[[dict[str, object]], dict[str, object]]


class LocalLLMDayProgram:
    """A durable Day controller layered over trusted task execution.

    ``planner`` may be a Codex Architect adapter.  Its output is data only and
    is checked before execution.  ``work_order_executor`` is the existing
    guarded engine boundary; the default intentionally permits read-only
    evidence work only.
    """

    PROGRAM_PATH = Path(__file__).resolve().parents[2] / "config" / "local_llm_day_program.yaml"
    MAX_TASKS = 3
    MAX_LOCAL_PROPOSALS = 3
    ACTIVE_STATES = ACTIVE
    RECOVERABLE_EXTERNAL_REVIEW_FAILURES = frozenset({
        "OPENAI_AUTHENTICATION_FAILED", "OPENAI_QUOTA_OR_RATE_LIMIT",
        "OPENAI_MODEL_ACCESS", "OPENAI_TIMEOUT", "OPENAI_RESPONSES_UNAVAILABLE",
    })
    KNOWN_INFRASTRUCTURE_RECOVERY_EVENT = "AUTHORITY_BLOCKER_REPLACED_BY_REGISTERED_RETRY"
    KNOWN_INFRASTRUCTURE_RECOVERY_FAILURE = (
        "Controller cannot safely continue: ValueError: "
        "'AUTHORITY_BLOCKER_REPLACED_BY_REGISTERED_RETRY' is not a valid AuditEventType"
    )
    CATALOG_PREFLIGHT_INPUTS = (
        "selected_day",
        "contract_fingerprint",
        "git_fingerprint",
        "effective_permission",
        "requested_limits",
        "external_prerequisite",
    )

    def __init__(
        self,
        root: Path,
        *,
        saved: dict[str, object] | None = None,
        persist: Callable[[dict[str, object]], None] | None = None,
        audit: Callable[[str, str, dict[str, object]], None] | None = None,
        planner: Planner | None = None,
        work_order_executor: WorkOrderExecutor | None = None,
        authority_resolver: Callable[[AuthorityBlocker], bool] | None = None,
        repair_builder: LocalOllamaRepairBuilder | None = None,
        solution_catalog: SolutionCatalog | None = None,
        repair_episode_store: RepairEpisodeStore | None = None,
        external_review: ExternalReviewCoordinator | None = None,
        retained_evidence_resolver: RetainedEvidenceResolver | None = None,
        approved_day_one_snapshot_paths: frozenset[str] = frozenset(),
        clock: Callable[[], float] = time.time,
        project_id: str = "local_llm_lab",
    ) -> None:
        self.root = root.resolve()
        self.persist, self.audit = persist, audit
        self.snapshot = LocalLLMDaySnapshot.model_validate(saved or {})
        self.planner = planner or self._deterministic_plan
        self.work_order_executor = work_order_executor or self._read_only_executor
        self.authority_resolver = authority_resolver
        self.repair_builder = repair_builder or LocalOllamaRepairBuilder()
        self.solution_catalog = solution_catalog or SolutionCatalog()
        self.repair_episode_store = repair_episode_store or RepairEpisodeStore()
        self.external_review = external_review or ExternalReviewCoordinator(self.root)
        self.retained_evidence_resolver = retained_evidence_resolver or RetainedEvidenceResolver(self.root)
        self.clock, self.project_id, self.evidence_registry = clock, project_id, REGISTRY
        self.approved_day_one_snapshot_paths = approved_day_one_snapshot_paths
        # The contract is the denominator.  Both registries must cover it at
        # construction time, before a browser can start a Day.
        self._assert_registry_conformance()
        self._lock, self._stop = RLock(), Event()
        self._thread: Thread | None = None
        self._operator_external_resume_episode_id: str | None = None
        self._restore_snapshot()
        if self.snapshot.state in self.ACTIVE_STATES:
            if self.snapshot.state == LocalLLMDayState.REPAIR_SUPERVISOR and self.snapshot.repair_episode_ids:
                episode = self.repair_episode_store.get(self.snapshot.repair_episode_ids[-1])
                if episode:
                    episode.interrupted = True
                    self.repair_episode_store.save(episode)
            self.snapshot.state = LocalLLMDayState.PAUSED
            self.snapshot.stop_reason = "INTERRUPTED_REQUIRES_RESUME"
            self.snapshot.activity = "Interrupted by restart; Resume continues from the persisted contract and task states."
            self._save()

    def days(self) -> list[dict[str, object]]:
        """Return per-Day read-only admission metadata without selecting or starting work."""
        try:
            document = yaml.safe_load(self.PROGRAM_PATH.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            return [self._blocked_catalog_day(day, "PROGRAM_CONFIG_UNAVAILABLE",
                                              "Restore the readable Day program configuration.")
                    for day in range(1, 15)]
        if not isinstance(document, dict):
            return [self._blocked_catalog_day(day, "PROGRAM_CONFIG_INVALID",
                                              "Repair the Day program configuration without inferring contracts.")
                    for day in range(1, 15)]
        sources = document.get("authoritative_sources")
        shared_constraints = document.get("shared_constraints")
        definitions = document.get("days")
        if (not isinstance(sources, list) or not all(isinstance(value, str) and value for value in sources)
                or not isinstance(shared_constraints, list)
                or not all(isinstance(value, str) and value for value in shared_constraints)
                or not isinstance(definitions, list)):
            return [self._blocked_catalog_day(day, "PROGRAM_CONFIG_INVALID",
                                              "Repair the Day program configuration without inferring contracts.")
                    for day in range(1, 15)]
        normalized_sources = [value.replace("\\", "/") for value in sources]
        by_day: dict[int, list[dict[str, object]]] = {day: [] for day in range(1, 15)}
        for definition in definitions:
            if isinstance(definition, dict) and isinstance(definition.get("day"), int):
                day = definition["day"]
                if day in by_day:
                    by_day[day].append(definition)
        catalog: list[dict[str, object]] = []
        for day in range(1, 15):
            if len(by_day[day]) != 1:
                reason = "DAY_NOT_CONFIGURED" if not by_day[day] else "DUPLICATE_DAY_DEFINITION"
                catalog.append(self._blocked_catalog_day(
                    day, reason, "Provide exactly one complete authoritative definition for this Day."))
                continue
            definition = by_day[day][0]
            raw_criteria = definition.get("completion_criteria")
            try:
                if not isinstance(raw_criteria, list):
                    raise ValueError("completion_criteria")
                criteria = [DayCriterion(
                    criterion_id=f"d{day}-{entry['id']}",
                    statement=entry["statement"],
                    required_evidence=entry["evidence"],
                ) for entry in raw_criteria if isinstance(entry, dict)]
                if len(criteria) != len(raw_criteria):
                    raise ValueError("completion_criteria")
                contract = LocalLLMDayContract(
                    day=day,
                    title=definition["title"],
                    version=str(document.get("version", "v1")),
                    objective=definition["objective"],
                    completion_criteria=criteria,
                    constraints=shared_constraints,
                    authoritative_sources=normalized_sources,
                    remaining_gaps=[criterion.criterion_id for criterion in criteria],
                )
            except (KeyError, TypeError, ValueError):
                catalog.append(self._blocked_catalog_day(
                    day, "DAY_DEFINITION_INVALID",
                    "Repair only this Day's contract definition; do not infer missing fields."))
                continue
            required_evidence = sorted({evidence for criterion in criteria
                                        for evidence in criterion.required_evidence})
            unregistered = sorted(set(required_evidence) - self.evidence_registry.names)
            source_scope = [{"path": path, "present": (self.root / path).is_file()}
                            for path in contract.authoritative_sources]
            missing_sources = [item["path"] for item in source_scope if not item["present"]]
            if unregistered:
                admission = {
                    "status": "BLOCKED", "next_state": "INPUT_BLOCKED",
                    "reason_code": "EVIDENCE_TYPE_UNREGISTERED",
                    "next_action": "Register the declared Evidence types before Go.",
                }
            elif missing_sources:
                admission = {
                    "status": "BLOCKED", "next_state": "INPUT_BLOCKED",
                    "reason_code": "AUTHORITATIVE_SOURCE_MISSING",
                    "next_action": "Restore the listed authoritative sources without synthesizing content.",
                }
            else:
                admission = {
                    "status": "ADMISSIBLE", "next_state": "PREFLIGHT",
                    "reason_code": None,
                    "next_action": "Supply a matching RunIntent and evaluate deterministic preflight before Go.",
                }
            catalog.append({
                "day": day,
                "title": contract.title,
                "objective": contract.objective,
                "contract_version": contract.version,
                "contract_fingerprint": self._contract_fingerprint(contract),
                "required_evidence": required_evidence,
                "source_scope": source_scope,
                "preflight_inputs": list(self.CATALOG_PREFLIGHT_INPUTS),
                "admission": admission,
                "execution_started": False,
            })
        return catalog

    @classmethod
    def _blocked_catalog_day(cls, day: int, reason_code: str, next_action: str) -> dict[str, object]:
        return {
            "day": day,
            "title": None,
            "objective": None,
            "contract_version": None,
            "contract_fingerprint": None,
            "required_evidence": [],
            "source_scope": [],
            "preflight_inputs": list(cls.CATALOG_PREFLIGHT_INPUTS),
            "admission": {
                "status": "BLOCKED",
                "next_state": "INPUT_BLOCKED",
                "reason_code": reason_code,
                "next_action": next_action,
            },
            "execution_started": False,
        }

    def admission(
        self,
        intent: RunIntent | None,
        *,
        effective_permission: bool | None,
        external_prerequisite: bool | None,
    ) -> dict[str, object]:
        """Evaluate Go admission without selecting, starting, or mutating a Day."""
        if intent is None:
            return day_admission(
                self.root, intent=None, expected_day=1, expected_contract_fingerprint="UNAVAILABLE",
                effective_permission=effective_permission, external_prerequisite=external_prerequisite,
            ).view()
        contract = self._load_contracts().get(intent.selected_day)
        if contract is None:
            return {
                "status": "BLOCKED",
                "next_state": "HUMAN_ACTION_REQUIRED",
                "reason_code": "DAY_NOT_CONFIGURED",
                "next_action": "Select a configured Day before Go.",
                "git_fingerprint": None,
            }
        return day_admission(
            self.root,
            intent=intent,
            expected_day=contract.day,
            expected_contract_fingerprint=self._contract_fingerprint(contract),
            effective_permission=effective_permission,
            external_prerequisite=external_prerequisite,
        ).view()

    def prepare_go(
        self,
        day: int,
        *,
        effective_permission: bool | None = None,
        external_prerequisite: bool | None = None,
    ) -> dict[str, object]:
        """Create one immutable RunIntent and evaluate preflight without starting a Day.

        The public API supplies only ``day``. Trusted permission and external
        prerequisite facts remain server-owned and therefore default to unknown.
        Tests may inject observed values at this internal boundary.
        """
        contract = self._load_contracts().get(day)
        if contract is None:
            return {
                "error_code": "DAY_NOT_CONFIGURED",
                "selected_day": day,
                "run_id": None,
                "run_intent": None,
                "admission": {
                    "status": "BLOCKED", "next_state": "INPUT_BLOCKED",
                    "reason_code": "DAY_NOT_CONFIGURED",
                    "next_action": "Select a configured Day before Go.",
                },
                "execution_started": False,
                "snapshot": self.view(),
            }
        try:
            repository_fingerprint = git_fingerprint(
                self.root,
                self.approved_day_one_snapshot_paths if day == 1 else frozenset(),
            )
        except (GitSafetyError, OSError):
            return {
                "error_code": "GIT_BASELINE_UNAVAILABLE",
                "selected_day": day,
                "run_id": None,
                "run_intent": None,
                "admission": {
                    "status": "BLOCKED", "next_state": "HUMAN_ACTION_REQUIRED",
                    "reason_code": "GIT_BASELINE_UNAVAILABLE",
                    "next_action": "Restore a readable Git baseline without resetting user work.",
                },
                "execution_started": False,
                "snapshot": self.view(),
            }
        intent = RunIntent(
            run_id=f"run-{uuid4().hex}",
            selected_day=day,
            go_at=datetime.now(timezone.utc),
            contract_fingerprint=self._contract_fingerprint(contract),
            policy_fingerprint=self._file_fingerprint(
                Path(__file__).resolve().parents[2] / "docs" / "WORKING_RULES.md"),
            config_fingerprint=self._file_fingerprint(self.PROGRAM_PATH),
            git_fingerprint=repository_fingerprint,
            requested_limits=RunLimits(
                active_work_seconds=MAX_ACTIVE_WORK_SECONDS,
                max_attempts=MAX_ATTEMPTS,
                max_cost=0,
                currency="JPY",
            ),
        )
        admission = day_admission(
            self.root,
            intent=intent,
            expected_day=day,
            expected_contract_fingerprint=intent.contract_fingerprint,
            effective_permission=effective_permission,
            external_prerequisite=external_prerequisite,
        ).view()
        return {
            "run_id": intent.run_id,
            "selected_day": day,
            "run_intent": intent.model_dump(mode="json"),
            "admission_run_id": intent.run_id,
            "admission": admission,
            "execution_started": False,
            "snapshot": self.view(),
        }

    def day_one_read_only_diagnostic(self) -> dict[str, object]:
        """Collect inspectable Day 1 facts without running the regression suite."""
        contract = self._load_contracts().get(1)
        if contract is None:
            return {"error_code": "DAY_NOT_CONFIGURED"}
        return self._collect_day_one_evidence(contract, execute_tests=False)

    def view(self) -> dict[str, object]:
        with self._lock:
            return {**self.snapshot.model_dump(mode="json"), "recommended_action": self._recommended_action(),
                    "enabled_controls": self._enabled_controls(), "blocker": self.snapshot.authority_blocker.model_dump(mode="json") if self.snapshot.authority_blocker else None}

    def _transition(self, state: LocalLLMDayState) -> None:
        validate_transition(self.snapshot.state, state)
        self.snapshot.state = state
        self.snapshot.phase = state.value
        self.snapshot.state_history.append(state.value)
        self._save()

    def _blocker_resolved(self) -> bool:
        blocker, contract = self.snapshot.authority_blocker, self.snapshot.contract
        if not blocker or not contract:
            return False
        inventory = self._inventory(contract)
        if (contract.day == 1 and blocker.reason_code == "REPAIR_SCOPE_AUTHORITY_REQUIRED"
                and blocker.action_template_id == "READ_ONLY_COLLECT"
                and blocker.evidence_type):
            current_observation = self._inventory_fingerprint(inventory)
            return any(record.day == 1 and record.evidence_type == blocker.evidence_type
                       and record.validator_result and record.observation_fingerprint != current_observation
                       for record in self.snapshot.evidence_store.values())
        if blocker.resolution_strategy == "AUTHORITATIVE_SOURCES":
            return not inventory["missing_sources"]
        if blocker.resolution_strategy == "RETAINED_EVIDENCE" and blocker.evidence_type:
            if (contract.day == 1 and blocker.reason_code == "UNAPPROVED_SOURCE_PATHS"
                    and blocker.action_template_id == "D1_BASELINE_CHECKPOINT_V2"
                    and self.approved_day_one_snapshot_paths):
                return not unsafe_paths(self.root, self.approved_day_one_snapshot_paths)
            record = inventory.get("retained_evidence", {}).get(blocker.evidence_type)
            return self.evidence_registry.validate(blocker.evidence_type, record)
        if blocker.resolution_strategy == "EXTERNAL_REVIEW_PREREQUISITE":
            if self.external_review.external_prerequisite_resolved(blocker.reason_code):
                return True
            return self.authority_resolver(blocker) is True if self.authority_resolver else False
        if self.authority_resolver:
            return self.authority_resolver(blocker) is True
        return False

    def _replacement_retry_is_eligible(self, blocker: AuthorityBlocker, contract: LocalLLMDayContract,
                                       inventory: dict[str, object]) -> bool:
        """Allow only an explicitly superseded failed action to reach its replacement.

        A retained-evidence blocker normally remains a hard authority boundary.  This
        exception is deliberately narrower: the persisted failed attempt must match
        the current Day/gap/input, and the registry must now expose a different,
        currently selectable template for that exact gap.
        """
        if (blocker.resolution_strategy != "RETAINED_EVIDENCE" or not blocker.criterion_id
                or not blocker.evidence_type or not blocker.action_template_id):
            return False
        strategy = STRATEGIES.get((contract.day, blocker.evidence_type))
        if not strategy or not strategy.template or strategy.template.template_id == blocker.action_template_id:
            return False
        diagnoses = self._diagnose_gaps(contract, inventory)
        diagnosis = next((item for item in diagnoses if item.criterion_id == blocker.criterion_id
                          and item.evidence_type == blocker.evidence_type), None)
        if not diagnosis or diagnosis.classification != DayIssueClassification.PRODUCE_DAY_EVIDENCE:
            return False
        old_attempt = next((attempt for attempt in self.snapshot.action_attempts
                            if attempt.action_template_id == blocker.action_template_id
                            and attempt.strategy_id == strategy.strategy_id
                            and attempt.criterion_id == diagnosis.criterion_id
                            and attempt.evidence_type == diagnosis.evidence_type
                            and attempt.input_fingerprint == diagnosis.input_fingerprint), None)
        if old_attempt is None:
            return False
        return self._select_registered_action([diagnosis]) is not None

    def _known_infrastructure_recovery_available(self) -> bool:
        """Permit only the audited Day 1 failure caused by the fixed audit enum omission."""
        blocker = self.snapshot.authority_blocker
        if (self.snapshot.selected_day != 1 or self.snapshot.state != LocalLLMDayState.FAILED_UNRECOVERABLE
                or self.snapshot.activity != self.KNOWN_INFRASTRUCTURE_RECOVERY_FAILURE
                or self.snapshot.baseline_checkpoint is not None or self.snapshot.contract is None
                or self.snapshot.contract.day != 1 or blocker is None
                or blocker.action_template_id != "D1_BASELINE_CHECKPOINT"):
            return False
        if self.KNOWN_INFRASTRUCTURE_RECOVERY_EVENT not in AuditEventType._value2member_map_:
            return False
        if any(attempt.action_template_id == "D1_BASELINE_CHECKPOINT_V2"
               for attempt in self.snapshot.action_attempts):
            return False
        if any(record.day == 1 and record.evidence_type == "commit_ref" and record.validator_result
               for record in self.snapshot.evidence_store.values()):
            return False
        return any(attempt.action_template_id == "D1_BASELINE_CHECKPOINT"
                   for attempt in self.snapshot.action_attempts)

    def _repair_resumable(self) -> bool:
        if self.snapshot.state != LocalLLMDayState.PAUSED or not self.snapshot.contract:
            return False
        if not self.snapshot.repair_episode_ids:
            return False
        episode = self.repair_episode_store.get(self.snapshot.repair_episode_ids[-1])
        item = next((item for item in self.snapshot.work_items if episode and item.item_id == episode.work_item_id), None)
        return bool(episode and item and episode.interrupted and not episode.final_outcome
                    and episode.contract_version == self.snapshot.contract.version
                    and episode.scope_fingerprint == self._text_fingerprint(item.dynamic_work_order.model_dump_json() if item.dynamic_work_order else "")
                    and episode.failure_fingerprint == self._failure_fingerprint(item, episode.failure_excerpt))

    def _external_review_operator_resume_episode(self) -> RepairEpisode | None:
        """Return the single provider-failure episode an operator may explicitly retry."""
        blocker, contract = self.snapshot.authority_blocker, self.snapshot.contract
        if (self.snapshot.state != LocalLLMDayState.EXTERNAL_ACTION_REQUIRED or not blocker or not contract
                or blocker.resolution_strategy != "EXTERNAL_REVIEW_PREREQUISITE"
                or blocker.reason_code not in self.RECOVERABLE_EXTERNAL_REVIEW_FAILURES
                or not self.snapshot.repair_episode_ids):
            return None
        episode = self.repair_episode_store.get(self.snapshot.repair_episode_ids[-1])
        item = next((value for value in self.snapshot.work_items if episode and value.item_id == episode.work_item_id), None)
        if not episode or not item or not item.dynamic_work_order:
            return None
        if (episode.external_review_failure_code != blocker.reason_code
                or episode.external_review_resume_attempts >= 1
                or episode.contract_version != contract.version
                or episode.day != contract.day
                or episode.scope_fingerprint != self._text_fingerprint(item.dynamic_work_order.model_dump_json())
                or episode.failure_fingerprint != self._failure_fingerprint(item, episode.failure_excerpt)):
            return None
        return episode

    def _begin_external_review_operator_resume(self, episode: RepairEpisode) -> None:
        """Authorize one operator-triggered provider retry without widening the repair scope."""
        episode.external_review_resume_attempts += 1
        episode.external_review_artifact = None
        episode.external_review_outcome = None
        episode.external_review_failure_code = None
        episode.external_review_phase = None
        episode.final_outcome = None
        episode.interrupted = True
        self.repair_episode_store.save(episode)
        self._operator_external_resume_episode_id = episode.episode_id
        self.snapshot.authority_blocker = None
        self._audit("EXTERNAL_REVIEW_OPERATOR_RESUME", {
            "episode_id": episode.episode_id, "day": episode.day,
            "scope_fingerprint": episode.scope_fingerprint,
            "resume_attempt": episode.external_review_resume_attempts,
        })
        self._save()

    def _enabled_controls(self) -> dict[str, bool]:
        state = self.snapshot.state
        repair = self._repair_resumable()
        return {"go": state == LocalLLMDayState.IDLE,
                "smoke": state == LocalLLMDayState.IDLE,
                "resume": self._known_infrastructure_recovery_available()
                          or (state in {LocalLLMDayState.PAUSED, LocalLLMDayState.STOPPED} and not repair)
                          or (state in AUTHORITY and (self._blocker_resolved()
                              or self._external_review_operator_resume_episode() is not None)),
                "repair_and_go": repair, "stop": state in ACTIVE,
                "select_day": state in {LocalLLMDayState.IDLE, LocalLLMDayState.COMPLETE}}

    def smoke(self, day: int) -> dict[str, object]:
        contract = self._load_contracts().get(day)
        if contract is None:
            return {"error_code": "DAY_NOT_CONFIGURED", **self.view()}
        inventory = self._inventory(contract)
        missing = inventory["missing_sources"]
        result = "SMOKE_PASS" if not missing else "SMOKE_SOURCE_MISSING"
        with self._lock:
            if self.snapshot.state != LocalLLMDayState.IDLE:
                return {"error_code": "SMOKE_NOT_PERMITTED", **self.view()}
            # Selecting a different contract is not a resume.  Do not permit
            # old work, repair state, or claimed evidence to cross that boundary.
            if self.snapshot.selected_day != day or self.snapshot.contract is None or self.snapshot.contract.version != contract.version:
                self.snapshot = LocalLLMDaySnapshot(selected_day=day, objective=contract.objective, contract=contract)
            else:
                self.snapshot.contract = self._restore_contract(contract, self.snapshot.contract)
                self.snapshot.work_items = self._valid_work_items(self.snapshot.work_items, contract)
            self.snapshot.smoke_report = LocalLLMDayReport(
                day=day, objective=contract.objective, result=result,
                summary="Authoritative sources and target Git state were inventoried; no Day task was started.",
                evidence=inventory,
            )
            self.snapshot.state = LocalLLMDayState.IDLE
            self.snapshot.activity = self.snapshot.smoke_report.summary
            self.snapshot.progress = 0
            self.snapshot.report = None
            self.snapshot.stop_reason = None
            self._save()
            return self.view()

    def select_day(self, day: int) -> dict[str, object]:
        with self._lock:
            if day not in self._load_contracts():
                return {"error_code": "DAY_NOT_CONFIGURED", **self.view()}
            if not self._enabled_controls()["select_day"]:
                return {"error_code": "DAY_SELECTION_NOT_PERMITTED", **self.view()}
            if self.snapshot.state == LocalLLMDayState.COMPLETE and day == self.snapshot.selected_day:
                return self.view()
            self.snapshot = LocalLLMDaySnapshot(selected_day=day)
            self._save()
            return self.view()

    def start(self, day: int) -> dict[str, object]:
        contracts = self._load_contracts()
        contract = contracts.get(day)
        if contract is None:
            return {"error_code": "DAY_NOT_CONFIGURED", **self.view()}
        with self._lock:
            if self.snapshot.state in self.ACTIVE_STATES:
                return {"error_code": "DAY_ALREADY_RUNNING", **self.view()}
            if self.snapshot.state == LocalLLMDayState.COMPLETE and day != self.snapshot.selected_day:
                self.snapshot = LocalLLMDaySnapshot()
            if self.snapshot.state != LocalLLMDayState.IDLE:
                return {"error_code": "DAY_START_NOT_PERMITTED", **self.view()}
            # Only same-version, validated same-Day evidence may survive Resume.
            if (self.snapshot.selected_day != day or self.snapshot.contract is None
                    or self.snapshot.contract.version != contract.version):
                self.snapshot = LocalLLMDaySnapshot(selected_day=day, objective=contract.objective, contract=contract)
            else:
                self.snapshot.contract = self._restore_contract(contract, self.snapshot.contract)
                self.snapshot.work_items = self._valid_work_items(self.snapshot.work_items, contract)
            self._stop.clear()
            if self.snapshot.contract_fingerprint is None:
                self.snapshot.contract_fingerprint = self._contract_fingerprint(self.snapshot.contract)
            self._transition(LocalLLMDayState.PREFLIGHT)
            self.snapshot.activity = "Loading the Day Contract and current trusted evidence."
            self.snapshot.stop_reason = None
            self._audit("LOCAL_LLM_DAY_STARTED", {"day": day, "objective": contract.objective})
            self._save()
            self._thread = Thread(target=self._execute, name=f"local-llm-day-{day}", daemon=True)
            self._thread.start()
            return self.view()

    def resume(self) -> dict[str, object]:
        with self._lock:
            infrastructure_recovery = self._known_infrastructure_recovery_available()
            if self.snapshot.selected_day is None or self.snapshot.state not in {
                LocalLLMDayState.PAUSED, LocalLLMDayState.STOPPED,
                LocalLLMDayState.HUMAN_ACTION_REQUIRED, LocalLLMDayState.EXTERNAL_ACTION_REQUIRED,
            } and not infrastructure_recovery:
                return {"error_code": "DAY_NOT_RESUMABLE", **self.view()}
            if infrastructure_recovery:
                day = self.snapshot.selected_day
                self._audit("LOCAL_LLM_DAY_STARTED", {
                    "day": day, "resume_mode": "AUTHORIZED_INFRASTRUCTURE_RECOVERY",
                    "prior_state": LocalLLMDayState.FAILED_UNRECOVERABLE.value,
                    "prior_failure": self.snapshot.activity,
                })
                self._transition(LocalLLMDayState.PREFLIGHT)
                self._stop.clear()
                self._save()
                self._thread = Thread(target=self._execute, name=f"local-llm-day-{day}-infrastructure-recovery", daemon=True)
                self._thread.start()
                return self.view()
            operator_episode = self._external_review_operator_resume_episode()
            if self.snapshot.state in AUTHORITY and operator_episode is not None:
                self._begin_external_review_operator_resume(operator_episode)
            elif self.snapshot.state not in AUTHORITY and not self._enabled_controls()["resume"]:
                return {"error_code": "DAY_BLOCKER_UNRESOLVED", **self.view()}
            day = self.snapshot.selected_day
            # Resume preserves the Day and rechecks the blocker; only a
            # resolved prerequisite proceeds past PREFLIGHT.
            self._transition(LocalLLMDayState.PREFLIGHT)
            self._stop.clear()
            self._save()
            self._thread = Thread(target=self._execute, name=f"local-llm-day-{day}-resume", daemon=True)
            self._thread.start()
            return self.view()

    def register_retained_evidence(self) -> dict[str, object]:
        """Persist only resolver-validated evidence for the selected Day.

        This is intentionally not a caller-supplied EvidenceRecord API: the
        resolver owns artifact discovery and provenance validation, while this
        method owns durable EvidenceRecord creation.
        """
        with self._lock:
            contract = self.snapshot.contract
            if self.snapshot.selected_day is None or contract is None:
                return {"error_code": "DAY_NOT_SELECTED", **self.view()}
            inventory = self._inventory(contract)
            retained = inventory.get("retained_evidence")
            if not isinstance(retained, dict) or not retained:
                return {"error_code": "NO_VALIDATED_RETAINED_EVIDENCE", **self.view()}
            before = set(self.snapshot.evidence_store)
            self._ingest_legacy_evidence(
                contract, retained, provider_id="retained-resolver",
                source_fingerprint=self._inventory_fingerprint(inventory),
            )
            self._evaluate_contract(contract, inventory)
            registered = sorted(set(self.snapshot.evidence_store) - before)
            self._audit("RETAINED_EVIDENCE_REGISTERED", {
                "day": contract.day, "evidence_types": sorted(retained), "record_ids": registered,
            })
            self._save()
            return {"registered_record_ids": registered, "registered_evidence_types": sorted(retained), **self.view()}

    def stop(self) -> dict[str, object]:
        with self._lock:
            if self.snapshot.state in self.ACTIVE_STATES:
                self._stop.set()
                self.snapshot.activity = "Stop requested; preserving the current task result."
                self._save()
        return self.view()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def repair_and_go(self) -> dict[str, object]:
        """Resume a persisted automatic repair episode only when interrupted."""
        with self._lock:
            if not self._repair_resumable():
                return {"error_code": "AUTONOMOUS_REPAIR_NOT_AVAILABLE", **self.view()}
            item = next((value for value in self.snapshot.work_items if value.state == LocalLLMWorkItemState.FAILED), None)
            contract = self.snapshot.contract
            if item is None or contract is None:
                return {"error_code": "REPAIR_TARGET_MISSING", **self.view()}
            self._transition(LocalLLMDayState.PREFLIGHT)
            self._stop.clear()
            self._thread = Thread(target=self._resume_repair, args=(item, contract), daemon=True)
            self._thread.start()
            return self.view()

    def _resume_repair(self, item: LocalLLMDayWorkItem, contract: LocalLLMDayContract) -> None:
        # Full preflight precedes resumed repair; the episode is selected in the
        # normal diagnosis loop and retains its absolute local-phase deadline.
        self._execute()

    def _supervise_repair(self, item: LocalLLMDayWorkItem, contract: LocalLLMDayContract) -> bool:
        """Run the entire bounded local-to-expert repair control loop."""
        excerpt = str(item.evidence.get("failure_excerpt", ""))[:2000]
        fingerprint = self._failure_fingerprint(item, excerpt)
        now = self.clock()
        matches = self.solution_catalog.find(
            project_id=self.project_id, failure_class=DayIssueClassification.IMPLEMENTATION_DEFECT.value,
            fingerprint=fingerprint, component=item.item_id,
        )
        existing = self.repair_episode_store.get(self.snapshot.repair_episode_ids[-1]) if self.snapshot.repair_episode_ids else None
        episode = existing if existing and not existing.final_outcome and existing.failure_fingerprint == fingerprint and existing.contract_version == contract.version else RepairEpisode(
            episode_id=uuid4().hex, project_id=self.project_id, day=contract.day,
            work_item_id=item.item_id, failure_class=DayIssueClassification.IMPLEMENTATION_DEFECT,
            failure_fingerprint=fingerprint, failure_excerpt=excerpt, component=item.item_id,
            started_at_epoch=now, repair_deadline_epoch=now + self.snapshot.repair_deadline_seconds,
            catalog_match_ids=[entry.catalog_id for entry in matches],
            contract_version=contract.version,
            scope_fingerprint=self._text_fingerprint(item.dynamic_work_order.model_dump_json() if item.dynamic_work_order else ""),
        )
        self.repair_episode_store.save(episode)
        if episode.episode_id not in self.snapshot.repair_episode_ids:
            self.snapshot.repair_episode_ids.append(episode.episode_id)
        self._save()
        files = self._bounded_repair_files(item)
        seen_proposals: set[str] = {p.proposal_fingerprint for p in episode.proposal_attempts}
        while episode.expert_solver_outcome is None and len(episode.proposal_attempts) < self.MAX_LOCAL_PROPOSALS and self.clock() < episode.repair_deadline_epoch:
            remaining = max(1, int(episode.repair_deadline_epoch - self.clock()))
            proposal = self.repair_builder.propose(
                failure_excerpt=excerpt, files=files, timeout_seconds=min(120, remaining),
                rejection_feedback="\n".join(episode.rejection_feedback[-2:]) or None,
                repair_knowledge=[self._catalog_guidance(entry) for entry in matches],
            )
            if proposal is None:
                episode.proposal_attempts.append(RepairProposalAttempt(proposal_fingerprint=self._text_fingerprint("NO_PROPOSAL"), outcome="NO_PROPOSAL", feedback="LocalLLM returned no bounded proposal."))
                episode.rejection_feedback.append("No parseable LocalLLM proposal was returned.")
                self.repair_episode_store.save(episode)
                break
            proposal_fingerprint = self._proposal_fingerprint(proposal)
            if proposal_fingerprint in seen_proposals:
                episode.proposal_attempts.append(RepairProposalAttempt(proposal_fingerprint=proposal_fingerprint, outcome="DUPLICATE", feedback="Duplicate proposal fingerprint."))
                episode.rejection_feedback.append("The same proposal fingerprint repeated; escalate independently.")
                self.repair_episode_store.save(episode)
                break
            seen_proposals.add(proposal_fingerprint)
            edits = getattr(proposal, "edits", ())
            if not edits or item.dynamic_work_order is None or any(getattr(edit, "path", "") not in item.dynamic_work_order.allowed_files for edit in edits):
                episode.proposal_attempts.append(RepairProposalAttempt(proposal_fingerprint=proposal_fingerprint, outcome="PREFILTER_REJECTED"))
                self.repair_episode_store.save(episode)
                break
            result = self.work_order_executor({
                "kind": "LOCAL_LLM_COUNTERMEASURE", "task_id": item.item_id, "proposal": proposal,
                "files": sorted(files), "dynamic_work_order": item.dynamic_work_order.model_dump(mode="json") if item.dynamic_work_order else None,
                "repair_episode_id": episode.episode_id,
            })
            if result.get("final_result") in {"COMPLETE", "COMPLETE_NO_CHANGE"} and result.get("verification_passed") is True:
                episode.proposal_attempts.append(RepairProposalAttempt(proposal_fingerprint=proposal_fingerprint, outcome="VERIFIED"))
                episode.codex_review_outcome = "ACCEPTED_AND_VERIFIED"
                episode.verification_result = "PASS"
                episode.final_outcome = "LOCAL_VERIFIED"
                self.repair_episode_store.save(episode)
                self._accept_repair(item, result, episode, source="LOCAL_VERIFIED", diagnosis=proposal.diagnosis)
                return True
            feedback = str(result.get("error_code") or result.get("final_result") or "CODEX_REJECTED_PROPOSAL")[:1000]
            episode.proposal_attempts.append(RepairProposalAttempt(proposal_fingerprint=proposal_fingerprint, outcome="REJECTED", feedback=feedback))
            episode.rejection_feedback.append(feedback)
            self.repair_episode_store.save(episode)
        expert: dict[str, object] = {"error_code": episode.expert_solver_outcome or "FAILED"}
        if episode.expert_solver_outcome is None:
            expert = self.work_order_executor({
                "kind": "CODEX_EXPERT_SOLVER", "task_id": item.item_id,
                "dynamic_work_order": item.dynamic_work_order.model_dump(mode="json") if item.dynamic_work_order else None,
                "failure_excerpt": excerpt, "failure_fingerprint": fingerprint,
                "catalog_matches": [self._catalog_guidance(entry) for entry in matches], "repair_episode_id": episode.episode_id,
            })
        if expert.get("final_result") in {"COMPLETE", "COMPLETE_NO_CHANGE"} and expert.get("verification_passed") is True:
            episode.expert_solver_outcome, episode.verification_result, episode.final_outcome = "VERIFIED", "PASS", "CODEX_VERIFIED"
            self._accept_repair(item, expert, episode, source="CODEX_VERIFIED", diagnosis="Codex Expert Solver independently repaired the verified failure.")
            return True
        if episode.expert_solver_outcome is None:
            episode.expert_solver_outcome = str(expert.get("error_code") or "FAILED")[:80]
            self.repair_episode_store.save(episode)
        return self._request_external_repair_guidance(item, contract, episode, matches, expert)

    def _request_external_repair_guidance(self, item: LocalLLMDayWorkItem, contract: LocalLLMDayContract,
                                          episode: RepairEpisode, matches: list[SolutionCatalogEntry],
                                          expert: dict[str, object]) -> bool:
        """Use one advisory external review without extending the repair order."""
        order = item.dynamic_work_order
        diagnosis = next((value for value in self.snapshot.gap_diagnoses
                          if value.criterion_id in item.criterion_ids and value.classification == DayIssueClassification.ENGINEERING_REPAIR), None)
        if order is None or diagnosis is None or not diagnosis.evidence_type:
            episode.final_outcome = "EXTERNAL_REVIEW_NOT_ELIGIBLE"
            self.repair_episode_store.save(episode)
            return False
        package = self.external_review.build_package(
            selected_day=contract.day, contract_version=contract.version,
            contract_fingerprint=self.snapshot.contract_fingerprint or self._contract_fingerprint(contract),
            criterion_id=diagnosis.criterion_id, evidence_type=diagnosis.evidence_type,
            gap_diagnosis=diagnosis.model_dump(mode="json"),
            failed_action=dict(self.snapshot.last_action or {"task_id": order.task_id}),
            allowed_files=order.allowed_files, context_files=order.context_files,
            acceptance_test_files=order.acceptance_test_files, failure_excerpt=item.evidence.get("failure_excerpt", ""),
            stdout_excerpt=expert.get("stdout", ""), stderr_excerpt=expert.get("stderr", ""),
            git_summary=self._external_review_git_summary(self._inventory(contract)),
            previous_attempts=[attempt.model_dump(mode="json") for attempt in episode.proposal_attempts],
            rejection_feedback=episode.rejection_feedback,
            catalog_matches=[self._catalog_guidance(entry) for entry in matches],
            expert_solver_outcome=episode.expert_solver_outcome or "FAILED",
            deterministic_verification_failure=expert.get("error_code") or expert.get("failure_excerpt") or "Expert Solver did not produce verified repair evidence.",
        )
        artifact = self.external_review.store.load_artifact(episode.external_review_artifact) if episode.external_review_artifact else None
        if artifact is not None and (artifact.package.selected_day != contract.day or artifact.package.repair_scope != package.repair_scope):
            artifact = None
        if episode.external_review_phase in {"SUBMISSION_STARTED", "ARTIFACT_RECORDED", "BUILDER_STARTED"} and artifact is None:
            episode.external_review_failure_code = "EXTERNAL_REVIEW_SUBMISSION_UNCERTAIN"
            episode.final_outcome = "EXTERNAL_REVIEW_UNRESOLVED"
            self.repair_episode_store.save(episode)
            return False
        if artifact is None:
            episode.external_review_fingerprint = package.fingerprint
            episode.external_review_phase = "SUBMISSION_STARTED"
            self.repair_episode_store.save(episode)
            artifact = self.external_review.request_review(package)
            episode.external_review_artifact = artifact.artifact_path
            episode.external_review_outcome = artifact.status
            episode.external_review_failure_code = artifact.failure_code
            episode.external_review_phase = "ARTIFACT_RECORDED"
            self.repair_episode_store.save(episode)
        review = artifact.normalized
        if artifact.status != "GUIDANCE_RECEIVED" or review is None or review.status != "REPAIR_GUIDANCE":
            episode.final_outcome = "EXTERNAL_REVIEW_UNRESOLVED"
            self.repair_episode_store.save(episode)
            return False
        episode.external_review_phase = "BUILDER_STARTED"
        self.repair_episode_store.save(episode)
        result = self.work_order_executor({
            "kind": "EXTERNAL_REVIEW_BUILDER", "task_id": item.item_id,
            "dynamic_work_order": order.model_dump(mode="json"),
            "failure_package": package.model_dump(mode="json"),
            "external_review": review.model_dump(mode="json"),
            "repair_episode_id": episode.episode_id,
        })
        if result.get("final_result") in {"COMPLETE", "COMPLETE_NO_CHANGE"} and result.get("verification_passed") is True:
            episode.external_review_builder_outcome, episode.verification_result, episode.final_outcome = "VERIFIED", "PASS", "EXTERNAL_REVIEW_VERIFIED"
            episode.external_review_phase = "BUILDER_VERIFIED"
            self.repair_episode_store.save(episode)
            self._accept_repair(item, result, episode, source="EXTERNAL_REVIEW_VERIFIED", diagnosis=review.diagnosis)
            return True
        episode.external_review_builder_outcome = str(result.get("error_code") or "FAILED")[:80]
        episode.verification_result = episode.external_review_builder_outcome
        episode.final_outcome = "EXTERNAL_REVIEW_BUILDER_FAILED"
        episode.external_review_phase = "BUILDER_FAILED"
        self.repair_episode_store.save(episode)
        return False

    @staticmethod
    def _external_review_git_summary(inventory: dict[str, object]) -> dict[str, object]:
        """Only bounded Git identity/status facts may leave the controller."""
        head = inventory.get("head")
        return {
            "branch": str(inventory.get("branch") or "")[:160],
            "head": str(head)[:64] if isinstance(head, str) and re.fullmatch(r"[0-9a-fA-F]{7,64}", head) else "",
            "working_tree_clean": inventory.get("working_tree_clean") is True,
            "status_count": min(max(int(inventory.get("status_count") or 0), 0), 10000),
        }

    def _external_repair_blocker(self, contract: LocalLLMDayContract, inventory: dict[str, object], item: LocalLLMDayWorkItem) -> None:
        diagnosis = next((value for value in self.snapshot.gap_diagnoses if value.criterion_id in item.criterion_ids), None)
        episode = self.repair_episode_store.get(self.snapshot.repair_episode_ids[-1]) if self.snapshot.repair_episode_ids else None
        if episode and episode.external_review_failure_code:
            self._terminal_blocker(contract, DayIssueClassification.EXTERNAL_AUTHORITY_REQUIRED,
                episode.external_review_failure_code,
                "The configured External Reviewer API prerequisite is unavailable; no source change was attempted.",
                inventory, diagnosis)
            return
        self._terminal_blocker(contract, DayIssueClassification.HUMAN_PRODUCT_DECISION_REQUIRED,
            "EXTERNAL_REPAIR_UNRESOLVED",
            "All bounded internal and external-guided repair routes were exhausted without verified evidence; an authority decision is required before expanding scope or criteria.",
            inventory, diagnosis)

    @staticmethod
    def _text_fingerprint(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()

    @staticmethod
    def _file_fingerprint(path: Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return "missing"

    def _failure_fingerprint(self, item: LocalLLMDayWorkItem, excerpt: str) -> str:
        return self._text_fingerprint(f"{item.item_id}|{item.dynamic_work_order.task_id if item.dynamic_work_order else ''}|{excerpt[:1200]}")

    def _inventory_fingerprint(self, inventory: dict[str, object]) -> str:
        material = {key: inventory.get(key) for key in ("head", "branch", "source_fingerprint", "configuration_fingerprint")}
        material["authority_resolution"] = self.snapshot.authority_resolution_fingerprint
        return self._text_fingerprint(json.dumps(material, sort_keys=True))

    def _proposal_fingerprint(self, proposal: object) -> str:
        edits = getattr(proposal, "edits", ())
        serialized = "|".join(f"{getattr(edit, 'path', '')}:{getattr(edit, 'find', '')}:{getattr(edit, 'replace', '')}" for edit in edits)
        return self._text_fingerprint(f"{getattr(proposal, 'diagnosis', '')}|{serialized}")

    @staticmethod
    def _catalog_guidance(entry: SolutionCatalogEntry) -> dict[str, object]:
        return {
            "catalog_id": entry.catalog_id, "cause": entry.root_cause,
            "investigation": entry.diagnostic_steps, "resolution_logic": entry.resolution_strategy,
            "verification": entry.verification, "preconditions": entry.preconditions,
        }

    def _accept_repair(self, item: LocalLLMDayWorkItem, result: dict[str, object], episode: RepairEpisode, *, source: str, diagnosis: str) -> None:
        entry = SolutionCatalogEntry(
            scope="project", project_id=self.project_id,
            failure_class=episode.failure_class.value, failure_fingerprint=episode.failure_fingerprint,
            component=episode.component, title=f"Verified repair for {item.item_id}",
            symptoms=episode.failure_excerpt or "deterministic engineering failure",
            root_cause=diagnosis[:1000], diagnostic_steps="Inspect the bounded failure excerpt and configured source/test scope.",
            resolution_strategy="Apply only an in-scope repair through the guarded worktree executor.",
            preconditions=["IMPLEMENTATION_DEFECT", "deterministic postcheck"],
            affected_files_or_scope=list(item.dynamic_work_order.allowed_files) if item.dynamic_work_order else [],
            verification="Configured deterministic postcheck passed.", source=source,
            success_count=1,
        )
        entry = self.solution_catalog.add_verified(entry)
        episode.catalog_update_id = entry.catalog_id
        self.repair_episode_store.save(episode)
        self.snapshot.repair_knowledge.append(LocalLLMRepairCard(
            problem_id=entry.catalog_id, title=entry.title, cause=entry.root_cause,
            investigation=entry.diagnostic_steps, resolution_logic=entry.resolution_strategy,
            verification=entry.verification, status=entry.status, uses=entry.uses,
            failure_class=entry.failure_class, failed_work_item=item.item_id,
        ))
        item.state = LocalLLMWorkItemState.COMPLETE
        item.evidence = self._bounded(result)
        # Display telemetry is bounded by key count; the adapter's evidence
        # payload is a separate contract and must never be truncated by the
        # insertion order of the engine result. It still passes the normal
        # Evidence Registry / Store ingestion and criterion validators below.
        evidence = result.get("evidence")
        if isinstance(evidence, dict):
            item.evidence["evidence"] = dict(evidence)
        self.snapshot.repair_attempted = True
        self.snapshot.issue_classification = None
        self.snapshot.codex_handoff = None
        self._save()

    def _execute(self) -> None:
        try:
            contract = self.snapshot.contract
            current = self._load_contracts().get(self.snapshot.selected_day)
            if not contract or not current:
                raise GitSafetyError("CONTRACT_VERSION_INVARIANT")
            expected = self.snapshot.contract_fingerprint or self._contract_fingerprint(contract)
            if self._contract_fingerprint(current) != expected or self._contract_fingerprint(contract) != expected:
                reason = "CONTRACT_VERSION_CONTENT_MISMATCH" if current.version == contract.version else "CONTRACT_VERSION_CHANGED"
                self._invalidate_contract_identity(current, expected, reason)
                raise GitSafetyError(reason)
            self.snapshot.contract_fingerprint = expected
            self._assert_registry_conformance()
            inventory = self._inventory(contract)
            blocker = self.snapshot.authority_blocker
            if blocker:
                if not self._blocker_resolved():
                    if not self._replacement_retry_is_eligible(blocker, contract, inventory):
                        self._fail(contract, blocker.reason_code, blocker.message, blocker.classification, inventory)
                        return
                    self._audit("AUTHORITY_BLOCKER_REPLACED_BY_REGISTERED_RETRY", {
                        "day": contract.day, "criterion_id": blocker.criterion_id,
                        "evidence_type": blocker.evidence_type,
                        "previous_template_id": blocker.action_template_id,
                    })
                    self.snapshot.authority_blocker = None
                    self._save()
                else:
                    self.snapshot.authority_resolution_fingerprint = self._text_fingerprint(
                        blocker.model_dump_json() + self._inventory_fingerprint(inventory))
                    self.snapshot.authority_blocker = None
                    self._save()
            if inventory["missing_sources"]:
                self._terminal_blocker(contract, DayIssueClassification.EXTERNAL_AUTHORITY_REQUIRED,
                                       "AUTHORITATIVE_SOURCE_MISSING", "Required authoritative sources are unavailable.", inventory)
                return
            self._transition(LocalLLMDayState.LOADING_CONTRACT)
            self._transition(LocalLLMDayState.INVENTORY)
            self._ingest_reusable_evidence(contract, inventory)
            self._transition(LocalLLMDayState.VALIDATING)
            while not self._stop.is_set():
                self._evaluate_contract(contract, inventory)
                if not contract.remaining_gaps:
                    self._complete(contract, inventory)
                    return
                if self._operator_external_resume_episode_id:
                    episode = self.repair_episode_store.get(self._operator_external_resume_episode_id)
                    item = next((value for value in self.snapshot.work_items
                                 if episode and value.item_id == episode.work_item_id), None)
                    self._operator_external_resume_episode_id = None
                    if not episode or not item or not episode.interrupted or episode.final_outcome:
                        self._terminal_blocker(contract, DayIssueClassification.EXTERNAL_AUTHORITY_REQUIRED,
                                               "EXTERNAL_REVIEW_RESUME_SCOPE_INVALID",
                                               "The requested External Reviewer retry no longer matches the persisted repair scope.", inventory)
                        return
                    self._transition(LocalLLMDayState.DIAGNOSING_GAP)
                    self._transition(LocalLLMDayState.REPAIR_SUPERVISOR)
                    episode.interrupted = False
                    self.repair_episode_store.save(episode)
                    if not self._supervise_repair(item, contract):
                        self._external_repair_blocker(contract, inventory, item)
                        return
                    self._ingest_legacy_evidence(contract, item.evidence.get("evidence", {}),
                                                 provider_id="repair-supervisor",
                                                 source_fingerprint=self._inventory_fingerprint(inventory))
                    self._transition(LocalLLMDayState.REVALIDATING)
                    inventory = self._inventory(contract)
                    self._transition(LocalLLMDayState.VALIDATING)
                    continue
                self._transition(LocalLLMDayState.DIAGNOSING_GAP)
                diagnoses = self._diagnose_gaps(contract, inventory)
                self.snapshot.gap_diagnoses = diagnoses
                self._save()
                episode = self.repair_episode_store.get(self.snapshot.repair_episode_ids[-1]) if self.snapshot.repair_episode_ids else None
                if episode and episode.interrupted and not episode.final_outcome:
                    item = next(item for item in self.snapshot.work_items if item.item_id == episode.work_item_id)
                    self._transition(LocalLLMDayState.REPAIR_SUPERVISOR)
                    episode.interrupted = False
                    if not self._supervise_repair(item, contract):
                        self._external_repair_blocker(contract, inventory, item)
                        return
                    self._ingest_legacy_evidence(contract, item.evidence.get("evidence", {}), provider_id="repair-supervisor", source_fingerprint=self._inventory_fingerprint(inventory))
                    self._transition(LocalLLMDayState.REVALIDATING)
                    inventory = self._inventory(contract)
                    self._transition(LocalLLMDayState.VALIDATING)
                    continue
                selected = self._select_registered_action(diagnoses)
                if selected is None:
                    authority = next((item for item in diagnoses if item.classification in {
                        DayIssueClassification.EXTERNAL_AUTHORITY_REQUIRED,
                        DayIssueClassification.HUMAN_PRODUCT_DECISION_REQUIRED,
                    }), None)
                    if authority:
                        self._terminal_blocker(contract, authority.classification,
                            authority.authority_basis or "AUTHORITY_REQUIRED", authority.reason, inventory, authority)
                    else:
                        self._route_observed_gap(contract, inventory, diagnoses[0])
                        if self.snapshot.state in ACTIVE:
                            inventory = self._inventory(contract)
                            self._transition(LocalLLMDayState.VALIDATING)
                            continue
                    return
                self._execute_registered_action(contract, inventory, selected)
                if self.snapshot.state not in ACTIVE:
                    return
                inventory = self._inventory(contract)
                self._ingest_reusable_evidence(contract, inventory)
                self._transition(LocalLLMDayState.VALIDATING)
            self._transition(LocalLLMDayState.STOPPED)
            self.snapshot.stop_reason = "STOP_REQUESTED"
            self.snapshot.activity = "Stopped at a durable action boundary; Resume continues this Day."
            self._save()
        except Exception as exc:
            if self.snapshot.contract:
                code = str(exc) if isinstance(exc, GitSafetyError) and str(exc).startswith("CONTRACT_VERSION") else "CONTROLLER_INVARIANT"
                self._fail(self.snapshot.contract, code,
                           f"Controller cannot safely continue: {type(exc).__name__}: {str(exc)[:300]}",
                           DayIssueClassification.ENGINEERING_REPAIR, {})

    def _diagnose_gaps(self, contract: LocalLLMDayContract, inventory: dict[str, object]) -> list[GapDiagnosis]:
        input_fingerprint = self._inventory_fingerprint(inventory)
        diagnoses: list[GapDiagnosis] = []
        for criterion in contract.completion_criteria:
            if criterion.satisfied:
                continue
            for evidence_type in criterion.required_evidence:
                if criterion.evidence_record_ids.get(evidence_type) and self._evidence_record_valid(evidence_type, self.snapshot.evidence_store.get(criterion.evidence_record_ids[evidence_type])):
                    continue
                strategy = STRATEGIES[(contract.day, evidence_type)]
                action_fingerprint = self._text_fingerprint(
                    f"{self.project_id}|{contract.version}|{contract.day}|{criterion.criterion_id}|{evidence_type}|{strategy.template.template_id if strategy.template else strategy.strategy_id}|{input_fingerprint}"
                )
                attempted = [attempt.action_fingerprint for attempt in self.snapshot.action_attempts if attempt.criterion_id == criterion.criterion_id and attempt.evidence_type == evidence_type]
                try:
                    classification = DayIssueClassification(strategy.classification)
                except ValueError as exc:
                    raise RuntimeError("invalid server strategy classification") from exc
                previous = next((attempt for attempt in reversed(self.snapshot.action_attempts)
                                 if strategy.template and attempt.action_template_id == strategy.template.template_id
                                 and attempt.input_fingerprint == input_fingerprint), None)
                failure_reason = "NOT_YET_PRODUCED"
                if previous:
                    failure_reason = previous.failure_reason or "ACTION_OUTPUT_FAILED_EVIDENCE_VALIDATION"
                    classification = previous.observed_classification or DayIssueClassification.ENGINEERING_REPAIR
                    if classification == DayIssueClassification.INSUFFICIENT_EVIDENCE:
                        classification = DayIssueClassification.ENGINEERING_REPAIR
                diagnoses.append(GapDiagnosis(
                    criterion_id=criterion.criterion_id, evidence_type=evidence_type,
                    classification=classification, failure_reason=failure_reason,
                    reason=f"{evidence_type} has no compatible validated Evidence Record: {failure_reason}.",
                    required_evidence=[evidence_type], strategy_id=strategy.strategy_id,
                    authority_basis=strategy.authority_requirement, input_fingerprint=input_fingerprint,
                    action_fingerprint=action_fingerprint, expected_information_gain=strategy.expected_information_gain,
                    expected_state_change=strategy.expected_state_change, attempted_action_fingerprints=attempted,
                ))
        return diagnoses

    def _select_registered_action(self, diagnoses: list[GapDiagnosis]) -> GapDiagnosis | None:
        priority = {
            ExecutionMode.READ_ONLY: 0, ExecutionMode.BASELINE_CHECKPOINT: 1,
            ExecutionMode.ENGINEERING_WORKTREE: 2, ExecutionMode.RESEARCH_RUN: 2,
            ExecutionMode.DECISION_OR_DOCUMENTATION_WORKTREE: 2,
        }
        candidates: list[tuple[int, str, str, GapDiagnosis]] = []
        attempted = {item.action_fingerprint for item in self.snapshot.action_attempts}
        for diagnosis in diagnoses:
            strategy = STRATEGIES[(self.snapshot.contract.day, diagnosis.evidence_type)] if self.snapshot.contract and diagnosis.evidence_type else None
            authorized_scope_retry = self._authorized_day_one_scope_retry(diagnosis, strategy)
            if strategy is None or strategy.template is None or (diagnosis.action_fingerprint in attempted and not authorized_scope_retry):
                continue
            # A prerequisite is never produced by automatically running its Day.
            if any(any(gap.evidence_type == name for gap in diagnoses) for name in strategy.template.input_evidence_types):
                continue
            if not authorized_scope_retry and any(attempt.action_template_id == strategy.template.template_id
                   and attempt.input_fingerprint == diagnosis.input_fingerprint
                   for attempt in self.snapshot.action_attempts):
                continue
            candidates.append((priority[strategy.template.execution_mode], strategy.strategy_id, diagnosis.criterion_id, diagnosis))
        return sorted(candidates, key=lambda item: item[:3])[0][3] if candidates else None

    def _authorized_day_one_scope_retry(self, diagnosis: GapDiagnosis, strategy) -> bool:
        if (not strategy or not strategy.template or self.snapshot.selected_day != 1
                or strategy.template.template_id != "D1_BASELINE_CHECKPOINT_V2"
                or diagnosis.evidence_type != "commit_ref" or not self.approved_day_one_snapshot_paths
                or unsafe_paths(self.root, self.approved_day_one_snapshot_paths)):
            return False
        attempts = [attempt for attempt in self.snapshot.action_attempts
                    if attempt.action_template_id == "D1_BASELINE_CHECKPOINT_V2"]
        return (any(attempt.outcome == "HUMAN_ACTION_REQUIRED"
                    and attempt.failure_reason == "UNAPPROVED_SOURCE_PATHS"
                    for attempt in attempts)
                and not any(attempt.outcome == "COMPLETE" for attempt in attempts))

    def _route_observed_gap(self, contract, inventory, diagnosis):
        """Repair an observed adapter/work defect, never retry an unchanged action.

        Missing output after work is different from work not yet attempted.
        Repair authority remains the registered template's exact source/test
        scope. Read-only/research templates cannot authorize source edits.
        """
        strategy = STRATEGIES[(contract.day, diagnosis.evidence_type)]
        template = strategy.template
        if diagnosis.classification == DayIssueClassification.MODEL_QUALITY_FINDING:
            self._terminal_blocker(contract, DayIssueClassification.HUMAN_PRODUCT_DECISION_REQUIRED,
                "RESEARCH_CONDITION_EXPANSION_REQUIRED",
                "The retained model finding does not satisfy this requirement. Repeating inference is forbidden; a new research condition requires an explicit decision.", inventory, diagnosis)
            return
        tests = [path for path in template.allowed_output_scope if path.startswith("tests/") and path.endswith(".py")] if template else []
        if not template or template.execution_mode != ExecutionMode.ENGINEERING_WORKTREE or not tests:
            self._terminal_blocker(contract, DayIssueClassification.HUMAN_PRODUCT_DECISION_REQUIRED,
                "REPAIR_SCOPE_AUTHORITY_REQUIRED",
                "The registered collector/result adapter did not produce valid evidence. Its current read-only/results-only scope does not authorize the source changes needed to repair it.", inventory, diagnosis)
            return
        item_id = f"repair-{diagnosis.strategy_id}"
        # Never repeat an already verified repair against the identical inputs.
        if any(item.item_id == item_id for item in self.snapshot.work_items):
            self._terminal_blocker(contract, DayIssueClassification.HUMAN_PRODUCT_DECISION_REQUIRED,
                "REPAIR_SCOPE_AUTHORITY_REQUIRED",
                "The bounded repair was verified but the evidence adapter remains invalid. Repeating the same repair is forbidden; repairing outside the registered source/test scope needs authority.", inventory, diagnosis)
            return
        self._ingest_action_result(contract, diagnosis, {
            "final_result": "FAILED", "issue_classification": "ENGINEERING_REPAIR",
            "failure_excerpt": diagnosis.reason,
            "dynamic_work_order": {
                "task_id": f"day-{contract.day}-{template.template_id.lower().replace('_', '-')}",
                "allowed_files": list(template.allowed_output_scope),
                "context_files": list(template.context_scope), "acceptance_test_files": tests,
            },
        })
        if self.snapshot.state in ACTIVE:
            self._transition(LocalLLMDayState.REVALIDATING)

    def _execute_registered_action(self, contract: LocalLLMDayContract, inventory: dict[str, object], diagnosis: GapDiagnosis) -> None:
        assert diagnosis.evidence_type and diagnosis.strategy_id and diagnosis.action_fingerprint
        strategy = STRATEGIES[(contract.day, diagnosis.evidence_type)]
        assert strategy.template is not None
        self.snapshot.active_action = {"strategy_id": strategy.strategy_id, "template_id": strategy.template.template_id, "evidence_type": diagnosis.evidence_type, "criterion_id": diagnosis.criterion_id, "execution_mode": strategy.template.execution_mode.value}
        self.snapshot.phase = strategy.template.execution_mode.value
        self._transition(LocalLLMDayState.COLLECTING_EVIDENCE if strategy.template.execution_mode == ExecutionMode.READ_ONLY else LocalLLMDayState.EXECUTING_DAY_WORK)
        self.snapshot.activity = f"Executing server-owned {strategy.template.execution_mode.value} for {diagnosis.evidence_type}."
        self._save()
        attempt = ActionAttempt(action_fingerprint=diagnosis.action_fingerprint, strategy_id=strategy.strategy_id,
            criterion_id=diagnosis.criterion_id, evidence_type=diagnosis.evidence_type,
            input_fingerprint=diagnosis.input_fingerprint, outcome="STARTED", action_template_id=strategy.template.template_id)
        self.snapshot.action_attempts.append(attempt)
        self._save()
        if strategy.template.execution_mode == ExecutionMode.READ_ONLY:
            has_current_test_result = any(
                record.day == 1 and record.evidence_type == "test_result"
                and record.observation_fingerprint == self._inventory_fingerprint(inventory)
                and self._evidence_record_valid("test_result", record)
                for record in self.snapshot.evidence_store.values()
            )
            result = self._collect_day_one_evidence(contract, execute_tests=not has_current_test_result) if contract.day == 1 else self.work_order_executor({"kind": "DAY_ACTION_TEMPLATE", "strategy_id": strategy.strategy_id, "day": contract.day})
        elif strategy.template.execution_mode == ExecutionMode.BASELINE_CHECKPOINT:
            result = self._create_day_one_baseline_checkpoint(contract)
        else:
            result = self.work_order_executor({
                "kind": "DAY_ACTION_TEMPLATE", "strategy_id": strategy.strategy_id,
                "day": contract.day,
                "action_template_id": strategy.template.template_id,
                "execution_mode": strategy.template.execution_mode.value,
                "criterion_id": diagnosis.criterion_id, "evidence_type": diagnosis.evidence_type,
                "criterion_ids": [diagnosis.criterion_id],
                "contract": contract.model_dump(mode="json"), "allowed_output_scope": list(strategy.template.allowed_output_scope),
                "mutation_policy": strategy.template.mutation_policy,
            })
        attempt.outcome = str(result.get("final_result", "UNKNOWN"))
        attempt.observed_classification = self._classify_result(result)
        attempt.failure_reason = str(result.get("error_code") or "ACTION_OUTPUT_FAILED_EVIDENCE_VALIDATION")[:500]
        self._ingest_action_result(contract, diagnosis, result)
        self.snapshot.last_action = self.snapshot.active_action
        self.snapshot.active_action = None
        if self.snapshot.state not in ACTIVE:
            self._save()
            return
        self._transition(LocalLLMDayState.REVALIDATING)
        self._save()

    def _terminal_blocker(self, contract: LocalLLMDayContract, classification: DayIssueClassification, reason_code: str, message: str, inventory: dict[str, object], diagnosis: GapDiagnosis | None = None) -> None:
        resolution = {"AUTHORITATIVE_SOURCE_MISSING": "AUTHORITATIVE_SOURCES",
                      "REAL_MODE_REQUIRED": "RUNTIME_REAL",
                      "RESEARCH_CONDITION_REQUIRED": "RESEARCH_CONDITION",
                      "RESEARCH_CONDITION_NOT_APPROVED": "RESEARCH_CONDITION",
                      "RESEARCH_CONDITION_CHOICE_REQUIRED": "RESEARCH_CONDITION",
                      "RESEARCH_CONDITION_CHANGED": "RESEARCH_CONDITION",
                      "SOURCE_TASK_DEPENDENCY_DIRTY": "SOURCE_DEPENDENCIES",
                      "REPAIR_SCOPE_AUTHORITY_REQUIRED": "AUTHORIZED_SCOPE",
                      "EXTERNAL_REPAIR_UNRESOLVED": "AUTHORIZED_SCOPE",
                      "OPENAI_CREDENTIALS_MISSING": "EXTERNAL_REVIEW_PREREQUISITE",
                      "OPENAI_AUTHENTICATION_FAILED": "EXTERNAL_REVIEW_PREREQUISITE",
                      "OPENAI_QUOTA_OR_RATE_LIMIT": "EXTERNAL_REVIEW_PREREQUISITE",
                      "OPENAI_MODEL_ACCESS": "EXTERNAL_REVIEW_PREREQUISITE",
                      "OPENAI_TIMEOUT": "EXTERNAL_REVIEW_PREREQUISITE",
                      "OPENAI_RESPONSES_UNAVAILABLE": "EXTERNAL_REVIEW_PREREQUISITE",
                      "RESEARCH_CONDITION_EXPANSION_REQUIRED": "AUTHORIZED_SCOPE"}.get(
                          reason_code, "RETAINED_EVIDENCE")
        strategy = STRATEGIES.get((contract.day, diagnosis.evidence_type)) if diagnosis else None
        self.snapshot.authority_blocker = AuthorityBlocker(classification=classification, reason_code=reason_code, message=message, criterion_id=diagnosis.criterion_id if diagnosis else None, evidence_type=diagnosis.evidence_type if diagnosis else None,
            resolution_strategy=resolution, action_template_id=strategy.template.template_id if strategy and strategy.template else None)
        self._fail(contract, reason_code, message, classification, inventory)

    def _run_item(self, item: LocalLLMDayWorkItem, contract: LocalLLMDayContract, inventory: dict[str, object]) -> None:
        item.state = LocalLLMWorkItemState.RUNNING
        self.snapshot.activity = item.title
        self._save()
        if item.kind == "EVIDENCE_CHECK" and contract.day == 1:
            result = self._collect_day_one_evidence(contract)
        else:
            result = self.work_order_executor({"kind": item.kind, "task_id": item.item_id, "engine_task_id": item.engine_task_id, "dynamic_work_order": item.dynamic_work_order.model_dump(mode="json") if item.dynamic_work_order else None, "criterion_ids": item.criterion_ids, "contract": contract.model_dump(mode="json"), "inventory": inventory})
        final = result.get("final_result")
        if final in {"COMPLETE", "COMPLETE_NO_CHANGE"}:
            item.state, item.evidence = LocalLLMWorkItemState.COMPLETE, self._bounded(result)
            self._evaluate_contract(contract, inventory)
            self._update_progress(contract)
            self._save()
            return
        classification = self._classify_result(result)
        item.state = LocalLLMWorkItemState.FAILED
        item.evidence = self._bounded(result)
        if classification == DayIssueClassification.IMPLEMENTATION_DEFECT and item.dynamic_work_order is not None:
            # Ordinary engineering failures automatically run the full bounded
            # repair episode. Research findings never enter this path.
            self.snapshot.state = LocalLLMDayState.REPAIR_SUPERVISOR
            self.snapshot.issue_classification = classification
            self._save()
            if self._supervise_repair(item, contract):
                self.snapshot.state = LocalLLMDayState.REVALIDATING
                self.snapshot.activity = "Repair verification succeeded; re-evaluating the Day Contract."
                self._save()
                return
            self._external_repair_blocker(contract, inventory, item)
            return
        self._fail(contract, "DAY_TASK_FAILED", "A planned task did not produce trusted evidence.", classification, inventory)

    def _complete(self, contract: LocalLLMDayContract, inventory: dict[str, object]) -> None:
        self._transition(LocalLLMDayState.COMPLETE)
        self.snapshot.progress = 100
        self.snapshot.report = LocalLLMDayReport(day=contract.day, objective=contract.objective, result="DAY_COMPLETE", summary="Completion criteria are supported by persisted evidence.", evidence={"satisfied_criteria": contract.satisfied_criteria, "inventory": inventory})
        self.snapshot.activity = self.snapshot.report.summary
        self._audit("LOCAL_LLM_DAY_COMPLETE", {"day": contract.day, "criteria": contract.satisfied_criteria})
        self._save()

    def _fail(self, contract: LocalLLMDayContract, result: str, summary: str, classification: DayIssueClassification, inventory: dict[str, object]) -> None:
        terminal = LocalLLMDayState.FAILED_UNRECOVERABLE
        if classification in {DayIssueClassification.MISSING_EXTERNAL_AUTHORITY, DayIssueClassification.EXTERNAL_AUTHORITY_REQUIRED}:
            terminal = LocalLLMDayState.EXTERNAL_ACTION_REQUIRED
        elif classification == DayIssueClassification.HUMAN_PRODUCT_DECISION_REQUIRED:
            terminal = LocalLLMDayState.HUMAN_ACTION_REQUIRED
        if self.snapshot.state != terminal:
            self._transition(terminal)
        self.snapshot.issue_classification = classification
        self.snapshot.report = LocalLLMDayReport(day=contract.day, objective=contract.objective, result=result, summary=summary, evidence={"issue_classification": classification.value, "remaining_gaps": contract.remaining_gaps, "inventory": inventory})
        self.snapshot.activity = summary
        self._save()

    def _evaluate_contract(self, contract: LocalLLMDayContract, inventory: dict[str, object]) -> None:
        # A work item is never proof.  Criteria only retain references to
        # records that have passed the per-type validator.
        self._ingest_reusable_evidence(contract, inventory)
        for criterion in contract.completion_criteria:
            references: dict[str, str] = {}
            for evidence_type in criterion.required_evidence:
                candidates = [record for record in self.snapshot.evidence_store.values()
                              if record.day == contract.day and record.contract_version == contract.version
                              and (contract.day != 1 or record.observation_fingerprint == self._inventory_fingerprint(inventory))
                              and record.evidence_type == evidence_type and self._evidence_record_valid(evidence_type, record)]
                if candidates:
                    selected = sorted(candidates, key=lambda record: record.collected_at)[-1]
                    references[evidence_type] = selected.record_id
            criterion.evidence_record_ids = references
            criterion.satisfied = len(references) == len(criterion.required_evidence)
            criterion.evidence = dict(references)
        contract.satisfied_criteria = [item.criterion_id for item in contract.completion_criteria if item.satisfied]
        contract.remaining_gaps = [item.criterion_id for item in contract.completion_criteria if not item.satisfied]
        self._update_progress(contract)

    def _assert_registry_conformance(self) -> None:
        """Fail startup closed unless every configured evidence name and pair is owned."""
        try:
            document = yaml.safe_load(self.PROGRAM_PATH.read_text(encoding="utf-8"))
            definitions = document.get("days", []) if isinstance(document, dict) else []
            pairs = {
                (definition["day"], evidence)
                for definition in definitions if isinstance(definition, dict) and isinstance(definition.get("day"), int)
                for criterion in definition.get("completion_criteria", []) if isinstance(criterion, dict)
                for evidence in criterion.get("evidence", []) if isinstance(evidence, str)
            }
            assert_coverage(pairs, self.evidence_registry.names)
        except (OSError, UnicodeError, yaml.YAMLError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("DAY_REGISTRY_CONFORMANCE_FAILURE") from exc

    def _ingest_reusable_evidence(self, contract: LocalLLMDayContract, inventory: dict[str, object]) -> None:
        retained = inventory.get("retained_evidence")
        if isinstance(retained, dict):
            for record in self.snapshot.evidence_store.values():
                if record.provider_id == "retained-resolver" and record.day == contract.day:
                    record.compatibility_result = self.evidence_registry.validate(record.evidence_type, retained.get(record.evidence_type))
            self._ingest_legacy_evidence(contract, retained, provider_id="retained-resolver", source_fingerprint=self._inventory_fingerprint(inventory))

    def _ingest_action_result(self, contract: LocalLLMDayContract, diagnosis: GapDiagnosis, result: dict[str, object]) -> None:
        evidence = result.get("evidence")
        if isinstance(evidence, dict):
            strategy = STRATEGIES[(contract.day, diagnosis.evidence_type)]
            allowed = set(strategy.template.post_action_evidence_types) if strategy.template else set()
            if contract.day == 1:
                allowed = {"commit_ref"} if diagnosis.evidence_type == "commit_ref" else {"git_head", "origin_ref", "status_audit", "staging_audit", "documentation_check", "test_result"}
            evidence = {name: value for name, value in evidence.items() if name in allowed}
            # Day 1's ordinary observation may see HEAD, but only the
            # BASELINE_CHECKPOINT template is allowed to satisfy commit_ref.
            if contract.day == 1 and diagnosis.evidence_type != "commit_ref":
                evidence = {name: value for name, value in evidence.items() if name != "commit_ref"}
            self._ingest_legacy_evidence(contract, evidence, provider_id=diagnosis.strategy_id or "action", source_fingerprint=diagnosis.input_fingerprint)
        classification = self._classify_result(result)
        if classification in {DayIssueClassification.EXTERNAL_AUTHORITY_REQUIRED, DayIssueClassification.HUMAN_PRODUCT_DECISION_REQUIRED}:
            self._terminal_blocker(contract, classification, str(result.get("error_code", "AUTHORITY_REQUIRED")),
                                   str(result.get("reason", diagnosis.reason)), {}, diagnosis)
            return
        if result.get("final_result") not in {"COMPLETE", "COMPLETE_NO_CHANGE"} and classification in {DayIssueClassification.IMPLEMENTATION_DEFECT, DayIssueClassification.ENGINEERING_REPAIR}:
            # Repair is an internal transition, never a request for a second Go.
            diagnosis.classification = DayIssueClassification.ENGINEERING_REPAIR
            diagnosis.failure_reason = str(result.get("error_code") or "DETERMINISTIC_WORK_FAILURE")[:500]
            diagnosis.reason = f"Registered work failed deterministic verification: {diagnosis.failure_reason}."
            self._transition(LocalLLMDayState.REPAIR_SUPERVISOR)
            self.snapshot.issue_classification = DayIssueClassification.ENGINEERING_REPAIR
            item = LocalLLMDayWorkItem(item_id=f"repair-{diagnosis.strategy_id}", title="Registered engineering repair", objective=diagnosis.reason, kind="DYNAMIC_ENGINEERING_WORK", criterion_ids=[diagnosis.criterion_id], contract_day=contract.day, contract_version=contract.version)
            # A template executor may provide an explicitly bounded dynamic order.
            dynamic = result.get("dynamic_work_order")
            if isinstance(dynamic, dict):
                try:
                    from backend.models.local_llm_day import DynamicDayWorkOrder
                    item.dynamic_work_order = DynamicDayWorkOrder.model_validate(dynamic)
                except (TypeError, ValueError):
                    pass
            if item.dynamic_work_order is not None:
                item.evidence = self._bounded(result)
                item.state = LocalLLMWorkItemState.FAILED
                self.snapshot.work_items.append(item)
                if self._supervise_repair(item, contract):
                    repair_evidence = item.evidence.get("evidence")
                    if isinstance(repair_evidence, dict):
                        self._ingest_legacy_evidence(contract, repair_evidence, provider_id="repair-supervisor", source_fingerprint=diagnosis.input_fingerprint)
                    return
                self._external_repair_blocker(contract, {}, item)
                return
        if classification == DayIssueClassification.MODEL_QUALITY_FINDING:
            # Retention is evidence; quality is not an engineering repair.
            self.snapshot.issue_classification = classification

    def _ingest_legacy_evidence(self, contract: LocalLLMDayContract, evidence: dict[str, object], *, provider_id: str, source_fingerprint: str) -> None:
        for evidence_type, legacy in evidence.items():
            if evidence_type not in self.evidence_registry.names or not isinstance(legacy, dict):
                continue
            if not self.evidence_registry.validate(evidence_type, legacy):
                continue
            value = legacy.get("value")
            fingerprint = self._text_fingerprint(json.dumps(value, sort_keys=True, ensure_ascii=False))
            # The same read-only observation value can be valid under more than
            # one Day 1 source snapshot. Retain each observation independently
            # so a newly authorized snapshot scope can be revalidated without
            # mutating the historical record.
            record_id = self._text_fingerprint(
                f"{self.project_id}|{contract.day}|{contract.version}|{evidence_type}|{fingerprint}|{source_fingerprint}"
            )[:32]
            if record_id in self.snapshot.evidence_store:
                self.snapshot.evidence_store[record_id].compatibility_result = True
                continue
            self.snapshot.evidence_store[record_id] = EvidenceRecord(
                record_id=record_id, project_id=self.project_id, day=contract.day, contract_version=contract.version,
                evidence_type=evidence_type, provider_id=provider_id, provider_version="v1",
                validator_id=f"evidence-registry:{evidence_type}", validator_version="v1",
                source_paths=list(legacy.get("source_paths", [])), source_revision=str(legacy.get("source_revision") or "unknown"),
                source_fingerprint=fingerprint, configuration_fingerprint=source_fingerprint,
                value=value, status="VALID", validator_result=True, compatibility_result=True,
                source_hashes=legacy.get("source_hashes", {}), observation_fingerprint=source_fingerprint,
                retained_artifact_reference=legacy.get("retained_artifact_reference"),
            )

    def _evidence_record_valid(self, evidence_type: str, record: EvidenceRecord | None) -> bool:
        if record is None or record.evidence_type != evidence_type or not record.validator_result or not record.compatibility_result or record.status != "VALID":
            return False
        if record.project_id != self.project_id or any(self._file_fingerprint(Path(path)) != digest for path, digest in record.source_hashes.items()):
            return False
        return self.evidence_registry.validate(evidence_type, {
            "evidence_type": evidence_type, "value": record.value, "source": record.provider_id,
            "verified": True, "validation": {"passed": record.validator_result, "validator": record.validator_id},
        })

    def _validated_plan(self, contract: LocalLLMDayContract, inventory: dict[str, object]) -> list[LocalLLMDayWorkItem]:
        # Day 1 evidence is collected only by the server-owned Git/document/
        # deterministic-test collector.  Configured process-smoke tasks have
        # no capability to prove this contract.
        if contract.day == 1:
            return [item.model_copy(update={"contract_day": contract.day, "contract_version": contract.version})
                    for item in self._deterministic_plan(contract, inventory)]
        proposed = self.planner(contract, inventory)
        known = {criterion.criterion_id for criterion in contract.completion_criteria}
        if not isinstance(proposed, list) or not 1 <= len(proposed) <= self.MAX_TASKS:
            raise ValueError("planner did not return a bounded task list")
        seen: set[str] = set()
        validated: list[LocalLLMDayWorkItem] = []
        for item in proposed:
            if not isinstance(item, LocalLLMDayWorkItem) or item.item_id in seen or item.kind not in {"EVIDENCE_CHECK", "ENGINE_WORK_ORDER", "DYNAMIC_ENGINEERING_WORK"}:
                raise ValueError("planner proposed an untrusted task")
            if item.kind == "ENGINE_WORK_ORDER" and not item.engine_task_id:
                raise ValueError("engine work order has no trusted task identifier")
            if item.kind == "DYNAMIC_ENGINEERING_WORK" and item.dynamic_work_order is None:
                raise ValueError("dynamic engineering work has no bounded work order")
            if item.kind == "DYNAMIC_ENGINEERING_WORK" and item.engine_task_id is not None:
                raise ValueError("dynamic engineering work cannot select a configured task")
            if item.kind == "EVIDENCE_CHECK" and item.engine_task_id is not None:
                raise ValueError("evidence check cannot select an engine task")
            if item.kind == "EVIDENCE_CHECK" and item.dynamic_work_order is not None:
                raise ValueError("evidence check cannot create an engineering work order")
            if not item.criterion_ids or not set(item.criterion_ids).issubset(set(contract.remaining_gaps) & known):
                raise ValueError("planner expanded Day authority")
            seen.add(item.item_id)
            validated.append(item.model_copy(update={"contract_day": contract.day, "contract_version": contract.version}))
        return validated

    def _deterministic_plan(self, contract: LocalLLMDayContract, _inventory: dict[str, object]) -> list[LocalLLMDayWorkItem]:
        # This is a safe fallback when Codex is unavailable.  Production may
        # replace it with a Codex Architect adapter; Python validates either.
        return [
            LocalLLMDayWorkItem(item_id=f"evidence-{contract.day}-{criterion.criterion_id}", title="Collect trusted completion evidence", objective=criterion.statement, criterion_ids=[criterion.criterion_id])
            for criterion in contract.completion_criteria if criterion.criterion_id in contract.remaining_gaps
        ][: self.MAX_TASKS]

    def _read_only_executor(self, work_order: dict[str, object]) -> dict[str, object]:
        """Default executor proves completion only from existing trusted sources."""
        if work_order.get("kind") != "EVIDENCE_CHECK":
            return {"final_result": "FAILED", "error_code": "ENGINE_WORK_ORDER_REQUIRED"}
        # Source presence tells the planner where to inspect; it is never
        # enough to claim a research or implementation criterion is met.
        return {"final_result": "COMPLETE", "evidence": {}, "inspection": "sources inventoried; delivery evidence not inferred"}

    def _load_contracts(self) -> dict[int, LocalLLMDayContract]:
        path = self.PROGRAM_PATH
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return {}
        if not isinstance(document, dict):
            return {}
        sources = document.get("authoritative_sources")
        shared_constraints = document.get("shared_constraints")
        definitions = document.get("days")
        if not isinstance(sources, list) or not isinstance(shared_constraints, list) or not isinstance(definitions, list):
            return {}
        declared = {
            evidence for definition in definitions if isinstance(definition, dict)
            for criterion in (definition.get("completion_criteria") or []) if isinstance(criterion, dict)
            for evidence in (criterion.get("evidence") or [])
        }
        if not declared or not declared.issubset(self.evidence_registry.names):
            return {}
        contracts: dict[int, LocalLLMDayContract] = {}
        for definition in definitions:
            if not isinstance(definition, dict):
                return {}
            day = definition.get("day")
            raw_criteria = definition.get("completion_criteria")
            if not isinstance(day, int) or not isinstance(raw_criteria, list):
                return {}
            criteria = [DayCriterion(criterion_id=f"d{day}-{entry['id']}", statement=entry["statement"], required_evidence=entry["evidence"])
                        for entry in raw_criteria if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("statement"), str) and isinstance(entry.get("evidence"), list)]
            if len(criteria) != len(raw_criteria):
                return {}
            contracts[day] = LocalLLMDayContract(day=day, title=str(definition.get("title", "")), version=str(document.get("version", "v1")), objective=str(definition.get("objective", "")), completion_criteria=criteria, constraints=[str(value) for value in shared_constraints], authoritative_sources=[str(value).replace("\\", "/") for value in sources], remaining_gaps=[criterion.criterion_id for criterion in criteria])
        return contracts if set(contracts) == set(range(1, 15)) else {}

    def _inventory(self, contract: LocalLLMDayContract | None = None) -> dict[str, object]:
        contract = contract or self.snapshot.contract
        sources = contract.authoritative_sources if contract else []
        source_presence = {source: (self.root / source).is_file() for source in sources}
        retained_evidence = self.retained_evidence_resolver.resolve(contract.day) if contract else {}
        try:
            source_fingerprint = git_fingerprint(
                self.root,
                self.approved_day_one_snapshot_paths if contract and contract.day == 1 else frozenset(),
            )
        except (GitSafetyError, OSError):
            source_fingerprint = self._text_fingerprint(json.dumps({path: self._file_fingerprint(self.root / path) for path in sources}, sort_keys=True))
        if contract and contract.day == 1 and self.snapshot.baseline_checkpoint:
            retained_evidence.update(self._recollect_checkpoint())
        return {**self._git_state(), "source_presence": source_presence,
                "source_fingerprint": source_fingerprint, "configuration_fingerprint": self._file_fingerprint(self.PROGRAM_PATH),
                "missing_sources": [path for path, present in source_presence.items() if not present],
                "retained_evidence": retained_evidence}

    def _git_state(self) -> dict[str, object]:
        def output(args: list[str]) -> str:
            try:
                return subprocess.run(["git", "-C", str(self.root), "-c", f"safe.directory={self.root}", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, check=False).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                return ""
        status = output(["status", "--short"])
        tracked = output(["ls-files"]).splitlines()
        safe_context = [path.replace("\\", "/") for path in tracked if path.startswith(("src/", "scripts/", "tests/", "config/", "docs/")) and Path(path).suffix.lower() in {".py", ".yaml", ".yml", ".json", ".md"}]
        return {"branch": output(["branch", "--show-current"]), "head": output(["rev-parse", "HEAD"]), "origin": output(["remote", "get-url", "origin"]), "working_tree_clean": not bool(status), "status_count": len(status.splitlines()) if status else 0, "tracked_context_files": safe_context[:80]}

    @staticmethod
    def _record(name: str, value: object, source: str, passed: bool) -> dict[str, object]:
        return {
            "evidence_type": name,
            "value": value,
            "source": source,
            "verified": True,
            "validation": {"passed": passed, "validator": f"deterministic:{name}"},
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _non_empty(value: object) -> bool:
        return value is not None and value != "" and value != {} and value != []

    def _criterion_evidence_valid(self, criterion: DayCriterion, evidence: object) -> bool:
        if not isinstance(evidence, dict) or not criterion.required_evidence:
            return False
        return all(self._validate_evidence_record(name, evidence.get(name)) for name in criterion.required_evidence)

    def _validate_evidence_record(self, name: str, record: object) -> bool:
        """Registry-owned semantic validation; no verified-envelope fallback."""
        return self.evidence_registry.validate(name, record)

    def _git(self, *args: str) -> tuple[int, str, str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "-c", f"safe.directory={self.root}", *args],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, check=False,
            )
            return result.returncode, result.stdout.strip(), result.stderr.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, "", type(exc).__name__

    @staticmethod
    def _is_generated_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        return normalized.startswith(("results/", "artifacts/", "models/", "datasets/", "teacher/", "telemetry/", "logs/", "cache/")) or normalized.endswith((".zip", ".gguf", ".safetensors"))

    def _day_one_status_audit(self) -> dict[str, object]:
        _code, output, _error = self._git("status", "--porcelain=v1", "-uall")
        staged: list[str] = []
        unstaged: list[str] = []
        untracked: list[str] = []
        generated: list[str] = []
        for line in output.splitlines():
            if len(line) < 4:
                continue
            state, path = line[:2], line[3:].replace("\\", "/")
            if " -> " in path:
                path = path.split(" -> ")[-1]
            if state == "??":
                untracked.append(path)
            else:
                if state[0] not in {" ", "?"}:
                    staged.append(path)
                if state[1] not in {" ", "?"}:
                    unstaged.append(path)
            if self._is_generated_path(path):
                generated.append(path)
        relevant_prefixes = ("src/", "backend/", "scripts/", "tests/", "config/", "docs/", "schemas/")
        relevant = sorted({path for path in [*staged, *unstaged] if path.startswith(relevant_prefixes)})
        return {
            "staged_tracked_paths": sorted(set(staged)), "unstaged_tracked_paths": sorted(set(unstaged)),
            "untracked_paths": sorted(set(untracked)), "relevant_dirty_paths": relevant,
            "generated_paths": sorted(set(generated)),
        }

    def _day_one_documentation_check(self, contract: LocalLLMDayContract) -> dict[str, object]:
        expected = ["docs/runbooks/work-plan-day1-14.md", "docs/README.md", "docs/architecture/decision-reasoning-architecture.md", "docs/handoff/handoff-2026-09-18.md"]
        failures: list[str] = []
        readable: dict[str, str] = {}
        for relative in expected:
            try:
                readable[relative] = (self.root / relative).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                failures.append(f"unreadable:{relative}")
        readme = readable.get("docs/README.md", "")
        current_execution_lines = [line for line in readme.splitlines() if "Current execution sequence" in line]
        checks = [
            {"name": "authoritative_files_readable", "passed": not failures},
            {"name": "readme_current_execution_sequence", "passed": any("runbooks/work-plan-day1-14.md" in line for line in current_execution_lines)},
            {"name": "readme_current_architecture", "passed": "architecture/decision-reasoning-architecture.md" in readme},
            {"name": "readme_current_handoff", "passed": "handoff/handoff-2026-09-18.md" in readme},
            {"name": "week1_not_current_execution_source", "passed": all("week1" not in line.lower() for line in current_execution_lines)},
        ]
        failures.extend(check["name"] for check in checks if not check["passed"])
        return {"checked_files": expected, "checks": checks, "failures": failures, "contract_sources": contract.authoritative_sources}

    def _day_one_test_result(self, *, execute: bool = True, cache_key: str | None = None) -> dict[str, object]:
        candidates = ["tests/test_process_consistency_smoke.py", "tests/test_process_consistency_review_set.py"]
        selected = [path for path in candidates if (self.root / path).is_file()]
        if not selected:
            return {"commands": [], "exit_code": 1, "passed": 0, "failed": 0, "deterministic_only": True, "timestamp": datetime.now(timezone.utc).isoformat(), "reason": "no approved deterministic regression tests found"}
        command = [sys.executable, "-m", "pytest", "-q", *selected]
        if not execute:
            return {"commands": [command], "exit_code": None, "passed": 0, "failed": 0, "deterministic_only": True, "timestamp": datetime.now(timezone.utc).isoformat(), "reason": "read-only diagnostic; command not run"}
        cached = self.snapshot.evidence_cache.get(cache_key) if cache_key else None
        if isinstance(cached, dict) and cached.get("exit_code") == 0 and isinstance(cached.get("passed"), int) and cached["passed"] > 0:
            return {**cached, "cache_hit": True}
        try:
            result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=False)
            combined = f"{result.stdout}\n{result.stderr}"
            import re
            passed = next((int(value) for value in re.findall(r"(\d+) passed", combined)), 0)
            failed = next((int(value) for value in re.findall(r"(\d+) failed", combined)), 0)
            evidence = {"commands": [command], "exit_code": result.returncode, "passed": passed, "failed": failed, "deterministic_only": True, "timestamp": datetime.now(timezone.utc).isoformat(), "cache_hit": False}
            if cache_key and result.returncode == 0 and passed > 0 and failed == 0:
                self.snapshot.evidence_cache[cache_key] = evidence
            return evidence
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"commands": [command], "exit_code": 1, "passed": 0, "failed": 0, "deterministic_only": True, "timestamp": datetime.now(timezone.utc).isoformat(), "reason": type(exc).__name__}

    def _collect_day_one_evidence(self, contract: LocalLLMDayContract, *, execute_tests: bool = True) -> dict[str, object]:
        branch_code, branch, _ = self._git("branch", "--show-current")
        head_code, head, _ = self._git("rev-parse", "HEAD")
        commit_code, _commit, _ = self._git("cat-file", "-e", f"{head}^{{commit}}") if head else (1, "", "")
        is_commit = head_code == 0 and commit_code == 0
        status = self._day_one_status_audit()
        upstream_code, upstream, _ = self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        origin_code, origin, _ = self._git("remote", "get-url", "origin")
        upstream_sha_code, upstream_sha, _ = self._git("rev-parse", "@{upstream}") if upstream_code == 0 else (1, "", "")
        ahead, behind = None, None
        if upstream_sha_code == 0:
            count_code, counts, _ = self._git("rev-list", "--left-right", "--count", f"HEAD...@{{upstream}}")
            if count_code == 0 and len(counts.split()) == 2:
                ahead, behind = counts.split()
        git_head = {"branch": branch, "head": head, "is_commit": is_commit}
        origin_ref = {"origin_url": origin, "upstream_ref": upstream, "upstream_sha": upstream_sha, "ahead": ahead, "behind": behind}
        staging = {"staged_paths": status["staged_tracked_paths"], "staged_generated_paths": [path for path in status["staged_tracked_paths"] if self._is_generated_path(path)]}
        staging["generated_artifacts_not_staged"] = not staging["staged_generated_paths"]
        documentation = self._day_one_documentation_check(contract)
        test_paths = [self.root / path for path in ("tests/test_process_consistency_smoke.py", "tests/test_process_consistency_review_set.py")]
        cache_material = "|".join([head, git_fingerprint(self.root, self.approved_day_one_snapshot_paths), contract.version, self._file_fingerprint(self.PROGRAM_PATH), *[
            f"{path.relative_to(self.root).as_posix()}:{self._file_fingerprint(path)}" for path in test_paths
        ]])
        tests = self._day_one_test_result(execute=execute_tests, cache_key=self._text_fingerprint(cache_material))
        commit_ref = {"branch": branch, "head": head, "is_commit": is_commit, "relevant_dirty_paths": status["relevant_dirty_paths"], "reproducible": not status["relevant_dirty_paths"]}
        evidence = {
            "git_head": self._record("git_head", git_head, "git branch --show-current; git rev-parse HEAD; git cat-file -e HEAD^{commit}", branch_code == 0 and is_commit),
            "origin_ref": self._record("origin_ref", origin_ref, "git remote get-url origin; git rev-parse @{upstream}; git rev-list --left-right --count", origin_code == 0 and upstream_sha_code == 0 and bool(origin) and bool(upstream) and bool(upstream_sha)),
            "status_audit": self._record("status_audit", status, "git status --porcelain=v1 -uall", True),
            "staging_audit": self._record("staging_audit", staging, "git status --porcelain=v1 -uall (index column)", bool(staging["generated_artifacts_not_staged"])),
            "documentation_check": self._record("documentation_check", documentation, "deterministic authoritative-document relationship checks", not documentation["failures"]),
            "test_result": self._record("test_result", tests, " ".join(str(part) for part in tests["commands"][0]) if tests["commands"] else "no approved command", tests["exit_code"] == 0),
            "commit_ref": self._record("commit_ref", commit_ref, "git rev-parse HEAD; git cat-file -e HEAD^{commit}; git status --porcelain=v1 -uall", is_commit and commit_ref["reproducible"]),
        }
        return {"final_result": "COMPLETE", "evidence": evidence, "collection": "day1_deterministic"}

    def _create_day_one_baseline_checkpoint(self, contract: LocalLLMDayContract) -> dict[str, object]:
        value = checkpoint(self.root, self.approved_day_one_snapshot_paths)
        if value.get("authority_required"):
            return {"final_result": "HUMAN_ACTION_REQUIRED", "issue_classification": "HUMAN_PRODUCT_DECISION_REQUIRED",
                    "error_code": value["authority_required"], "reason": "Approved source scope cannot be identified safely."}
        self.snapshot.baseline_checkpoint = value
        self._save()
        # Checkpoint creation is work; collection independently resolves the ref.
        return {"final_result": "COMPLETE", "evidence": self._recollect_checkpoint()}

    def _recollect_checkpoint(self) -> dict[str, object]:
        value = self.snapshot.baseline_checkpoint
        if not value:
            return {}
        code, tree, _ = self._git("rev-parse", f"{value['checkpoint_ref']}^{{tree}}")
        if (code or tree != value["tree"]
                or git_fingerprint(self.root, self.approved_day_one_snapshot_paths) != value["source_fingerprint"]):
            return {}
        return {"commit_ref": self._record("commit_ref", value, str(value["checkpoint_ref"]), True)}

    def _record_repair_rejection(self, item: LocalLLMDayWorkItem, code: str, *, card: LocalLLMRepairCard | None = None, executor_result: dict[str, object] | None = None) -> dict[str, object]:
        with self._lock:
            if card:
                self.snapshot.repair_knowledge.append(card.model_copy(update={"status": "CODEX_REJECTED"}))
            self.snapshot.codex_handoff = {"status": "READY", "failed_work_item": item.item_id, "issue_classification": DayIssueClassification.IMPLEMENTATION_DEFECT.value, "local_llm_result": code, "executor_result": self._bounded(executor_result or {})}
            self.snapshot.activity = "LocalLLM proposal was not applied; a bounded Codex handoff is ready."
            self._save()
            return self.view()

    def _bounded_repair_files(self, item: LocalLLMDayWorkItem) -> dict[str, str]:
        """Provide only declared work-order context to the local repair model."""
        if item.dynamic_work_order is None:
            return {}
        protected = ("results/", "artifacts/", "models/", "datasets/", ".env")
        files: dict[str, str] = {}
        for relative in [*item.dynamic_work_order.context_files, *item.dynamic_work_order.allowed_files]:
            if relative in files or relative.startswith(protected):
                continue
            path = (self.root / relative).resolve()
            try:
                path.relative_to(self.root)
                if path.is_file():
                    files[relative] = path.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue
            if len(files) >= 3:
                break
        return files

    @staticmethod
    def _classify_result(result: dict[str, object]) -> DayIssueClassification:
        value = result.get("issue_classification")
        try:
            return DayIssueClassification(str(value))
        except ValueError:
            return DayIssueClassification.INSUFFICIENT_EVIDENCE

    @staticmethod
    def _criterion_ids(contract: LocalLLMDayContract) -> set[str]:
        return {criterion.criterion_id for criterion in contract.completion_criteria}

    def _restore_contract(self, current: LocalLLMDayContract, saved: LocalLLMDayContract) -> LocalLLMDayContract:
        """Restore only evidence that validates under the current contract."""
        prior = {criterion.criterion_id: criterion for criterion in saved.completion_criteria}
        criteria: list[DayCriterion] = []
        for criterion in current.completion_criteria:
            old = prior.get(criterion.criterion_id)
            references = old.evidence_record_ids if old else {}
            criteria.append(criterion.model_copy(update={"evidence": references, "evidence_record_ids": references, "satisfied": False}))
        restored = current.model_copy(update={"completion_criteria": criteria})
        restored.satisfied_criteria = [criterion.criterion_id for criterion in criteria if criterion.satisfied]
        restored.remaining_gaps = [criterion.criterion_id for criterion in criteria if not criterion.satisfied]
        return restored

    def _valid_work_items(self, items: list[LocalLLMDayWorkItem], contract: LocalLLMDayContract) -> list[LocalLLMDayWorkItem]:
        known = self._criterion_ids(contract)
        return [item for item in items if item.contract_day == contract.day and item.contract_version == contract.version
                and item.criterion_ids and set(item.criterion_ids).issubset(known)]

    @staticmethod
    def _contract_fingerprint(contract: LocalLLMDayContract) -> str:
        material = contract.model_dump(exclude={"completion_criteria", "satisfied_criteria", "remaining_gaps"})
        material["completion_criteria"] = sorted([
            {"criterion_id": c.criterion_id, "statement": c.statement,
             "required_evidence": sorted(c.required_evidence)} for c in contract.completion_criteria
        ], key=lambda c: c["criterion_id"])
        material["constraints"] = sorted(material["constraints"])
        material["authoritative_sources"] = sorted(material["authoritative_sources"])
        return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    def _invalidate_contract_identity(self, current: LocalLLMDayContract, saved_fingerprint: str | None, reason: str) -> None:
        """Retain mismatched state for audit, but make it unusable as proof."""
        saved = self.snapshot.contract
        for record in self.snapshot.evidence_store.values():
            record.compatibility_result = False
        if saved:
            for criterion in saved.completion_criteria:
                criterion.satisfied = False
            saved.satisfied_criteria = []
            saved.remaining_gaps = [criterion.criterion_id for criterion in saved.completion_criteria]
        self.snapshot.stop_reason = reason
        self.snapshot.activity = reason
        self.snapshot.report = LocalLLMDayReport(
            day=self.snapshot.selected_day,
            objective=current.objective,
            result=reason,
            summary="Persisted contract identity differs from its authoritative definition; no evidence or action may be reused.",
            evidence={"saved_fingerprint": saved_fingerprint, "current_fingerprint": self._contract_fingerprint(current)},
        )

    def _restore_snapshot(self) -> None:
        """A persisted boolean is never authority after process restart."""
        day = self.snapshot.selected_day
        current = self._load_contracts().get(day) if day is not None else None
        if current is None:
            return
        saved = self.snapshot.contract
        fingerprint = self.snapshot.contract_fingerprint or (self._contract_fingerprint(saved) if saved else None)
        if saved is None or fingerprint != self._contract_fingerprint(current) or fingerprint != self._contract_fingerprint(saved):
            reason = "CONTRACT_VERSION_CONTENT_MISMATCH" if saved and saved.version == current.version else "CONTRACT_VERSION_CHANGED"
            self._invalidate_contract_identity(current, fingerprint, reason)
            self.snapshot.state = LocalLLMDayState.FAILED_UNRECOVERABLE
            self._save()
            return
        self.snapshot.contract_fingerprint = fingerprint
        self.snapshot.contract = self._restore_contract(current, self.snapshot.contract)
        self.snapshot.work_items = self._valid_work_items(self.snapshot.work_items, current)
        self._evaluate_contract(self.snapshot.contract, self._inventory(current))
        if (self.snapshot.state == LocalLLMDayState.FAILED and self.snapshot.report
                and self.snapshot.report.result == "DAY_INSUFFICIENT_EVIDENCE"):
            # Compatibility only: the legacy controller persisted a generic
            # insufficient-evidence FAILED state without a legal exit. Keep
            # its report/history, but require the current controller's normal
            # Resume -> PREFLIGHT validation before it may proceed.
            self.snapshot.state = LocalLLMDayState.PAUSED
            self.snapshot.stop_reason = "LEGACY_INSUFFICIENT_EVIDENCE_REQUIRES_RESUME"
            self.snapshot.activity = "Legacy insufficient-evidence state restored; Resume revalidates the same Day."
            self._save()
            return
        if self.snapshot.state == LocalLLMDayState.COMPLETE and self.snapshot.contract.remaining_gaps:
            self.snapshot.state = LocalLLMDayState.PAUSED
            self.snapshot.activity = "Saved completion was invalidated because required evidence does not validate."

    @staticmethod
    def _bounded(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            return {"value": str(value)[:1000]}
        return {str(key): value[key] for key in list(value)[:20]}

    def _update_progress(self, contract: LocalLLMDayContract) -> None:
        total = len(contract.completion_criteria)
        self.snapshot.progress = round(len(contract.satisfied_criteria) * 100 / total) if total else 0

    def _recommended_action(self) -> dict[str, object]:
        if self.snapshot.state in self.ACTIVE_STATES:
            return {"action_id": "WAIT", "label": "Working", "enabled": False, "reason": "The selected Day is autonomously collecting, diagnosing, repairing, or revalidating evidence."}
        if self._repair_resumable():
            return {"action_id": "REPAIR_AND_GO", "label": "Repair and Go", "enabled": True, "reason": "Resume the interrupted bounded repair episode."}
        if self.snapshot.state in {LocalLLMDayState.PAUSED, LocalLLMDayState.STOPPED}:
            return {"action_id": "RESUME", "label": "Resume", "enabled": True, "reason": "Continue persisted incomplete work."}
        if self.snapshot.state == LocalLLMDayState.COMPLETE:
            return {"action_id": "SELECT_NEXT_DAY", "label": "Select next Day", "enabled": True, "reason": "The selected Day has sufficient evidence."}
        if self.snapshot.state in {LocalLLMDayState.HUMAN_ACTION_REQUIRED, LocalLLMDayState.EXTERNAL_ACTION_REQUIRED}:
            return {"action_id": "SHOW_REQUIRED_ACTION", "label": "Action required", "enabled": False, "reason": "A genuine authority or external prerequisite blocks continuation."}
        if self.snapshot.state == LocalLLMDayState.FAILED_UNRECOVERABLE:
            return {"action_id": "SHOW_FAILURE", "label": "Safety failure", "enabled": False, "reason": "The controller cannot safely continue this Day."}
        return {"action_id": "GO", "label": "Go", "enabled": self.snapshot.state == LocalLLMDayState.IDLE, "reason": "Load a Day Contract and plan only remaining evidence gaps."}

    def _save(self) -> None:
        self.snapshot.updated_at = datetime.now(timezone.utc)
        if self.persist:
            self.persist(self.snapshot.model_dump(mode="json"))

    def _audit(self, event: str, details: dict[str, object]) -> None:
        if self.audit:
            self.audit("LOCAL_LLM_DAY", event, details)
