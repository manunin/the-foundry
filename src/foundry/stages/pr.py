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


MAX_FILES_PER_PR = 80
FORBIDDEN_PATH_SUBSTRINGS = ("__pycache__", ".pyc", ".DS_Store", ".venv/")


def commit_and_push_changes(
    task: Task,
    worktree_path: Path,
    branch_name: str,
    commit_message: str,
    *,
    allow_no_changes: bool = False,
) -> dict:
    """Commit current worktree changes and push them to the PR branch."""
    shell.run(["git", "add", "-A"], cwd=worktree_path)

    status = shell.run(["git", "status", "--porcelain"], cwd=worktree_path)
    changes = [line for line in status.stdout.splitlines() if line.strip()]
    pushed = True
    if changes:
        _sanity_check_changes(changes)
        touched_files = [_porcelain_path(line) for line in changes]
        shell.run(["git", "commit", "-m", commit_message], cwd=worktree_path)
    else:
        touched_files = _touched_files_from_local_commits(
            worktree_path,
            branch_name,
        )
        if not touched_files:
            touched_files = _touched_files_from_existing_commit(
                worktree_path,
                commit_message,
                allow_no_changes=allow_no_changes,
            )
            pushed = bool(touched_files)
    if pushed:
        _push_branch(
            worktree_path,
            branch_name,
            allow_replace_remote=not allow_no_changes,
        )
    return {
        "branch": branch_name,
        "files_changed": len(touched_files),
        "touched_files": touched_files,
        "pushed": pushed,
    }


def _push_branch(
    worktree_path: Path,
    branch_name: str,
    *,
    allow_replace_remote: bool,
) -> None:
    push_cmd = ["git", "push", "-u", "origin", f"HEAD:{branch_name}"]
    result = shell.run(push_cmd, cwd=worktree_path, check=False)
    if result.ok:
        return
    if not _is_non_fast_forward_rejection(result.stderr):
        raise shell.ShellError(push_cmd, result.returncode, result.stdout, result.stderr)

    if allow_replace_remote:
        _replace_remote_branch_with_lease(worktree_path, branch_name)
        return

    remote_ref = f"origin/{branch_name}"
    shell.run(["git", "fetch", "origin", branch_name], cwd=worktree_path)
    shell.run(["git", "rebase", remote_ref], cwd=worktree_path)
    shell.run(push_cmd, cwd=worktree_path)


def _replace_remote_branch_with_lease(worktree_path: Path, branch_name: str) -> None:
    shell.run(["git", "fetch", "origin", branch_name], cwd=worktree_path)
    remote_ref = f"origin/{branch_name}"
    expected_oid = shell.run(
        ["git", "rev-parse", remote_ref],
        cwd=worktree_path,
    ).stdout.strip()
    if not expected_oid:
        raise RuntimeError(f"cannot replace remote branch {branch_name}: empty remote oid")
    shell.run(
        [
            "git",
            "push",
            "-u",
            f"--force-with-lease=refs/heads/{branch_name}:{expected_oid}",
            "origin",
            f"HEAD:{branch_name}",
        ],
        cwd=worktree_path,
        allow_unsafe=True,
    )


def _is_non_fast_forward_rejection(stderr: str) -> bool:
    return "non-fast-forward" in stderr or "fetch first" in stderr


def _touched_files_from_local_commits(
    worktree_path: Path,
    branch_name: str,
) -> list[str]:
    remote_ref = f"origin/{branch_name}"
    rev_list = shell.run(
        ["git", "rev-list", "--count", f"{remote_ref}..HEAD"],
        cwd=worktree_path,
        check=False,
    )
    if not rev_list.ok:
        return []
    try:
        ahead_count = int(rev_list.stdout.strip() or "0")
    except ValueError:
        return []
    if ahead_count <= 0:
        return []

    diff = shell.run(
        ["git", "diff", "--name-only", remote_ref, "HEAD"],
        cwd=worktree_path,
    )
    touched_files = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    _sanity_check_changes([f" M {path}" for path in touched_files])
    return touched_files


def _touched_files_from_existing_commit(
    worktree_path: Path,
    commit_message: str,
    *,
    allow_no_changes: bool = False,
) -> list[str]:
    subject = shell.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=worktree_path,
    ).stdout.strip()
    if subject != commit_message:
        if allow_no_changes:
            return []
        raise RuntimeError("implement stage produced no changes — nothing to commit")

    diff_tree = shell.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
        cwd=worktree_path,
    )
    touched_files = [line.strip() for line in diff_tree.stdout.splitlines() if line.strip()]
    if not touched_files:
        if allow_no_changes:
            return []
        raise RuntimeError("implement stage produced no changes — nothing to commit")
    _sanity_check_changes([f" M {path}" for path in touched_files])
    return touched_files


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
