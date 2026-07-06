from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from foundry import worktree
from foundry.shell import Result


def test_create_worktree_removes_stale_branch_before_add(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    base = root / "_base"
    base.mkdir(parents=True)

    calls: list[tuple[list[str], Path | None, bool, int | None]] = []

    def fake_run(cmd: list[str], cwd: Path | None = None, check: bool = True, **kwargs):
        calls.append((cmd, cwd, check, kwargs.get("timeout")))

    with patch("foundry.worktree.shell.run", side_effect=fake_run):
        path, branch = worktree.create_worktree(root, task_id=6)

    assert path == (root / "task-6").resolve()
    assert branch == "foundry/task-6"
    assert (
        ["git", "branch", "-D", "foundry/task-6"],
        base,
        False,
        None,
    ) in calls
    assert calls[-1] == (
        ["git", "worktree", "add", str(path), "-b", "foundry/task-6", "main"],
        base,
        True,
        worktree.GIT_WORKTREE_TIMEOUT_SEC,
    )


def test_cleanup_worktree_unlocks_interrupted_initialization(tmp_path: Path) -> None:
    base = tmp_path / "_base"
    path = tmp_path / "task-6"

    with patch("foundry.worktree.shell.run") as run:
        worktree.cleanup_worktree(base, path)

    assert run.call_args_list == [
        call(
            ["git", "worktree", "unlock", str(path)],
            cwd=base,
            check=False,
            timeout=worktree.GIT_WORKTREE_TIMEOUT_SEC,
        ),
        call(
            ["git", "worktree", "remove", "--force", str(path)],
            cwd=base,
            check=False,
            timeout=worktree.GIT_WORKTREE_TIMEOUT_SEC,
        ),
    ]


def test_ensure_base_repo_syncs_configured_base_branch(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    base = root / "_base"
    base.mkdir(parents=True)
    calls: list[tuple[list[str], Path | None]] = []

    def fake_run(cmd: list[str], cwd: Path | None = None, **kwargs):
        calls.append((cmd, cwd))
        if cmd == ["git", "remote", "get-url", "origin"]:
            return Result(0, "https://github.com/owner/sandbox.git\n", "")
        return Result(0, "", "")

    with patch("foundry.worktree.shell.run", side_effect=fake_run):
        out = worktree.ensure_base_repo(root, "owner/sandbox", "develop")

    assert out == base
    assert calls == [
        (["git", "remote", "get-url", "origin"], base),
        (["git", "fetch", "origin"], base),
        (["git", "checkout", "develop"], base),
        (["git", "reset", "--hard", "origin/develop"], base),
    ]


def test_ensure_base_repo_delegates_initial_clone_to_forge(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    base = root / "_base"
    provider = MagicMock()

    with patch("foundry.worktree.shell.run"):
        out = worktree.ensure_base_repo(
            root, "target-group/code", "main", provider
        )

    assert out == base
    provider.clone.assert_called_once_with("target-group/code", base)


def test_ensure_base_repo_rejects_origin_for_another_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "worktrees"
    base = root / "_base"
    base.mkdir(parents=True)

    with patch(
        "foundry.worktree.shell.run",
        return_value=Result(
            0,
            "http://gitlab.example/group/old-project.git\n",
            "",
        ),
    ):
        with pytest.raises(RuntimeError, match="group/old-project"):
            worktree.ensure_base_repo(root, "group/new-project")


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("http://gitlab.example/group/sub/repo.git", "group/sub/repo"),
        ("git@gitlab.example:group/repo.git", "group/repo"),
        ("ssh://git@gitlab.example/group/repo.git", "group/repo"),
    ],
)
def test_repo_from_remote_normalizes_supported_git_urls(
    remote: str, expected: str
) -> None:
    assert worktree._repo_from_remote(remote) == expected
