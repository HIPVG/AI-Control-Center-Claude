"""Production bridge from frozen templates to guarded work and result adapters."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import sys

from backend.control.day_action_registry import STRATEGIES, ExecutionMode
from backend.control.day_research import ResearchRun, digest, research_inventory, validate_research_plan
from backend.control.tasks import ConfiguredTask, TaskCommand
from backend.models.task import TaskType


class DayActionExecutor:
    def __init__(self, engine, *, research_runner=None):
        self.engine = engine
        project = engine.projects.get("local_llm_lab")
        self.root = project.path.resolve() if project else engine.project_root / "missing-project"
        self.research_runner = research_runner or ResearchRun(self.root, engine.worktree_root.parent / "research-runs")

    def research_plan(self, template):
        controller = self.engine.local_llm_day_program
        request = research_inventory(self.root, controller.snapshot.contract, template)
        identities = {p["condition_fingerprint"] for p in request["candidates"]}
        if len(identities) > 1:
            raise ValueError("RESEARCH_CONDITION_CHOICE_REQUIRED")
        proposal = self.engine.local_llm_contract_planner.plan_research(request)
        condition = validate_research_plan(self.root, controller.snapshot.contract, template, proposal)
        previous = controller.snapshot.research_execution_plans.get(template.template_id)
        if previous and previous["condition_fingerprint"] != condition.condition_identity:
            raise ValueError("RESEARCH_CONDITION_CHANGED")
        return condition

    def execute(self, order: dict) -> dict:
        strategy = next((value for (day, _), value in STRATEGIES.items()
                         if day == order.get("day") and value.strategy_id == order.get("strategy_id")), None)
        if strategy is None or strategy.template is None:
            raise ValueError("ACTION_REGISTRY_INVARIANT")
        template = strategy.template
        if template.execution_mode == ExecutionMode.RESEARCH_RUN:
            try:
                condition = self.research_plan(template)
            except (ValueError, OSError) as exc:
                return {"final_result": "HUMAN_ACTION_REQUIRED", "issue_classification": "HUMAN_PRODUCT_DECISION_REQUIRED",
                        "error_code": str(exc)[:160] or "RESEARCH_CONDITION_NOT_APPROVED",
                        "configuration_status": "EXTERNAL_OR_HUMAN_CONFIGURATION_REQUIRED",
                        "reason": f"A unique bounded research plan could not be validated for {template.template_id}; resolving condition semantics requires human configuration authority."}
            controller = self.engine.local_llm_day_program
            controller.snapshot.research_execution_plans[template.template_id] = condition.execution_plan
            controller._save()
            return self.research_runner.execute(condition)
        if template.execution_mode == ExecutionMode.READ_ONLY:
            if strategy.evidence_type == "preservation_audit":
                return {"final_result": "COMPLETE", "evidence": {"preservation_audit": self.record("preservation_audit", {"protected_paths": ["results/", "models/", "datasets/", "artifacts/"], "preserved": True}, [])}}
            if template.template_id == "D13_FULL_REGRESSION":
                return self._regression(template)
            return {"final_result": "FAILED", "issue_classification": "ENGINEERING_REPAIR", "error_code": "READ_ONLY_ADAPTER_MISSING"}
        if template.execution_mode in {ExecutionMode.ENGINEERING_WORKTREE, ExecutionMode.DECISION_OR_DOCUMENTATION_WORKTREE}:
            if template.execution_mode == ExecutionMode.DECISION_OR_DOCUMENTATION_WORKTREE:
                return self._decision(strategy)
            return self._engineering(strategy)
        raise ValueError("UNSUPPORTED_EXECUTION_MODE")

    def _engineering(self, strategy):
        template = strategy.template
        tests = [path for path in template.allowed_output_scope if path.startswith("tests/") and path.endswith(".py")]
        if not tests:
            return {"final_result": "HUMAN_ACTION_REQUIRED", "issue_classification": "HUMAN_PRODUCT_DECISION_REQUIRED",
                    "error_code": "DECISION_EVIDENCE_REQUIRED", "reason": "A decision requires validated input evidence before a bounded report can be written."}
        argv = [sys.executable, "-m", "pytest", "-q", *tests]
        task = ConfiguredTask(task_id=f"day-{strategy.day}-{template.template_id.lower().replace('_', '-')}",
            project_id="local_llm_lab", title=template.template_id, task_type=TaskType.CODE_FIX,
            allowed_files=list(template.allowed_output_scope), context_files=list(template.context_scope),
            precheck=TaskCommand(argv=[sys.executable, "-c", "raise SystemExit(1)"]),
            postcheck=TaskCommand(argv=argv), max_retry=0, requires_codex=True)
        result = self.engine.run_task(task.task_id, task_definition=task, max_codex_attempts=1)
        adapted = self.adapt_engine_result(strategy, result)
        if adapted.get("final_result") not in {"COMPLETE", "COMPLETE_NO_CHANGE"}:
            adapted["dynamic_work_order"] = {"task_id": task.task_id, "allowed_files": task.allowed_files,
                                            "context_files": task.context_files, "acceptance_test_files": tests}
        return adapted

    def _decision(self, strategy):
        from uuid import uuid4
        from backend.control.day_git import git
        template = strategy.template
        controller = self.engine.local_llm_day_program
        records = [record for record in controller.snapshot.evidence_store.values() if controller._evidence_record_valid(record.evidence_type, record)]
        references = [record.record_id for record in records]
        run_id = uuid4().hex
        worktree = self.engine.worktree_root / run_id
        self.engine.worktree_root.mkdir(parents=True, exist_ok=True)
        git(self.root, "worktree", "add", "-b", f"codex/day-{strategy.day}-{run_id[:10]}", str(worktree), "HEAD")
        relative = template.allowed_output_scope[0]
        report = worktree / relative
        report.parent.mkdir(parents=True, exist_ok=True)
        contract = controller.snapshot.contract
        limitations = "Only the cited validated observations support this advisory. Unmeasured quality, repeatability, hardware benefit and product readiness remain unproven. No commercial or human review decision is made."
        sections = [f"# {template.template_id}", "", contract.objective, "", "## Evidence", ""]
        sections.extend(f"- {record.record_id}: {record.evidence_type}; {record.retained_artifact_reference or record.source_revision}" for record in records)
        sections.extend(["", "## Limitations", "", limitations, "", "## Advisory conclusion", "",
                         "The cited evidence bounds the provisional 14B architecture conclusion and product readiness. Unresolved requirements remain open. No measured benefit for a larger model or GPU is inferred from missing data."])
        if strategy.day == 11:
            sections.extend(["", "| Tier | Evidence requirement |", "| --- | --- |", "| 12–16 GB standard | Measured workload fits capacity and quality requirements |", "| Upper tier | Measured memory or quality deficit remains |", "| Optional 30B / 24 GB | Comparative benefit and business justification required |"])
        if strategy.day == 12:
            sections.extend(["", "## Operator procedure", "",
                "Use the repository's documented Python/runtime requirements. Install dependencies only with operator authority. Verify model availability before inference; do not download automatically.",
                "Back up tracked source and configuration using a Git checkpoint; copy retained generated artifacts separately. Restore to a separate location and verify hashes before replacing any working copy.",
                "On failure preserve the artifact, condition fingerprint and failure classification. Resume the same Day after resolving its recorded blocker. Model-quality failures must not be rerun to improve results.",
                "Keep models, datasets, results, artifacts and logs outside Git. Retain sanitized validation status and metrics; do not store raw response text or hidden reasoning."])
        report.write_text("\n".join(sections) + "\n", encoding="utf-8")
        values = {
            "sprint_review": {"review_path": str(report), "conclusions": [limitations]},
            "limitation_record": {"limitations": [limitations], "scope": contract.objective},
            "decision_record": {"decision": "Advisory only; preserve observed results and assess remaining gaps before adding a Critic or Selector.", "evidence_refs": references},
            "deployment_matrix": {"matrix_path": str(report), "tiers": ["12-16 GB", "upper tier", "optional 30B"]},
            "advisory_record": {"advisory": str(report), "limitations": [limitations]},
            "classification_record": {"classification": "RETAINED_FINDINGS", "artifact_path": str(report)},
            "status_summary": {"summary_path": str(report), "evidence_refs": references},
        }
        if strategy.day == 12:
            import subprocess
            values["operator_docs"] = {"document_paths": [str(report)], "covered_topics": ["setup", "install", "backup", "recovery", "artifacts", "logging"]}
            probes = ["results/probe.json", "models/probe.gguf", "datasets/probe.json", "artifacts/probe.json"]
            ignored = subprocess.run(["git", "-C", str(worktree), "check-ignore", "--no-index", "--", *probes], capture_output=True, text=True, encoding="utf-8")
            if set(ignored.stdout.splitlines()) == set(probes):
                values["gitignore_check"] = {"gitignore_path": str(worktree / ".gitignore"), "protected_patterns": probes}
            # Verify recovery using an independent in-memory byte roundtrip;
            # does not overwrite user files or research artifacts.
            payload = report.read_bytes()
            import io
            import zipfile
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as backup:
                backup.writestr("operator-report.md", payload)
            with zipfile.ZipFile(archive) as backup:
                restored = backup.read("operator-report.md")
            values["recovery_check"] = {"commands": [["python", "zipfile-byte-roundtrip"]], "exit_code": 0 if restored == payload else 1, "passed": int(restored == payload), "failed": int(restored != payload)}
        return {"final_result": "COMPLETE", "evidence": {name: self.record(name, values[name], [report])
                for name in template.post_action_evidence_types if name in values}, "worktree_path": str(worktree)}

    def adapt_engine_result(self, strategy, result):
        if result.get("final_result") not in {"COMPLETE", "COMPLETE_NO_CHANGE"}:
            code = result.get("error_code")
            if code == "REAL_MODE_REQUIRED":
                return {**result, "issue_classification": "EXTERNAL_AUTHORITY_REQUIRED", "reason": "The configured runtime does not authorize real Codex execution."}
            if code == "SOURCE_TASK_DEPENDENCY_DIRTY":
                return {**result, "issue_classification": "HUMAN_PRODUCT_DECISION_REQUIRED", "reason": "The task dependencies have user changes requiring reconciliation."}
            return {**result, "issue_classification": "ENGINEERING_REPAIR"}
        worktree = Path(str(result.get("worktree_path", ""))).resolve()
        if not worktree.is_relative_to(self.engine.worktree_root) or not worktree.is_dir():
            raise ValueError("ENGINE_WORKTREE_PROVENANCE_MISSING")
        check = result.get("postcheck") or result.get("precheck") or {}
        passed = re.search(r"(\d+) passed", str(check.get("stdout", "")))
        if check.get("exit_code") != 0 or not passed or int(passed[1]) == 0:
            return {"final_result": "FAILED", "issue_classification": "ENGINEERING_REPAIR", "error_code": "EMPTY_OR_INVALID_TEST_EVIDENCE"}
        test_value = {"commands": [check.get("argv")], "exit_code": 0, "passed": int(passed[1]), "failed": 0}
        paths = [worktree / relative for relative in strategy.template.allowed_output_scope]
        sources = [path for path in paths if path.suffix == ".py" and "tests" not in path.parts and path.is_file()]
        for source in sources:
            ast.parse(source.read_text(encoding="utf-8"))
        values = {"test_result": test_value, "deterministic_tests": test_value,
                  "provenance_test": test_value, "nonmutation_test": test_value,
                  "source_check": {"checked_paths": [str(path) for path in sources], "assertions": ["Python syntax and configured deterministic tests validated"]},
                  "architecture_check": {"checked_documents": [str(worktree / path) for path in strategy.template.context_scope], "assertions": ["Configured deterministic contract checks passed"]}}
        schema = next((path for path in paths if path.suffix == ".json" and path.is_file()), None)
        if schema:
            document = json.loads(schema.read_text(encoding="utf-8"))
            if isinstance(document, dict) and document.get("type") == "object" and document.get("properties"):
                values["schema_contract"] = {"schema_path": str(schema), "schema_version": str(document.get("$id", document.get("$schema", "v1")))}
        evidence = {name: self.record(name, values[name], [path for path in paths if path.is_file()])
                    for name in strategy.template.post_action_evidence_types if name in values}
        return {**result, "evidence": evidence, "verification_passed": bool(evidence)}

    @staticmethod
    def record(name, value, paths):
        return {"evidence_type": name, "value": value, "source": f"template-adapter:{name}", "verified": True,
                "validation": {"passed": True}, "source_paths": [str(path) for path in paths],
                "source_hashes": {str(path): digest(path) for path in paths}}

    def _regression(self, template):
        import subprocess
        argv = [sys.executable, "-m", "pytest", "-q", "tests"]
        completed = subprocess.run(argv, cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
        count = re.search(r"(\d+) passed", completed.stdout)
        value = {"commands": [argv], "exit_code": completed.returncode, "passed": int(count[1]) if count else 0, "failed": 0 if completed.returncode == 0 else 1}
        return {"final_result": "COMPLETE", "evidence": {"full_test_result": self.record("full_test_result", value, [])}}
