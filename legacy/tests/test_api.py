import pytest
from fastapi.testclient import TestClient

import backend.app as control_app
from backend.orchestrator.engine import ControlCenterEngine
from backend.models.runtime import RuntimeConfig


class StubDailyOperation:
    def autostart_status(self):
        return {"supported": True, "enabled": False, "task_name": "AI Control Center", "state": "NOT_ENABLED", "startup_diagnostics": [{"code": "LAUNCH_EXCEPTION", "reason": "PYTHON_INVOCATION_FAILED", "exception_type": "RuntimeException"}]}

    def enable_autostart(self):
        return {"supported": True, "enabled": True, "task_name": "AI Control Center", "state": "ENABLED", "action": "ENABLED", "startup_diagnostics": [{"code": "UVICORN_LAUNCHED", "value": 8000}]}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(control_app, "engine", ControlCenterEngine(runtime_config=RuntimeConfig()))
    monkeypatch.setattr(control_app, "daily_operation", StubDailyOperation())
    return TestClient(control_app.app)


def test_required_endpoints_are_available(client):
    for path in ("/api/status", "/api/plan", "/api/tasks", "/api/timeline", "/api/token-usage", "/api/runtime", "/api/runtime/readiness", "/api/operation/health", "/api/reviewer-bus/status", "/api/day/plans", "/api/day/status", "/api/local-llm/days", "/api/local-llm/day/status", "/api/experiments", "/api/goals", "/api/next-action", "/api/git/candidates", "/api/git/completions", "/api/zero-touch"):
        assert client.get(path).status_code == 200
    assert client.post("/api/run/mock").status_code == 200
    assert client.post("/api/run/codex-smoke").json()["error_code"] == "REAL_MODE_REQUIRED"
    assert client.post("/api/local-llm/day/register-retained-evidence").status_code == 200
    assert client.get("/").status_code == 200


def test_day_endpoints_accept_only_configured_plan_ids(client):
    assert client.post("/api/day/start/not-configured", json={"command": "unsafe"}).json()["error_code"] == "PLAN_NOT_CONFIGURED"
    assert client.post("/api/day/resume", json={"single_step": False}).json()["error_code"] == "NO_DAY_PLAN"
    assert client.post("/api/day/stop").status_code == 200


def test_day_mode_is_a_typed_query_not_a_browser_supplied_configuration_object(client):
    assert client.post("/api/day/start/not-configured?mode=continuous").json()["error_code"] == "PLAN_NOT_CONFIGURED"
    assert client.post("/api/day/start/not-configured?mode=unsafe").status_code == 422


def test_dashboard_is_the_single_local_llm_day_runner(client):
    days = client.get("/api/local-llm/days").json()
    assert [day["day"] for day in days] == list(range(1, 15))
    html = (control_app.ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (control_app.ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    for control in ("day-selector", "selection-status", "smoke", "go", "repair-and-go", "stop", "resume", "git-push", "state", "activity", "run-indicator", "run-indicator-detail", "day-work-items", "repair-knowledge", "codex-handoff", "result-title", "smoke-title", "smoke-summary", "recommended-action", "recommended-action-reason"):
        assert f'id="{control}"' in html
    for panel in ("reviewer-bus-state", "reviewer-bus-summary", "reviewer-bus-events"):
        assert f'id="{panel}"' in html
    assert "goal-input" not in html
    assert "scenario" not in html.lower()
    assert "/api/local-llm/day/" in script
    assert 'fetch("/api/local-llm/day/go"' in script
    assert "JSON.stringify({selected_day: selectedDay})" in script
    assert "/select`" not in script
    assert "/start`" not in script
    assert "/smoke" in script
    assert "repair-and-go" in script
    assert "snapshot.enabled_controls" in script
    assert 'recommendation.action_id !== "REPAIR_AND_GO"' not in script
    snapshot = client.get("/api/local-llm/day/status").json()
    assert set(snapshot["enabled_controls"]) == {"go", "smoke", "resume", "repair_and_go", "stop", "select_day"}
    assert all(type(value) is bool for value in snapshot["enabled_controls"].values())
    assert "SMOKE_PASS" in script
    assert '"SMOKE_PASS"' in script
    assert "codex-resolution" not in script
    assert "/api/git/candidates" in script
    assert "/api/git/complete/" in script
    assert "/api/goals" not in script
    assert "innerHTML" not in script
    assert "/api/reviewer-bus/status" in script
    assert "ReviewerStatus.rows(status)" in script
    assert 'id="reviewer-bus-pending"' in html
    assert 'id="reviewer-bus-completed"' in html
    assert "/static/reviewer-status.js" in html


def test_go_api_accepts_selection_only_and_never_starts_a_day(client, monkeypatch):
    observed = {}

    def prepare(day):
        observed["day"] = day
        return {
            "run_id": "run-api-fixture",
            "selected_day": day,
            "run_intent": {"run_id": "run-api-fixture", "selected_day": day},
            "admission_run_id": "run-api-fixture",
            "admission": {"status": "ADMISSIBLE", "next_state": "PREFLIGHT", "reason_code": None},
            "execution_started": False,
            "snapshot": {"selected_day": None, "state": "IDLE"},
        }

    monkeypatch.setattr(control_app.engine, "prepare_local_llm_day_go", prepare)

    response = client.post("/api/local-llm/day/go", json={"selected_day": 6})

    assert response.status_code == 200
    assert observed == {"day": 6}
    assert response.json()["run_id"] == response.json()["admission_run_id"]
    assert response.json()["admission"]["next_state"] == "PREFLIGHT"
    assert response.json()["execution_started"] is False
    assert client.post("/api/local-llm/day/go", json={
        "selected_day": 6, "effective_permission": True,
    }).status_code == 422
    assert client.post("/api/local-llm/day/6/select").status_code == 404


def test_goal_endpoint_accepts_only_the_bounded_goal_field(client):
    rejected = client.post("/api/goals", json={"goal": "Run the trusted LocalLLM experiment", "command": "unsafe"})
    assert rejected.status_code == 422
    result = client.post("/api/goals", json={"goal": "Run the trusted LocalLLM process consistency experiment"})
    assert result.json()["status"] == "PROPOSED"
    assert "command" not in result.json()


def test_zero_touch_start_accepts_only_the_bounded_goal_field(client):
    response = client.post("/api/zero-touch/start", json={"goal": "Run the trusted LocalLLM experiment", "command": "unsafe"})
    assert response.status_code == 422


def test_continue_endpoint_uses_only_current_trusted_policy(client, monkeypatch):
    action = client.get("/api/next-action").json()
    assert action["action_type"] == "RUN_WEEK1_DAY"
    monkeypatch.setattr(control_app.engine, "run_week1_day", lambda day: {"day": day, "status": "COMPLETE", "reason_code": "TEST_ONLY"})
    result = client.post("/api/next-action/continue", json={"command": "unsafe", "target_id": "unsafe"}).json()
    assert result["result"]["day"] == 4


def test_git_completion_endpoint_rejects_unknown_run_id_without_browser_supplied_git_data(client):
    response = client.post("/api/git/complete/not-a-verified-run", json={"branch": "main", "command": "unsafe", "path": "unsafe"})
    assert response.json()["error_code"] == "GIT_COMPLETION_NOT_READY"


def test_model_router_single_step_day_is_auditable_without_external_execution(monkeypatch):
    engine = ControlCenterEngine(runtime_config=RuntimeConfig())
    engine.day_runner.execute_task = lambda task_id, **kwargs: {
        "run_id": "mock-day-run", "task_id": task_id, "final_result": "COMPLETE_NO_CHANGE",
        "codex_attempts": [], "codex_invoked": False, "precheck_result": "PASS",
    }
    monkeypatch.setattr(control_app, "engine", engine)
    response = TestClient(control_app.app).post("/api/day/start/week1-day3-local-llm-v2?mode=single-step")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "PAUSED"
    assert body["queue"][0]["task_id"] == "PC-001-A"
    assert body["queue"][0]["final_result"] == "COMPLETE_NO_CHANGE"
    assert body["architect_calls"] == 1
    assert body["codex_calls"] == 0
    assert body["evaluator_calls"] == 0
    assert [decision["role"] for decision in body["model_routing_decisions"]] == ["architect"]
    assert body["current_task"]["task_id"] == "PC-001-C"
    assert body["current_routing"]["provider"] == "codex"
    assert "human_review_queue" in TestClient(control_app.app).get("/api/day/status").json()
    assert any(event.event_type.value == "DAY_MODEL_ROUTING" for event in engine.timeline)
    assert any(event.event_type.value == "DAY_DETERMINISTIC_NO_AI" for event in engine.timeline)


def test_status_includes_timeline_for_dashboard_rendering(client):
    response = client.get("/api/status")
    assert "timeline" in response.json()
    assert isinstance(response.json()["timeline"], list)


def test_health_and_autostart_endpoints_expose_only_fixed_operation_state(client):
    health = client.get("/api/operation/health").json()
    assert health["server_state"] == "HEALTHY"
    assert health["autostart"]["task_name"] == "AI Control Center"
    enabled = client.post("/api/operation/autostart/enable").json()
    assert enabled["action"] == "ENABLED"
    assert any(event.event_type.value == "DAILY_OPERATION_AUTOSTART" for event in control_app.engine.timeline)


def test_codex_smoke_endpoint_does_not_accept_browser_supplied_commands(client):
    response = client.post("/api/run/codex-smoke", json={"command": "unsafe", "prompt": "unsafe"})
    assert response.status_code == 200
    assert response.json()["error_code"] == "REAL_MODE_REQUIRED"


def test_project_smoke_endpoint_accepts_only_configured_path_parameter(client):
    unknown = client.post("/api/run/project-smoke/not-configured", json={"path": "C:/unsafe", "command": "unsafe", "prompt": "unsafe"})
    assert unknown.status_code == 200
    assert unknown.json()["error_code"] == "PROJECT_NOT_CONFIGURED"
    configured = client.post("/api/run/project-smoke/local_llm_lab", json={"path": "C:/unsafe"})
    assert configured.status_code == 200
    assert configured.json()["error_code"] == "REAL_MODE_REQUIRED"


def test_configured_tasks_endpoint_hides_commands_and_task_endpoint_rejects_request_injection(client):
    configured = client.get("/api/tasks/configured")
    assert configured.status_code == 200
    assert configured.json()[0]["task_id"] == "PC-001-A"
    assert "argv" not in configured.json()[0]
    unknown = client.post("/api/run/task/not-configured", json={"command": "unsafe", "path": "C:/unsafe"})
    assert unknown.status_code == 200
    assert unknown.json()["error_code"] == "TASK_NOT_CONFIGURED"
    selected = client.post("/api/run/task/PC-001-A", json={"command": "unsafe", "path": "C:/unsafe"})
    assert selected.status_code == 200
    assert selected.json()["error_code"] == "REAL_MODE_REQUIRED"


def test_discovery_endpoint_accepts_only_a_configured_discovery_id(client):
    response = client.post("/api/tasks/discover-failing/not-configured", json={"command": "unsafe", "cases": ["unsafe"]})
    assert response.status_code == 200
    assert response.json()["error_code"] == "DISCOVERY_NOT_CONFIGURED"


def test_fault_repair_endpoint_accepts_only_a_configured_fault_id(client):
    response = client.post("/api/run/fault-repair/not-configured", json={"target_file": "unsafe", "mutation": "unsafe"})
    assert response.status_code == 200
    assert response.json()["error_code"] == "FAULT_NOT_CONFIGURED"
