from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LocalLLMDayState(str, Enum):
    IDLE = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    LOADING_CONTRACT = "LOADING_CONTRACT"
    INVENTORY = "INVENTORY"
    VALIDATING = "VALIDATING"
    DIAGNOSING_GAP = "DIAGNOSING_GAP"
    COLLECTING_EVIDENCE = "COLLECTING_EVIDENCE"
    EXECUTING_DAY_WORK = "EXECUTING_DAY_WORK"
    CORRECTIVE_WORK = "CORRECTIVE_WORK"
    REPAIR_SUPERVISOR = "REPAIR_SUPERVISOR"
    REVALIDATING = "REVALIDATING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"
    HUMAN_ACTION_REQUIRED = "HUMAN_ACTION_REQUIRED"
    EXTERNAL_ACTION_REQUIRED = "EXTERNAL_ACTION_REQUIRED"
    FAILED_UNRECOVERABLE = "FAILED_UNRECOVERABLE"
    FAILED = "FAILED"  # Compatibility for snapshots created before the canonical state model.
    STOPPED = "STOPPED"


class LocalLLMWorkItemState(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    DEFERRED = "DEFERRED"
    BLOCKED = "BLOCKED"


class RunLimits(BaseModel):
    """Requested limits are inputs, not evidence of execution authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    active_work_seconds: int = Field(gt=0)
    max_attempts: int = Field(gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_cost: float = Field(ge=0, allow_inf_nan=False)
    currency: str = Field(min_length=1)


class RunIntent(BaseModel):
    """Immutable Go identity; persistence alone never starts a Day."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1, max_length=120)
    selected_day: int = Field(ge=1, le=14)
    go_at: datetime
    contract_fingerprint: str = Field(min_length=8, max_length=128)
    policy_fingerprint: str = Field(min_length=8, max_length=128)
    config_fingerprint: str = Field(min_length=8, max_length=128)
    git_fingerprint: str = Field(min_length=8, max_length=128)
    requested_limits: RunLimits

    @field_validator("go_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("Go time must include timezone")
        return value


class DayGoRequest(BaseModel):
    """Browser intent contains selection only; server owns all trusted preflight facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    selected_day: int = Field(ge=1, le=14)


class RunControl(BaseModel):
    """Server-owned snapshot, not a liveness or completion certificate."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1, max_length=120)
    selected_day: int = Field(ge=1, le=14)
    contract_fingerprint: str = Field(min_length=8, max_length=128)
    current_state: LocalLLMDayState
    state_history: tuple[LocalLLMDayState, ...] = ()
    next_action: str = Field(min_length=1)
    blocker: str | None = None
    resume_target: Literal["PREFLIGHT"] | None = None
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        return RunIntent.require_timezone(value)


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    intent: RunIntent
    control: RunControl

    @model_validator(mode="after")
    def same_identity(self) -> "RunRecord":
        for name in ("run_id", "selected_day", "contract_fingerprint"):
            if getattr(self.intent, name) != getattr(self.control, name):
                raise ValueError(f"Run identity mismatch: {name}")
        return self


class DayIssueClassification(str, Enum):
    ENGINEERING_REPAIR = "ENGINEERING_REPAIR"
    COLLECT_EVIDENCE = "COLLECT_EVIDENCE"
    CORRECT_REPOSITORY_STATE = "CORRECT_REPOSITORY_STATE"
    IMPLEMENTATION_DEFECT = "IMPLEMENTATION_DEFECT"
    TEST_OR_CONTRACT_DEFECT = "TEST_OR_CONTRACT_DEFECT"
    MODEL_QUALITY_FINDING = "MODEL_QUALITY_FINDING"
    EXPERIMENT_CONFIGURATION_ISSUE = "EXPERIMENT_CONFIGURATION_ISSUE"
    MISSING_EXTERNAL_AUTHORITY = "MISSING_EXTERNAL_AUTHORITY"
    EXTERNAL_AUTHORITY_REQUIRED = "EXTERNAL_AUTHORITY_REQUIRED"
    HUMAN_PRODUCT_DECISION_REQUIRED = "HUMAN_PRODUCT_DECISION_REQUIRED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    PRODUCE_DAY_EVIDENCE = "PRODUCE_DAY_EVIDENCE"
    EXPERIMENT_CONFIGURATION_REPAIR = "EXPERIMENT_CONFIGURATION_REPAIR"


class DayCriterion(BaseModel):
    criterion_id: str = Field(min_length=1, max_length=100)
    statement: str = Field(min_length=1, max_length=1200)
    required_evidence: list[str] = Field(default_factory=list, max_length=12)
    satisfied: bool = False
    evidence: dict[str, object] = Field(default_factory=dict)
    # A criterion is a reference to validator-owned evidence, never a copy of
    # a work-item result.  ``evidence`` is retained only to read old snapshots.
    evidence_record_ids: dict[str, str] = Field(default_factory=dict)


class LocalLLMDayContract(BaseModel):
    """The durable WHAT contract; it intentionally contains no task recipe."""

    day: int = Field(ge=1, le=14)
    title: str = Field(min_length=1, max_length=300)
    version: str = Field(default="v1", min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=1600)
    completion_criteria: list[DayCriterion] = Field(min_length=1, max_length=20)
    constraints: list[str] = Field(min_length=1, max_length=20)
    authoritative_sources: list[str] = Field(min_length=1, max_length=20)
    satisfied_criteria: list[str] = Field(default_factory=list, max_length=20)
    remaining_gaps: list[str] = Field(default_factory=list, max_length=20)


class LocalLLMDayWorkItem(BaseModel):
    """A bounded planner output, persisted independently from the Day contract."""

    item_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=300)
    objective: str = Field(min_length=1, max_length=1200)
    kind: str = Field(default="EVIDENCE_CHECK", max_length=60)
    engine_task_id: str | None = Field(default=None, max_length=120)
    dynamic_work_order: "DynamicDayWorkOrder | None" = None
    criterion_ids: list[str] = Field(default_factory=list, max_length=12)
    contract_day: int | None = Field(default=None, ge=1, le=14)
    contract_version: str | None = Field(default=None, max_length=80)
    state: LocalLLMWorkItemState = LocalLLMWorkItemState.PENDING
    evidence: dict[str, object] = Field(default_factory=dict)
    blocked_reason: str | None = None


class DynamicDayWorkOrder(BaseModel):
    """Server-validated authority for one new Day engineering task.

    It deliberately describes a bounded edit and deterministic test files, not
    a shell command.  The engine derives commands and always runs in a managed
    worktree.
    """

    task_id: str = Field(min_length=6, max_length=120, pattern=r"^day-[0-9]{1,2}-[a-z0-9-]+$")
    project_id: str = Field(default="local_llm_lab", pattern=r"^local_llm_lab$")
    task_type: str = Field(default="code_fix", pattern=r"^(code_fix|config|plan)$")
    allowed_files: list[str] = Field(min_length=1, max_length=8)
    context_files: list[str] = Field(min_length=1, max_length=10)
    acceptance_test_files: list[str] = Field(min_length=1, max_length=5)

    @staticmethod
    def _safe_paths(values: list[str], *, tests_only: bool = False) -> list[str]:
        normalized = [value.replace("\\\\", "/").strip() for value in values]
        if any(not value or value.startswith("/") or ":" in value or ".." in value.split("/") for value in normalized):
            raise ValueError("dynamic work-order paths must be safe relative paths")
        if tests_only and any(not value.startswith("tests/") or not value.endswith(".py") for value in normalized):
            raise ValueError("dynamic acceptance tests must be repository test files")
        if len(normalized) != len(set(normalized)):
            raise ValueError("dynamic work-order paths must be unique")
        return normalized

    @field_validator("allowed_files", "context_files")
    @classmethod
    def safe_paths(cls, values: list[str]) -> list[str]:
        return cls._safe_paths(values)

    @field_validator("acceptance_test_files")
    @classmethod
    def safe_test_paths(cls, values: list[str]) -> list[str]:
        return cls._safe_paths(values, tests_only=True)


class LocalLLMDayReport(BaseModel):
    day: int = Field(ge=1, le=14)
    objective: str
    result: str
    summary: str
    evidence: dict[str, object] = Field(default_factory=dict)
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LocalLLMRepairCard(BaseModel):
    """A proposal audit record. It never grants source-edit authority."""

    problem_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    cause: str = Field(min_length=1, max_length=1000)
    investigation: str = Field(min_length=1, max_length=1000)
    resolution_logic: str = Field(min_length=1, max_length=1200)
    verification: str = Field(min_length=1, max_length=1000)
    status: str = Field(default="PROPOSAL", max_length=40)
    uses: int = Field(default=0, ge=0)
    failure_class: str | None = Field(default=None, max_length=160)
    failed_work_item: str | None = Field(default=None, max_length=80)


class GapDiagnosis(BaseModel):
    """Persisted server-owned explanation of one unmet Day criterion."""

    criterion_id: str = Field(min_length=1, max_length=100)
    classification: DayIssueClassification
    reason: str = Field(min_length=1, max_length=1200)
    failure_reason: str = "NOT_YET_PRODUCED"
    required_evidence: list[str] = Field(default_factory=list, max_length=12)
    input_fingerprint: str = Field(min_length=8, max_length=128)
    action_fingerprint: str | None = Field(default=None, max_length=128)
    evidence_type: str | None = Field(default=None, min_length=1, max_length=100)
    strategy_id: str | None = Field(default=None, min_length=1, max_length=160)
    authority_basis: str | None = Field(default=None, max_length=400)
    expected_information_gain: str | None = Field(default=None, max_length=600)
    expected_state_change: str | None = Field(default=None, max_length=600)
    attempted_action_fingerprints: list[str] = Field(default_factory=list, max_length=20)


class EvidenceRecord(BaseModel):
    """Immutable, validator-owned evidence retained independently of work."""

    record_id: str = Field(min_length=8, max_length=80)
    # Legacy Day snapshots predate RunIntent.  New completion decisions require
    # both bindings; optional defaults preserve read compatibility only.
    run_id: str | None = Field(default=None, min_length=1, max_length=120)
    criterion_id: str | None = Field(default=None, min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=80)
    day: int = Field(ge=1, le=14)
    contract_version: str = Field(min_length=1, max_length=80)
    evidence_type: str = Field(min_length=1, max_length=100)
    provider_id: str = Field(min_length=1, max_length=160)
    provider_version: str = Field(min_length=1, max_length=80)
    validator_id: str = Field(min_length=1, max_length=160)
    validator_version: str = Field(min_length=1, max_length=80)
    source_paths: list[str] = Field(default_factory=list, max_length=30)
    source_revision: str | None = Field(default=None, max_length=200)
    source_fingerprint: str = Field(min_length=8, max_length=128)
    configuration_fingerprint: str = Field(min_length=8, max_length=128)
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    value: object = None
    status: str = Field(min_length=1, max_length=80)
    validator_result: bool
    validator_failure_reason: str | None = Field(default=None, max_length=1000)
    compatibility_result: bool
    retained_artifact_reference: str | None = Field(default=None, max_length=500)
    source_hashes: dict[str, str] = Field(default_factory=dict)
    observation_fingerprint: str = ""


class EvidenceResultInput(BaseModel):
    """Strict provider output consumed by the server-owned Result Adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str = Field(min_length=1, max_length=120)
    criterion_id: str = Field(min_length=1, max_length=100)
    evidence_type: str = Field(min_length=1, max_length=100)
    provider_id: str = Field(min_length=1, max_length=160)
    provider_version: str = Field(min_length=1, max_length=80)
    source_fingerprint: str = Field(min_length=8, max_length=128)
    configuration_fingerprint: str = Field(min_length=8, max_length=128)
    value: object
    source_paths: list[str] = Field(default_factory=list, max_length=30)
    source_revision: str | None = Field(default=None, max_length=200)
    source_hashes: dict[str, str] = Field(default_factory=dict)
    retained_artifact_reference: str | None = Field(default=None, max_length=500)


class ActionAttempt(BaseModel):
    action_fingerprint: str = Field(min_length=8, max_length=128)
    strategy_id: str = Field(min_length=1, max_length=160)
    criterion_id: str = Field(min_length=1, max_length=100)
    evidence_type: str = Field(min_length=1, max_length=100)
    input_fingerprint: str = Field(min_length=8, max_length=128)
    outcome: str = Field(min_length=1, max_length=80)
    action_template_id: str = ""
    action_input: dict[str, object] = Field(default_factory=dict)
    observed_classification: DayIssueClassification | None = None
    failure_reason: str | None = None
    attempted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AuthorityBlocker(BaseModel):
    classification: DayIssueClassification
    reason_code: str = Field(min_length=1, max_length=160)
    message: str = Field(min_length=1, max_length=1200)
    criterion_id: str | None = Field(default=None, max_length=100)
    evidence_type: str | None = Field(default=None, max_length=100)
    resolution_strategy: Literal["AUTHORITATIVE_SOURCES", "RETAINED_EVIDENCE", "RUNTIME_REAL", "RESEARCH_CONDITION", "SOURCE_DEPENDENCIES", "AUTHORIZED_SCOPE", "EXTERNAL_REVIEW_PREREQUISITE"] = "RETAINED_EVIDENCE"
    action_template_id: str | None = None


class RepairProposalAttempt(BaseModel):
    proposal_fingerprint: str = Field(min_length=8, max_length=128)
    outcome: str = Field(min_length=1, max_length=80)
    feedback: str | None = Field(default=None, max_length=1000)
    attempted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RepairEpisode(BaseModel):
    """Durable history for one bounded engineering repair episode."""

    episode_id: str = Field(min_length=8, max_length=80)
    project_id: str = Field(min_length=1, max_length=80)
    day: int = Field(ge=1, le=14)
    work_item_id: str = Field(min_length=1, max_length=80)
    failure_class: DayIssueClassification
    failure_fingerprint: str = Field(min_length=8, max_length=128)
    failure_excerpt: str = Field(default="", max_length=2000)
    component: str = Field(default="", max_length=160)
    started_at_epoch: float = Field(ge=0)
    repair_deadline_epoch: float = Field(ge=0)
    catalog_match_ids: list[str] = Field(default_factory=list, max_length=10)
    proposal_attempts: list[RepairProposalAttempt] = Field(default_factory=list, max_length=3)
    rejection_feedback: list[str] = Field(default_factory=list, max_length=3)
    implementer_review_outcome: str | None = Field(default=None, max_length=80)
    expert_solver_outcome: str | None = Field(default=None, max_length=80)
    verification_result: str | None = Field(default=None, max_length=80)
    final_outcome: str | None = Field(default=None, max_length=80)
    catalog_update_id: str | None = Field(default=None, max_length=80)
    external_review_fingerprint: str | None = Field(default=None, max_length=128)
    external_review_artifact: str | None = Field(default=None, max_length=600)
    external_review_outcome: str | None = Field(default=None, max_length=80)
    external_review_failure_code: str | None = Field(default=None, max_length=100)
    external_review_builder_outcome: str | None = Field(default=None, max_length=80)
    external_review_phase: str | None = Field(default=None, max_length=80)
    external_review_resume_attempts: int = Field(default=0, ge=0, le=1)
    contract_version: str = ""
    scope_fingerprint: str = ""
    interrupted: bool = False


class LocalLLMDaySnapshot(BaseModel):
    selected_day: int | None = Field(default=None, ge=1, le=14)
    state: LocalLLMDayState = LocalLLMDayState.IDLE
    objective: str | None = None
    contract: LocalLLMDayContract | None = None
    contract_fingerprint: str | None = None
    activity: str = "Waiting for a Day selection."
    progress: int = Field(default=0, ge=0, le=100)
    completed_steps: list[str] = Field(default_factory=list)
    work_items: list[LocalLLMDayWorkItem] = Field(default_factory=list, max_length=30)
    smoke_report: LocalLLMDayReport | None = None
    repair_deadline_seconds: int = Field(default=300, ge=1, le=300)
    repair_attempted: bool = False
    repair_knowledge: list[LocalLLMRepairCard] = Field(default_factory=list, max_length=30)
    repair_episode_ids: list[str] = Field(default_factory=list, max_length=30)
    implementer_handoff: dict[str, object] | None = None
    issue_classification: DayIssueClassification | None = None
    gap_diagnoses: list[GapDiagnosis] = Field(default_factory=list, max_length=20)
    evidence_store: dict[str, EvidenceRecord] = Field(default_factory=dict, max_length=300)
    action_attempts: list[ActionAttempt] = Field(default_factory=list, max_length=300)
    authority_blocker: AuthorityBlocker | None = None
    active_action: dict[str, object] | None = None
    last_action: dict[str, object] | None = None
    state_history: list[str] = Field(default_factory=list)
    baseline_checkpoint: dict[str, object] | None = None
    authority_resolution_fingerprint: str = ""
    research_execution_plans: dict[str, dict[str, object]] = Field(default_factory=dict)
    phase: str | None = None
    replan_count: int = Field(default=0, ge=0, le=20)
    replan_fingerprints: list[str] = Field(default_factory=list, max_length=20)
    evidence_cache: dict[str, dict[str, object]] = Field(default_factory=dict, max_length=20)
    report: LocalLLMDayReport | None = None
    stop_reason: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
