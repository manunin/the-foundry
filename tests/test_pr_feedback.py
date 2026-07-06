from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from foundry import state, workflows
from foundry.config import Settings
from foundry.events import read_events
from foundry.forges import (
    ChangeFeedback,
    CheckResult,
    FeedbackItem,
    ForgeChange,
)
from foundry.models import ForgeKind, Stage, Task, TaskStatus


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        source_repo="owner/sandbox",
        target_repo="owner/sandbox",
        issue_label="agent-task",
        worktree_root=tmp_path / "worktrees",
        db_path=tmp_path / "foundry.sqlite",
        poll_interval_seconds=30,
    )


def _seed_task(db_path: Path) -> Task:
    task = Task(
        repo="owner/sandbox",
        issue_number=42,
        issue_title="do the thing",
        issue_body="please",
        branch_name="foundry/task-1",
        pr_url="https://github.com/owner/sandbox/pull/7",
        status=TaskStatus.DONE,
        current_stage=Stage.DONE,
    )
    return state.upsert_task(db_path, task)


def test_change_feedback_formats_requested_changes_and_failing_ci() -> None:
    feedback = ChangeFeedback(
        items=(
            FeedbackItem(
                "review-1", "Please add a regression test.", "reviewer"
            ),
        ),
        failing_checks=(CheckResult("check-1", "lint", "FAILURE"),),
    )
    formatted = feedback.format()

    assert "Requested changes" in formatted
    assert "Please add a regression test." in formatted
    assert "Failing CI" in formatted
    assert "lint: FAILURE" in formatted


def test_pr_feedback_once_applies_fix_pushes_same_branch_and_comments(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    state.init_db(settings.db_path)
    task = _seed_task(settings.db_path)
    change = ForgeChange(
        number=7,
        title="foundry: task",
        branch="foundry/task-1",
        url="https://github.com/owner/sandbox/pull/7",
    )
    feedback = ChangeFeedback(
        items=(FeedbackItem("review-1", "Add regression coverage.", "reviewer"),)
    )
    provider = MagicMock()
    provider.kind = ForgeKind.GITHUB
    provider.list_changes.return_value = [change]
    provider.load_feedback.return_value = feedback

    with patch(
        "foundry.workflows._prepare_pr_feedback_worktree",
        return_value=(tmp_path / "base", tmp_path / "wt"),
    ), patch(
        "foundry.workflows.worktree.cleanup_worktree"
    ), patch(
        "foundry.workflows.agent_implement_stage.run",
        return_value={"result": "fixed", "response": ""},
    ) as implement, patch(
        "foundry.workflows.verify_stage.run",
        return_value={"passed": True, "report": "green"},
    ), patch(
        "foundry.workflows.pr_stage.commit_and_push_changes",
        return_value={"branch": "foundry/task-1", "files_changed": 1},
    ) as push:
        processed = workflows.pr_feedback_once(settings, provider)

    assert [t.id for t in processed] == [task.id]
    final = state.get_task(settings.db_path, task.id)
    assert final.status == TaskStatus.DONE
    assert final.current_stage == Stage.DONE
    assert final.branch_name == "foundry/task-1"
    assert final.pr_url == "https://github.com/owner/sandbox/pull/7"

    implement_input = implement.call_args.args[1]["plan"]
    assert "Add regression coverage." in implement_input
    push.assert_called_once()
    assert push.call_args.args[2] == "foundry/task-1"
    provider.comment_change.assert_called_once()

    events = read_events(settings.db_path, task_id=task.id)
    feedback_events = [e for e in events if e.kind == "pr_feedback"]
    assert len(feedback_events) == 1
    assert feedback_events[0].payload["status"] == "pending"
    assert feedback_events[0].payload["stage"] == "implement"
    assert "Add regression coverage." in feedback_events[0].payload["feedback"]


def test_pr_feedback_once_skips_prs_without_requested_changes_or_failing_ci(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    state.init_db(settings.db_path)
    _seed_task(settings.db_path)
    change = ForgeChange(
        number=7,
        title="foundry: task",
        branch="foundry/task-1",
        url="https://github.com/owner/sandbox/pull/7",
    )
    provider = MagicMock()
    provider.kind = ForgeKind.GITHUB
    provider.list_changes.return_value = [change]
    provider.load_feedback.return_value = ChangeFeedback()

    with patch("foundry.workflows.pr_feedback") as apply_feedback:
        processed = workflows.pr_feedback_once(settings, provider)

    assert processed == []
    apply_feedback.assert_not_called()
