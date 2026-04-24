from __future__ import annotations

from pathlib import Path

from langfuse import observe

from .. import shell
from ..config import Settings
from ..models import Task


@observe(name="stage.pr")
def run(task: Task, worktree_path: Path, branch_name: str, settings: Settings) -> dict:
    """Commit, push and open a PR against settings.target_repo.

    Idempotent-ish: if task already has a pr_url, callers should skip this stage.
    """
    shell.run(["git", "add", "-A"], cwd=worktree_path)

    status = shell.run(["git", "status", "--porcelain"], cwd=worktree_path)
    changes = [line for line in status.stdout.splitlines() if line.strip()]
    if not changes:
        raise RuntimeError("implement stage produced no changes — nothing to commit")
    _sanity_check_changes(changes)

    commit_message = f"foundry: task #{task.issue_number} — {task.issue_title}"
    shell.run(["git", "commit", "-m", commit_message], cwd=worktree_path)
    shell.run(["git", "push", "-u", "origin", branch_name], cwd=worktree_path)

    body = (
        f"Automated PR from The Foundry (skeleton mode).\n\n"
        f"Closes #{task.issue_number}\n\n"
        f"Issue: {task.issue_title}"
    )
    pr_result = shell.run(
        [
            "gh", "pr", "create",
            "--repo", settings.target_repo,
            "--head", branch_name,
            "--base", "main",
            "--title", commit_message,
            "--body", body,
        ],
        cwd=worktree_path,
    )
    pr_url = pr_result.stdout.strip().splitlines()[-1]

    shell.run(
        [
            "gh", "issue", "close", str(task.issue_number),
            "--repo", task.repo,
            "--comment", f"Closed automatically by The Foundry after opening {pr_url}.",
        ],
        cwd=worktree_path,
    )

    return {"pr_url": pr_url, "branch": branch_name}


MAX_FILES_PER_PR = 40
FORBIDDEN_PATH_SUBSTRINGS = ("__pycache__", ".pyc", ".DS_Store", ".venv/")


def _sanity_check_changes(porcelain_lines: list[str]) -> None:
    """Reject suspicious worktree state before committing.

    Guards against agents accidentally copying parent-repo artifacts into the
    sandbox: build caches, dotfiles, or very large file sets.
    """
    if len(porcelain_lines) > MAX_FILES_PER_PR:
        raise RuntimeError(
            f"refusing to commit: agent produced {len(porcelain_lines)} changed "
            f"files (limit {MAX_FILES_PER_PR}) — likely a sandbox escape"
        )
    bad: list[str] = []
    for line in porcelain_lines:
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if any(sub in path for sub in FORBIDDEN_PATH_SUBSTRINGS):
            bad.append(path)
    if bad:
        raise RuntimeError(
            f"refusing to commit: forbidden paths in agent changes: {bad[:5]}"
        )
