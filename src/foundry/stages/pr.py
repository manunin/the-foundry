from __future__ import annotations

from pathlib import Path

from langfuse import observe

from foundry import shell
from foundry.config import Settings
from foundry.forges import ChangeRequestInput, ForgeProvider, provider_for
from foundry.models import Task


@observe(name="stage.pr")
def run(
    task: Task,
    worktree_path: Path,
    branch_name: str,
    settings: Settings,
    report: str | None = None,
    provider: ForgeProvider | None = None,
) -> dict:
    """Commit, push and open a PR against settings.target_repo.

    Idempotent-ish: if task already has a pr_url, callers should skip this stage.
    `report` is an optional human-readable summary (from verify, or the
    implement agent response until a real verifier exists) embedded in the PR
    body.
    """
    commit_message = f"foundry: task #{task.issue_number} — {task.issue_title}"
    commit_result = commit_and_push_changes(
        task, worktree_path, branch_name, commit_message
    )

    body_parts = [
        "Automated change request from The Foundry.",
        "",
        f"Issue: {task.issue_title}",
        task.issue_url or _fallback_issue_url(task),
    ]
    if report:
        body_parts += ["", "## Отчёт", "", report.strip()]
    body = "\n".join(body_parts)
    active_provider = provider or provider_for(settings)
    change = active_provider.create_change(
        settings.target_repo,
        ChangeRequestInput(
            title=commit_message,
            body=body,
            branch=branch_name,
            base_branch=settings.base_branch,
        ),
    )
    pr_url = change.url

    active_provider.close_issue(
        task.repo,
        task.issue_number,
        f"Closed automatically by The Foundry after opening {pr_url}.",
    )

    return {
        "pr_url": pr_url,
        "branch": branch_name,
        "touched_files": commit_result["touched_files"],
        "files_changed": commit_result["files_changed"],
    }


def _fallback_issue_url(task: Task) -> str:
    separator = "/-/issues/" if task.forge.value == "gitlab" else "/issues/"
    return f"https://{task.forge_host}/{task.repo}{separator}{task.issue_number}"


MAX_FILES_PER_PR = 40
FORBIDDEN_PATH_SUBSTRINGS = ("__pycache__", ".pyc", ".DS_Store", ".venv/")


def commit_and_push_changes(
    task: Task,
    worktree_path: Path,
    branch_name: str,
    commit_message: str,
) -> dict:
    """Commit current worktree changes and push them to the PR branch."""
    shell.run(["git", "add", "-A"], cwd=worktree_path)

    status = shell.run(["git", "status", "--porcelain"], cwd=worktree_path)
    changes = [line for line in status.stdout.splitlines() if line.strip()]
    if not changes:
        raise RuntimeError("implement stage produced no changes — nothing to commit")
    _sanity_check_changes(changes)
    touched_files = [_porcelain_path(line) for line in changes]

    shell.run(["git", "commit", "-m", commit_message], cwd=worktree_path)
    shell.run(["git", "push", "-u", "origin", branch_name], cwd=worktree_path)
    return {
        "branch": branch_name,
        "files_changed": len(changes),
        "touched_files": touched_files,
    }


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
        path = _porcelain_path(line)
        if any(sub in path for sub in FORBIDDEN_PATH_SUBSTRINGS):
            bad.append(path)
    if bad:
        raise RuntimeError(
            f"refusing to commit: forbidden paths in agent changes: {bad[:5]}"
        )


def _porcelain_path(line: str) -> str:
    path = line[3:].strip() if len(line) > 3 else line.strip()
    if " -> " in path:
        return path.split(" -> ", 1)[1].strip()
    return path
