from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from foundry import state
from foundry.config import Settings
from foundry.events import read_events, record_event
from foundry.forges import ForgeComment
from foundry.models import Stage, Task, TaskStatus
from foundry.shell import Result
from foundry.stages import fetch as fetch_stage


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        source_repo="owner/sandbox",
        target_repo="owner/sandbox",
        issue_label="agent-task",
        worktree_root=tmp_path / "worktrees",
        db_path=tmp_path / "foundry.sqlite",
        poll_interval_seconds=30,
        issue_assignee="octocat",
        issue_milestone="v1",
        issue_labels=("agent-task", "queue/backend"),
        issue_limit=25,
    )


def test_fetch_filters_and_sorts_by_priority_labels(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state.init_db(settings.db_path)
    issues = [
        {
            "number": 3,
            "title": "normal",
            "body": "",
            "labels": [{"name": "agent-task"}],
        },
        {
            "number": 1,
            "title": "p1",
            "body": "",
            "labels": [{"name": "priority/p1"}],
        },
        {
            "number": 2,
            "title": "p0",
            "body": "",
            "labels": [{"name": "priority/p0"}],
        },
    ]
    seen_cmd: list[str] = []

    def _run(cmd: list[str]) -> Result:
        seen_cmd.extend(cmd)
        return Result(returncode=0, stdout=json.dumps(issues), stderr="")

    with patch("foundry.stages.fetch.shell.run", side_effect=_run):
        tasks = fetch_stage.fetch(settings)

    assert [task.issue_number for task in tasks] == [2, 1, 3]
    assert seen_cmd == [
        "gh",
        "issue",
        "list",
        "--repo", "owner/sandbox",
        "--state", "open",
        "--json", "number,title,body,labels",
        "--limit", "25",
        "--label", "agent-task",
        "--label", "queue/backend",
        "--assignee", "octocat",
        "--milestone", "v1",
    ]


def test_fetch_issue_bypasses_queue_filters(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state.init_db(settings.db_path)
    seen_cmd: list[str] = []

    def _run(cmd: list[str]) -> Result:
        seen_cmd.extend(cmd)
        return Result(
            returncode=0,
            stdout=json.dumps(
                {
                    "number": 7,
                    "title": "manual",
                    "body": "go",
                    "labels": [{"name": "ui-agent-test"}],
                }
            ),
            stderr="",
        )

    with patch("foundry.stages.fetch.shell.run", side_effect=_run):
        task = fetch_stage.fetch_issue(settings, 7)

    assert task.issue_number == 7
    assert task.issue_title == "manual"
    assert task.issue_labels == ("ui-agent-test",)
    assert seen_cmd == [
        "gh",
        "issue",
        "view",
        "7",
        "--repo", "owner/sandbox",
        "--json", "number,title,body,labels",
    ]


def test_fetch_refreshes_labels_before_plan_and_freezes_after_plan(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    state.init_db(settings.db_path)
    existing = state.upsert_task(
        settings.db_path,
        Task(
            repo=settings.source_repo,
            issue_number=4,
            issue_title="old",
            issue_body="old",
            current_stage=Stage.CONTEXT,
            issue_labels=("agent-task",),
        ),
    )
    provider = MagicMock()
    provider.list_issues.return_value = [
        MagicMock(
            number=4,
            title="new",
            body="new body",
            labels=("agent-task", "ui-agent-test"),
            url="https://example/issues/4",
        )
    ]
    provider.list_issue_comments.return_value = []

    fetch_stage.fetch(settings, provider)
    refreshed = state.get_task(settings.db_path, existing.id)
    assert refreshed.issue_labels == ("agent-task", "ui-agent-test")
    assert refreshed.issue_title == "new"

    refreshed.current_stage = Stage.PLAN
    state.upsert_task(settings.db_path, refreshed)
    provider.list_issues.return_value[0].labels = ("agent-task",)
    fetch_stage.fetch(settings, provider)
    assert state.get_task(settings.db_path, existing.id).issue_labels == (
        "agent-task",
        "ui-agent-test",
    )


def test_fetch_includes_pending_tasks_from_sqlite_queue(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state.init_db(settings.db_path)
    queued = state.upsert_task(
        settings.db_path,
        Task(
            repo=settings.source_repo,
            issue_number=99,
            issue_title="already queued",
            issue_body="",
        ),
    )

    with patch(
        "foundry.stages.fetch.shell.run",
        return_value=Result(returncode=0, stdout="[]", stderr=""),
    ):
        tasks = fetch_stage.fetch(settings)

    assert [task.id for task in tasks] == [queued.id]


def test_fetch_resumes_blocked_plan_with_comment_and_previous_draft(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    state.init_db(settings.db_path)
    task = state.upsert_task(
        settings.db_path,
        Task(
            repo=settings.source_repo,
            issue_number=7,
            issue_title="Create file",
            issue_body="Create a Markdown file.",
            status=TaskStatus.BLOCKED,
            current_stage=Stage.PLAN,
            worktree_path=str(tmp_path / "worktrees" / "task-1"),
            branch_name="foundry/task-1",
        ),
    )
    state.save_stage_result(
        settings.db_path, task.id, Stage.CONTEXT, {"repo": task.repo}
    )
    state.save_stage_result(
        settings.db_path,
        task.id,
        Stage.PLAN,
        {"plan": "Draft plan\n\nWhich filename?\n\nNEED_VERIFICATION"},
    )
    record_event(
        settings.db_path,
        task.id,
        Stage.ISSUE_COMMENT.value,
        "stage_finished",
        {"output": {"issue_number": 7}},
    )
    provider = MagicMock()
    provider.list_issues.return_value = []
    provider.list_issue_comments.return_value = [
        ForgeComment(
            "note-2",
            "Use test-foundry.md.",
            "maintainer",
            int(time.time() * 1000) + 1_000,
        )
    ]

    tasks = fetch_stage.fetch(settings, provider)

    assert [item.id for item in tasks] == [task.id]
    resumed = state.get_task(settings.db_path, task.id)
    assert resumed is not None
    assert resumed.status == TaskStatus.PENDING
    assert resumed.current_stage == Stage.PLAN
    assert resumed.worktree_path == task.worktree_path
    assert "Draft plan" in resumed.issue_body
    assert "Use test-foundry.md." in resumed.issue_body
    assert state.get_stage_result(
        settings.db_path, task.id, Stage.CONTEXT
    ) == {"repo": task.repo}
    assert state.get_stage_result(settings.db_path, task.id, Stage.PLAN) is None
    clarification_events = [
        event
        for event in read_events(settings.db_path, task.id)
        if event.kind == "human_clarification_received"
    ]
    assert clarification_events[-1].payload["comment_ids"] == ["note-2"]


def test_fetch_does_not_reuse_processed_clarification(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state.init_db(settings.db_path)
    task = state.upsert_task(
        settings.db_path,
        Task(
            repo=settings.source_repo,
            issue_number=8,
            issue_title="Blocked",
            issue_body="Body",
            status=TaskStatus.BLOCKED,
            current_stage=Stage.PLAN,
        ),
    )
    record_event(
        settings.db_path,
        task.id,
        Stage.ISSUE_COMMENT.value,
        "stage_finished",
        {},
    )
    record_event(
        settings.db_path,
        task.id,
        Stage.FETCH.value,
        "human_clarification_received",
        {"comment_ids": ["note-1"], "authors": ["maintainer"]},
    )
    provider = MagicMock()
    provider.list_issues.return_value = []
    provider.list_issue_comments.return_value = [
        ForgeComment(
            "note-1",
            "Already processed.",
            "maintainer",
            int(time.time() * 1000) + 1_000,
        )
    ]

    tasks = fetch_stage.fetch(settings, provider)

    assert tasks == []
    assert state.get_task(settings.db_path, task.id).status == TaskStatus.BLOCKED
