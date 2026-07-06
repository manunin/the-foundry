from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from foundry import shell
from foundry.forges import ForgeProvider
from foundry.forges.github import GitHubProvider

BASE_DIR_NAME = "_base"
GIT_WORKTREE_TIMEOUT_SEC = 600


def base_repo_path(worktree_root: Path) -> Path:
    return worktree_root / BASE_DIR_NAME


def ensure_base_repo(
    worktree_root: Path,
    source_repo: str,
    base_branch: str = "main",
    provider: ForgeProvider | None = None,
) -> Path:
    """Clone source_repo into worktree_root/_base and sync base_branch."""
    worktree_root.mkdir(parents=True, exist_ok=True)
    base = base_repo_path(worktree_root)
    if not base.exists():
        (provider or GitHubProvider()).clone(source_repo, base)
        shell.run(
            ["git", "fetch", "origin"],
            cwd=base,
            timeout=GIT_WORKTREE_TIMEOUT_SEC,
        )
        shell.run(
            ["git", "checkout", base_branch],
            cwd=base,
            timeout=GIT_WORKTREE_TIMEOUT_SEC,
        )
    else:
        remote = shell.run(
            ["git", "remote", "get-url", "origin"], cwd=base
        ).stdout.strip()
        remote_repo = _repo_from_remote(remote)
        if remote_repo != source_repo:
            raise RuntimeError(
                "cached base repository does not match configured target: "
                f"origin={remote_repo or 'unknown'}, target={source_repo}. "
                "Use a clean WORKTREE_ROOT or update _base/origin explicitly."
            )
        shell.run(
            ["git", "fetch", "origin"],
            cwd=base,
            timeout=GIT_WORKTREE_TIMEOUT_SEC,
        )
        shell.run(
            ["git", "checkout", base_branch],
            cwd=base,
            timeout=GIT_WORKTREE_TIMEOUT_SEC,
        )
        shell.run(
            ["git", "reset", "--hard", f"origin/{base_branch}"],
            cwd=base,
            timeout=GIT_WORKTREE_TIMEOUT_SEC,
            allow_unsafe=True,
        )
    return base


def _repo_from_remote(remote: str) -> str | None:
    value = remote.strip().rstrip("/")
    if not value:
        return None
    if "://" in value:
        path = urlsplit(value).path
    elif ":" in value:
        path = value.split(":", 1)[1]
    else:
        return None
    return path.strip("/").removesuffix(".git")


def create_worktree(
    worktree_root: Path,
    task_id: int,
    base_branch: str = "main",
) -> tuple[Path, str]:
    base = base_repo_path(worktree_root)
    if not base.exists():
        raise RuntimeError(
            f"base repo not found at {base} — call ensure_base_repo first"
        )

    worktree_path = (worktree_root / f"task-{task_id}").resolve()
    branch_name = f"foundry/task-{task_id}"

    if worktree_path.exists():
        cleanup_worktree(base, worktree_path)

    shell.run(["git", "branch", "-D", branch_name], cwd=base, check=False)
    shell.run(
        ["git", "worktree", "add", str(worktree_path), "-b", branch_name, base_branch],
        cwd=base,
        timeout=GIT_WORKTREE_TIMEOUT_SEC,
    )
    return worktree_path, branch_name


def cleanup_worktree(base_repo: Path, worktree_path: Path) -> None:
    shell.run(
        ["git", "worktree", "unlock", str(worktree_path)],
        cwd=base_repo,
        check=False,
        timeout=GIT_WORKTREE_TIMEOUT_SEC,
    )
    shell.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=base_repo,
        check=False,
        timeout=GIT_WORKTREE_TIMEOUT_SEC,
    )
