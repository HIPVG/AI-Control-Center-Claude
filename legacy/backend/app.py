from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.orchestrator.engine import ControlCenterEngine, JsonStateStore
from backend.models.day import DayExecutionMode
from backend.control.daily_operation import DailyOperationService
from backend.control.reviewer_bus import ReviewerBusWatcher
from backend.models.goal import GoalSubmission
from backend.models.local_llm_day import DayGoRequest

ROOT = Path(__file__).resolve().parent.parent
engine = ControlCenterEngine(JsonStateStore(ROOT / "state" / "control-center.json"), ROOT / "config" / "budget.yaml")
daily_operation = DailyOperationService(ROOT)
reviewer_bus = ReviewerBusWatcher(ROOT, codex_executable=engine.runtime.codex.executable)
started_at = datetime.now(timezone.utc)

app = FastAPI(title="AI Control Center", version="0.1.0")
app.mount("/static", StaticFiles(directory=ROOT / "frontend"), name="static")


@app.on_event("startup")
def start_reviewer_bus() -> None:
    reviewer_bus.start()


@app.on_event("shutdown")
def stop_reviewer_bus() -> None:
    reviewer_bus.stop()


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(ROOT / "frontend" / "index.html")


@app.get("/api/status")
def status() -> dict:
    return engine.status()


@app.get("/api/plan")
def plan() -> dict:
    state = engine.status()
    return {"week": state["week"], "validation_day": state["validation_day"], "calendar_day": state["calendar_day"], "days": state["days"]}


@app.get("/api/tasks")
def tasks() -> list[dict]:
    return engine.status()["tasks"]


@app.get("/api/timeline")
def timeline() -> list[dict]:
    return [event.model_dump(mode="json") for event in engine.timeline]


@app.get("/api/token-usage")
def token_usage() -> dict:
    return engine.budgets.usage_view()


@app.get("/api/runtime")
def runtime() -> dict:
    return engine.runtime_view()


@app.get("/api/runtime/readiness")
def runtime_readiness() -> dict | None:
    return engine.runtime_readiness()


@app.get("/api/operation/health")
def operation_health() -> dict:
    return {
        "server_state": "HEALTHY",
        "started_at": started_at.isoformat(),
        "day_state": engine.day_status()["state"],
        "autostart": daily_operation.autostart_status(),
        "reviewer_bus": reviewer_bus.status(),
    }


@app.get("/api/reviewer-bus/status")
def reviewer_bus_status() -> dict:
    return reviewer_bus.status()


@app.post("/api/operation/autostart/enable")
def enable_autostart() -> dict:
    result = daily_operation.enable_autostart()
    if result.get("action") == "ENABLED":
        engine.record_autostart_enabled()
    return result


@app.get("/api/day/plans")
def day_plans() -> list[dict]:
    return engine.configured_plans()


@app.get("/api/day/status")
def day_status() -> dict:
    return engine.day_status()


@app.get("/api/local-llm/days")
def local_llm_days() -> list[dict]:
    return engine.local_llm_days()


@app.get("/api/local-llm/day/status")
def local_llm_day_status() -> dict:
    return engine.local_llm_day_status()


@app.post("/api/local-llm/day/{day}/smoke")
def smoke_local_llm_day(day: int) -> dict:
    return engine.smoke_local_llm_day(day)


@app.post("/api/local-llm/day/{day}/start")
def start_local_llm_day(day: int) -> dict:
    return engine.start_local_llm_day(day)


@app.post("/api/local-llm/day/go")
def prepare_local_llm_day_go(request: DayGoRequest) -> dict:
    return engine.prepare_local_llm_day_go(request.selected_day)


@app.post("/api/local-llm/day/resume")
def resume_local_llm_day() -> dict:
    return engine.resume_local_llm_day()


@app.post("/api/local-llm/day/register-retained-evidence")
def register_local_llm_retained_evidence() -> dict:
    return engine.register_local_llm_retained_evidence()


@app.post("/api/local-llm/day/repair-and-go")
def repair_and_go_local_llm_day() -> dict:
    return engine.repair_and_go_local_llm_day()


@app.post("/api/local-llm/day/stop")
def stop_local_llm_day() -> dict:
    return engine.stop_local_llm_day()


@app.get("/api/experiments")
def experiments() -> list[dict]:
    return engine.configured_experiments()


@app.get("/api/goals")
def goals() -> list[dict]:
    return engine.goal_plans()


@app.get("/api/next-action")
def next_action() -> dict:
    return engine.next_action()


@app.post("/api/next-action/continue")
def continue_autonomously() -> dict:
    return engine.continue_autonomously()


@app.get("/api/zero-touch")
def zero_touch_runs() -> list[dict]:
    return engine.zero_touch_runs()


@app.post("/api/zero-touch/start")
def start_zero_touch(submission: GoalSubmission) -> dict:
    return engine.start_zero_touch(submission.goal)


@app.post("/api/zero-touch/continue")
def continue_zero_touch() -> dict:
    return engine.continue_zero_touch()


@app.get("/api/git/candidates")
def git_completion_candidates() -> list[dict]:
    return engine.git_completion_candidates()


@app.get("/api/git/completions")
def git_completions() -> list[dict]:
    return engine.git_completions()


@app.post("/api/git/complete/{run_id}")
def complete_verified_work(run_id: str) -> dict:
    return engine.complete_verified_work(run_id)


@app.post("/api/validation/git-completion")
def run_git_completion_validation() -> dict:
    return engine.run_git_completion_validation()


@app.post("/api/goals")
def propose_goal(submission: GoalSubmission) -> dict:
    return engine.propose_goal(submission.goal)


@app.post("/api/goals/{goal_id}/execute")
def execute_goal(goal_id: str) -> dict:
    return engine.execute_goal(goal_id)


@app.post("/api/experiments/{experiment_id}/run")
def run_experiment(experiment_id: str) -> dict:
    return engine.run_experiment(experiment_id)


@app.post("/api/validation/escalation/{scenario}")
def run_escalation_validation(scenario: str) -> dict:
    return engine.run_escalation_validation(scenario)


@app.post("/api/day/start/{plan_id}")
def start_day(plan_id: str, mode: DayExecutionMode = Query(DayExecutionMode.SINGLE_STEP)) -> dict:
    return engine.start_day(plan_id, mode=mode)


@app.post("/api/day/resume")
def resume_day(mode: DayExecutionMode | None = Query(None)) -> dict:
    return engine.resume_day(mode=mode)


@app.post("/api/day/stop")
def stop_day() -> dict:
    return engine.stop_day()


@app.post("/api/run/mock")
def run_mock() -> dict:
    return engine.run_mock()


@app.post("/api/run/codex-smoke")
def run_codex_smoke() -> dict:
    return engine.run_codex_smoke()


@app.post("/api/run/project-smoke/{project_id}")
def run_project_smoke(project_id: str) -> dict:
    return engine.run_project_smoke(project_id)


@app.get("/api/tasks/configured")
def configured_tasks() -> list[dict]:
    return engine.configured_tasks()


@app.post("/api/run/task/{task_id}")
def run_task(task_id: str) -> dict:
    return engine.run_task(task_id)


@app.post("/api/tasks/discover-failing/{discovery_id}")
def discover_failing_task(discovery_id: str) -> dict:
    return engine.discover_failing_task(discovery_id)


@app.post("/api/run/fault-repair/{fault_id}")
def run_fault_repair(fault_id: str) -> dict:
    return engine.run_fault_repair(fault_id)
