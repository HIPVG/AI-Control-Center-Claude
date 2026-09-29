import subprocess
from pathlib import Path

from backend.control.git_completion import GitCompletionService
from backend.models.git_completion import GitCompletionCandidate, GitCompletionStatus
from backend.models.runtime import RuntimeConfig
from backend.orchestrator.engine import ControlCenterEngine


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def update_bare_ref(remote: Path, branch: str, sha: str) -> None:
    completed = subprocess.run(["git", "--git-dir", str(remote), "update-ref", f"refs/heads/{branch}", sha], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    assert completed.returncode == 0, completed.stderr


def link_bare_objects(remote: Path, worktree: Path) -> None:
    (remote / "objects" / "info" / "alternates").write_text((worktree / ".git" / "objects").as_posix(), encoding="utf-8")


def verified_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    remote, worktree = tmp_path / "remote.git", tmp_path / "worktree"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(worktree)], check=True, capture_output=True)
    git(worktree, "config", "user.name", "Test")
    git(worktree, "config", "user.email", "test@example.invalid")
    (worktree / "baseline.txt").write_text("baseline\n", encoding="utf-8")
    git(worktree, "add", "baseline.txt"); git(worktree, "commit", "-m", "initial")
    link_bare_objects(remote, worktree)
    git(worktree, "remote", "add", "origin", str(remote))
    update_bare_ref(remote, "main", git(worktree, "rev-parse", "main"))
    branch = "agent/verified-work"
    git(worktree, "checkout", "-b", branch)
    (worktree / "verified.txt").write_text("verified\n", encoding="utf-8")
    return remote, worktree, branch


def test_verified_work_commits_pushes_agent_branch_and_prepares_but_never_merges_main(tmp_path):
    remote, worktree, branch = verified_worktree(tmp_path)
    main_before = git(worktree, "rev-parse", "main")
    candidate = GitCompletionCandidate(run_id="run-1", task_id="TASK-1", project_id="project", worktree_path=str(worktree), task_branch=branch, allowed_files=["verified.txt"], changed_files=["verified.txt"])
    service = GitCompletionService()
    original_run = service._run

    def deterministic_local_push(root, arguments):
        if arguments[:3] == ["push", "-u", "origin"]:
            update_bare_ref(remote, arguments[3], git(root, "rev-parse", "HEAD"))
            return 0, "", ""
        return original_run(root, arguments)

    service._run = deterministic_local_push
    result = service.complete(candidate, base_branch="main")
    assert result.status == GitCompletionStatus.PR_READY
    assert result.remote_branch == branch
    assert result.pull_request.compare_ref == f"main...{branch}"
    assert result.base_branch_unchanged is True
    assert git(worktree, "rev-parse", "main") == main_before
    assert subprocess.run(["git", "--git-dir", str(remote), "rev-parse", f"refs/heads/{branch}"], capture_output=True, text=True, check=False).returncode == 0


def test_completion_rejects_unverified_scope_and_main_branch(tmp_path):
    _, worktree, branch = verified_worktree(tmp_path)
    bad_scope = GitCompletionCandidate(run_id="run-2", task_id="TASK-2", project_id="project", worktree_path=str(worktree), task_branch=branch, allowed_files=["other.txt"], changed_files=["verified.txt"])
    assert GitCompletionService().complete(bad_scope, base_branch="main").error_code == "GIT_VERIFIED_SCOPE_MISMATCH"
    main_candidate = bad_scope.model_copy(update={"task_branch": "main", "allowed_files": ["verified.txt"]})
    assert GitCompletionService().complete(main_candidate, base_branch="main").error_code == "GIT_CANDIDATE_INVALID"


def test_engine_validation_is_isolated_and_records_structured_pr_preparation(tmp_path):
    engine = ControlCenterEngine(runtime_config=RuntimeConfig())
    engine.project_root = tmp_path
    result = engine.run_git_completion_validation()
    assert result["status"] == "PR_READY"
    assert result["validation_only"] is True
    assert result["pull_request"]["base_branch"] == "main"
    assert result["base_branch_unchanged"] is True
    assert any(event.event_type.value == "GIT_PR_PREPARED" for event in engine.timeline)
