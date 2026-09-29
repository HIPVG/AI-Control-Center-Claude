"""Deterministic policy for the first bounded Goal-to-Plan capability."""

from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from backend.models.goal import GoalPlan, GoalPlanStatus


TRUSTED_LOCAL_LLM_EXPERIMENT = "local_llm_process_consistency_smoke"
FORBIDDEN_AUTHORITY_TERMS = (
    "download", "install", "cloud", "openai", "api key", "git", "delete", "remove", "path", "command", "budget",
    "ダウンロード", "インストール", "クラウド", "削除", "コマンド", "予算", "パス",
)


def propose_goal(goal: str, configured_experiment_ids: set[str]) -> GoalPlan:
    """Map a known safe intent to one preconfigured experiment, or reject it."""
    normalized = " ".join(goal.split())
    digest = sha256(normalized.encode("utf-8")).hexdigest()
    base = {"goal_id": str(uuid4()), "created_at": datetime.now(timezone.utc), "goal_digest": digest}
    lowered = normalized.casefold()
    if any(term in lowered for term in FORBIDDEN_AUTHORITY_TERMS):
        return GoalPlan(**base, status=GoalPlanStatus.REJECTED, summary="Goal rejected by trusted authority policy.", policy_reason="FORBIDDEN_AUTHORITY_REQUEST")
    is_local_llm_experiment = (
        all(term in lowered for term in ("local", "llm", "experiment"))
        or ("ローカル" in normalized and "llm" in lowered and "実験" in normalized)
    )
    if not is_local_llm_experiment:
        return GoalPlan(**base, status=GoalPlanStatus.REJECTED, summary="Goal is outside the initial trusted experiment capability.", policy_reason="GOAL_NOT_RECOGNIZED")
    if TRUSTED_LOCAL_LLM_EXPERIMENT not in configured_experiment_ids:
        return GoalPlan(**base, status=GoalPlanStatus.REJECTED, summary="Trusted experiment is not configured.", policy_reason="TRUSTED_EXPERIMENT_NOT_CONFIGURED")
    return GoalPlan(**base, status=GoalPlanStatus.PROPOSED, summary="Run the configured trusted LocalLLM process-consistency experiment.", target_type="TRUSTED_EXPERIMENT", target_id=TRUSTED_LOCAL_LLM_EXPERIMENT, policy_reason="TRUSTED_LOCAL_LLM_EXPERIMENT_MATCH")
