from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from foundry.config import Settings
from foundry.models import Task
from foundry.shell import Result
from foundry.stages import pr
from foundry.stages.pr import MAX_FILES_PER_PR, _sanity_check_changes


def test_sanity_check_accepts_small_clean_change() -> None:
    lines = [" M src/foundry/pipeline.py", "?? README.md"]

    _sanity_check_changes(lines)


def test_sanity_check_rejects_too_many_files() -> None:
    lines = [f" M file_{i}.py" for i in range(MAX_FILES_PER_PR + 1)]

    with pytest.raises(RuntimeError, match="sandbox escape"):
        _sanity_check_changes(lines)


def test_sanity_check_accepts_new_limit() -> None:
    lines = [f" M file_{i}.py" for i in range(MAX_FILES_PER_PR)]

    _sanity_check_changes(lines)


def test_sanity_check_rejects_pycache_paths() -> None:
    lines = [" M src/foundry/__pycache__/pipeline.cpython-311.pyc"]

    with pytest.raises(RuntimeError, match="forbidden paths"):
        _sanity_check_changes(lines)


def test_sanity_check_rejects_venv_paths() -> None:
    lines = [" M .venv/bin/activate"]

    with pytest.raises(RuntimeError, match="forbidden paths"):
        _sanity_check_changes(lines)


def test_sanity_check_rejects_ds_store() -> None:
    lines = ["?? .DS_Store"]

    with pytest.raises(RuntimeError, match="forbidden paths"):
        _sanity_check_changes(lines)


def test_sanity_check_allows_env_example() -> None:
    """`.env.example` must not be caught by a forbidden substring — it's legitimate."""
    lines = [" M .env.example"]

    _sanity_check_changes(lines)


def test_pr_create_uses_configured_base_branch(tmp_path: Path) -> None:
    settings = Settings(
        source_repo="owner/sandbox",
        target_repo="owner/sandbox",
        issue_label="agent-task",
        worktree_root=tmp_path / "worktrees",
        db_path=tmp_path / "foundry.sqlite",
        poll_interval_seconds=30,
        base_branch="develop",
    )
    task = Task(
        repo="owner/sandbox",
        issue_number=42,
        issue_title="do the thing",
        issue_body="",
    )
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs) -> Result:
        commands.append(cmd)
        return Result(
            returncode=0,
            stdout="https://github.com/owner/sandbox/pull/1\n",
            stderr="",
        )

    with patch(
        "foundry.stages.pr.commit_and_push_changes",
        return_value={"touched_files": ["README.md"], "files_changed": 1},
    ), patch("foundry.stages.pr.shell.run", side_effect=fake_run):
        pr.run(task, tmp_path, "foundry/task-42", settings)

    pr_create = next(cmd for cmd in commands if cmd[:3] == ["gh", "pr", "create"])
    base_index = pr_create.index("--base")
    assert pr_create[base_index + 1] == "develop"


def test_commit_and_push_reuses_existing_task_commit(tmp_path: Path) -> None:
    task = Task(
        repo="owner/sandbox",
        issue_number=42,
        issue_title="do the thing",
        issue_body="",
    )
    commit_message = "foundry: task #42 — do the thing"
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs) -> Result:
        commands.append(cmd)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return Result(returncode=0, stdout="", stderr="")
        if cmd[:4] == ["git", "log", "-1", "--pretty=%B"]:
            return Result(returncode=0, stdout=f"{commit_message}\n", stderr="")
        if cmd[:4] == ["git", "diff-tree", "--no-commit-id", "--name-only"]:
            return Result(returncode=0, stdout="README.md\nsrc/app.py\n", stderr="")
        return Result(returncode=0, stdout="", stderr="")

    with patch("foundry.stages.pr.shell.run", side_effect=fake_run):
        result = pr.commit_and_push_changes(
            task,
            tmp_path,
            "foundry/task-42-retry",
            commit_message,
        )

    assert result["files_changed"] == 2
    assert result["touched_files"] == ["README.md", "src/app.py"]
    assert ["git", "commit", "-m", commit_message] not in commands
    assert ["git", "push", "-u", "origin", "HEAD:foundry/task-42-retry"] in commands


def test_commit_and_push_allows_clean_noop_when_requested(tmp_path: Path) -> None:
    task = Task(
        repo="owner/sandbox",
        issue_number=42,
        issue_title="do the thing",
        issue_body="",
    )
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs) -> Result:
        commands.append(cmd)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return Result(returncode=0, stdout="", stderr="")
        if cmd[:4] == ["git", "log", "-1", "--pretty=%B"]:
            return Result(returncode=0, stdout="previous task commit\n", stderr="")
        return Result(returncode=0, stdout="", stderr="")

    with patch("foundry.stages.pr.shell.run", side_effect=fake_run):
        result = pr.commit_and_push_changes(
            task,
            tmp_path,
            "foundry/task-42-retry",
            "foundry: address PR feedback for task #42",
            allow_no_changes=True,
        )

    assert result["files_changed"] == 0
    assert result["touched_files"] == []
    assert result["pushed"] is False
    assert ["git", "push", "-u", "origin", "HEAD:foundry/task-42-retry"] not in commands


def test_commit_and_push_pushes_local_commits_ahead_of_remote(tmp_path: Path) -> None:
    task = Task(
        repo="owner/sandbox",
        issue_number=42,
        issue_title="do the thing",
        issue_body="",
    )
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs) -> Result:
        commands.append(cmd)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return Result(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["git", "rev-list", "--count"]:
            return Result(returncode=0, stdout="1\n", stderr="")
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return Result(returncode=0, stdout="db/changelog.xml\n", stderr="")
        return Result(returncode=0, stdout="", stderr="")

    with patch("foundry.stages.pr.shell.run", side_effect=fake_run):
        result = pr.commit_and_push_changes(
            task,
            tmp_path,
            "foundry/task-42-retry",
            "foundry: address PR feedback for task #42",
            allow_no_changes=True,
        )

    assert result["files_changed"] == 1
    assert result["touched_files"] == ["db/changelog.xml"]
    assert result["pushed"] is True
    assert ["git", "commit", "-m", "foundry: address PR feedback for task #42"] not in commands
    assert ["git", "push", "-u", "origin", "HEAD:foundry/task-42-retry"] in commands


def test_commit_and_push_rebases_and_retries_non_fast_forward_push(
    tmp_path: Path,
) -> None:
    task = Task(
        repo="owner/sandbox",
        issue_number=42,
        issue_title="do the thing",
        issue_body="",
    )
    commit_message = "foundry: task #42 - do the thing"
    commands: list[list[str]] = []
    push_attempts = 0

    def fake_run(cmd: list[str], **kwargs) -> Result:
        nonlocal push_attempts
        commands.append(cmd)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return Result(returncode=0, stdout="", stderr="")
        if cmd[:4] == ["git", "log", "-1", "--pretty=%B"]:
            return Result(returncode=0, stdout=f"{commit_message}\n", stderr="")
        if cmd[:4] == ["git", "diff-tree", "--no-commit-id", "--name-only"]:
            return Result(returncode=0, stdout="README.md\n", stderr="")
        if cmd[:3] == ["git", "push", "-u"]:
            push_attempts += 1
            if push_attempts == 1:
                return Result(
                    returncode=1,
                    stdout="",
                    stderr=(
                        " ! [rejected] HEAD -> foundry/task-42 "
                        "(non-fast-forward)\n"
                    ),
                )
            return Result(returncode=0, stdout="", stderr="")
        return Result(returncode=0, stdout="", stderr="")

    with patch("foundry.stages.pr.shell.run", side_effect=fake_run):
        result = pr.commit_and_push_changes(
            task,
            tmp_path,
            "foundry/task-42",
            commit_message,
            allow_no_changes=True,
        )

    push_cmd = ["git", "push", "-u", "origin", "HEAD:foundry/task-42"]
    assert result["pushed"] is True
    assert commands.count(push_cmd) == 2
    assert ["git", "fetch", "origin", "foundry/task-42"] in commands
    assert ["git", "rebase", "origin/foundry/task-42"] in commands
    assert not any("--force" in cmd or "--force-with-lease" in cmd for cmd in commands)


def test_commit_and_push_replaces_stale_task_branch_with_lease(
    tmp_path: Path,
) -> None:
    task = Task(
        repo="owner/sandbox",
        issue_number=42,
        issue_title="do the thing",
        issue_body="",
    )
    commit_message = "foundry: task #42 - do the thing"
    commands: list[list[str]] = []
    push_attempts = 0

    def fake_run(cmd: list[str], **kwargs) -> Result:
        nonlocal push_attempts
        commands.append(cmd)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return Result(returncode=0, stdout="", stderr="")
        if cmd[:4] == ["git", "log", "-1", "--pretty=%B"]:
            return Result(returncode=0, stdout=f"{commit_message}\n", stderr="")
        if cmd[:4] == ["git", "diff-tree", "--no-commit-id", "--name-only"]:
            return Result(returncode=0, stdout="README.md\n", stderr="")
        if cmd[:3] == ["git", "push", "-u"]:
            push_attempts += 1
            if push_attempts == 1:
                return Result(
                    returncode=1,
                    stdout="",
                    stderr=(
                        " ! [rejected] HEAD -> foundry/task-42 "
                        "(non-fast-forward)\n"
                    ),
                )
            return Result(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["git", "rev-parse", "origin/foundry/task-42"]:
            return Result(returncode=0, stdout="abc123\n", stderr="")
        return Result(returncode=0, stdout="", stderr="")

    with patch("foundry.stages.pr.shell.run", side_effect=fake_run):
        result = pr.commit_and_push_changes(
            task,
            tmp_path,
            "foundry/task-42",
            commit_message,
        )

    assert result["pushed"] is True
    assert ["git", "fetch", "origin", "foundry/task-42"] in commands
    assert ["git", "rebase", "origin/foundry/task-42"] not in commands
    assert [
        "git",
        "push",
        "-u",
        "--force-with-lease=refs/heads/foundry/task-42:abc123",
        "origin",
        "HEAD:foundry/task-42",
    ] in commands
