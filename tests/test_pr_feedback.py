from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from foundry import state, workflows
from foundry.config import Settings
from foundry.events import read_events
from foundry.forges import (
    TRACK_CI_FEEDBACK,
    ChangeFeedback,
    CheckResult,
    FeedbackItem,
    ForgeChange,
)
from foundry.models import ForgeKind, Stage, Task, TaskStatus
from foundry.shell import Result


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


def _latest_implement_prompt(db_path: Path, task_id: int) -> str:
    events = read_events(db_path, task_id=task_id)
    started = [
        event
        for event in events
        if event.stage == Stage.IMPLEMENT.value and event.kind == "stage_started"
    ]
    return str(started[-1].payload["input"]["prompt"])


def test_change_feedback_includes_failing_ci() -> None:
    feedback = ChangeFeedback(
        items=(
            FeedbackItem(
                "review-1", "Please add a regression test.", "reviewer"
            ),
        ),
        failing_checks=(CheckResult("check-1", "lint", "FAILURE"),),
    )
    formatted = feedback.format()

    assert TRACK_CI_FEEDBACK is True
    assert feedback.actionable is True
    assert "Requested changes" in formatted
    assert "Please add a regression test." in formatted
    assert "Failing CI" in formatted
    assert "lint: FAILURE" in formatted


def test_change_feedback_allows_ci_only_feedback() -> None:
    feedback = ChangeFeedback(
        failing_checks=(CheckResult("check-1", "lint", "FAILURE"),),
    )

    assert feedback.actionable is True
    assert feedback.format() == "### Failing CI\n- lint: FAILURE"


def test_change_feedback_fingerprint_includes_ci_details() -> None:
    first = ChangeFeedback(
        failing_checks=(
            CheckResult(
                "pipeline-1",
                "pipeline",
                "FAILED",
                "https://gitlab.example/pipelines/1",
                "pytest: FAILED; url: https://gitlab.example/jobs/1",
            ),
        )
    )
    second = ChangeFeedback(
        failing_checks=(
            CheckResult(
                "pipeline-1",
                "pipeline",
                "FAILED",
                "https://gitlab.example/pipelines/1",
                "ruff: FAILED; url: https://gitlab.example/jobs/2",
            ),
        )
    )

    assert first.fingerprint != second.fingerprint


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
        items=(FeedbackItem("review-1", "Add regression coverage.", "reviewer"),),
        failing_checks=(CheckResult("pipeline-1", "pipeline", "FAILED"),),
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
        "foundry.workflows._changed_files_for_pr_feedback",
        return_value=[],
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
    assert "Failing CI" in implement_input
    assert "pipeline: FAILED" in implement_input
    push.assert_called_once()
    assert push.call_args.args[2] == "foundry/task-1"
    provider.comment_change.assert_called_once()

    events = read_events(settings.db_path, task_id=task.id)
    feedback_events = [e for e in events if e.kind == "pr_feedback"]
    assert len(feedback_events) == 1
    assert feedback_events[0].payload["status"] == "pending"
    assert feedback_events[0].payload["stage"] == "implement"
    assert "Add regression coverage." in feedback_events[0].payload["feedback"]
    prompt = _latest_implement_prompt(settings.db_path, task.id)
    assert "applying feedback from an existing change request" in prompt
    assert "Add regression coverage." in prompt
    assert "Human clarification from issue comments" not in prompt
    assert "Этап 1" not in prompt
    assert "Failing CI" in prompt
    assert "pipeline: FAILED" in prompt
    assert "Do not edit CI/CD configuration files" in prompt
    assert ".gitlab-ci.yml" in prompt


def test_pr_feedback_retry_preserves_fix_and_includes_verifier_report(
    tmp_path: Path,
) -> None:
    settings = replace(_settings(tmp_path), max_implement_attempts=2)
    state.init_db(settings.db_path)
    task = _seed_task(settings.db_path)
    change = ForgeChange(
        number=7,
        title="foundry: task",
        branch="foundry/task-1",
        url="https://github.com/owner/sandbox/pull/7",
    )
    feedback = ChangeFeedback(
        items=(FeedbackItem("review-1", "Fix the build.", "reviewer"),)
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
    ) as cleanup, patch(
        "foundry.workflows.agent_implement_stage.run",
        side_effect=[
            {"result": "added the first dependency", "response": ""},
            {"result": "corrected the dependency", "response": ""},
        ],
    ) as implement, patch(
        "foundry.workflows.verify_stage.run",
        side_effect=[
            {
                "passed": False,
                "retryable": True,
                "failure_kind": "acceptance",
                "report": "JpaRepository requires spring-data-jpa",
            },
            {"passed": True, "report": "green"},
        ],
    ), patch(
        "foundry.workflows.pr_stage.commit_and_push_changes",
        return_value={"branch": "foundry/task-1", "files_changed": 1},
    ) as push:
        processed = workflows.pr_feedback_once(settings, provider)

    assert [item.id for item in processed] == [task.id]
    assert implement.call_count == 2
    assert implement.call_args_list[0].args[2] == tmp_path / "wt"
    assert implement.call_args_list[1].args[2] == tmp_path / "wt"
    retry_plan = implement.call_args_list[1].args[1]
    assert "added the first dependency" in retry_plan["plan"]
    assert "JpaRepository requires spring-data-jpa" in retry_plan["plan"]
    assert "Preserve and review all existing task changes" in retry_plan["plan"]
    retry_prompt = _latest_implement_prompt(settings.db_path, task.id)
    assert "Previous implement summary" in retry_prompt
    assert "Previous verification report" in retry_prompt
    push.assert_called_once()
    cleanup.assert_called_once()


def test_pr_feedback_openspec_mode_includes_openspec_context(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings = replace(settings, openspec_mode=True)
    state.init_db(settings.db_path)
    task = _seed_task(settings.db_path)
    task.issue_body += "\n\n## Human clarification from issue comments\nstale"
    task = state.upsert_task(settings.db_path, task)
    change = ForgeChange(
        number=7,
        title="foundry: task",
        branch="foundry/task-1",
        url="https://github.com/owner/sandbox/pull/7",
    )
    feedback = ChangeFeedback(
        items=(
            FeedbackItem(
                "review-1",
                "Please validate openspec/changes/demo/tasks.md and tick done tasks.",
                "reviewer",
            ),
        )
    )
    provider = MagicMock()
    provider.kind = ForgeKind.GITHUB
    provider.list_changes.return_value = [change]
    provider.load_feedback.return_value = feedback

    with patch(
        "foundry.workflows._prepare_pr_feedback_worktree",
        return_value=(tmp_path / "base", tmp_path / "wt"),
    ), patch("foundry.workflows.worktree.cleanup_worktree"), patch(
        "foundry.workflows.openspec.collect_context",
        return_value={"present": True, "forced": True},
    ), patch(
        "foundry.workflows.openspec.format_context",
        return_value=["### OpenSpec", "Active change: demo"],
    ), patch(
        "foundry.workflows.openspec.build_implementation_handoff",
        return_value="Active change: demo\n- openspec/changes/demo/tasks.md",
    ) as handoff, patch(
        "foundry.workflows.agent_plan_stage.run",
        return_value={
            "plan": "Updated OpenSpec tasks",
            "summary": "updated",
            "openspec_artifacts": ["openspec/changes/demo/tasks.md"],
        },
    ) as plan, patch(
        "foundry.workflows.agent_implement_stage.run",
        return_value={"result": "fixed", "response": ""},
    ) as implement, patch(
        "foundry.workflows.verify_stage.run",
        return_value={"passed": True, "report": "green"},
    ), patch(
        "foundry.workflows.pr_stage.commit_and_push_changes",
        return_value={"branch": "foundry/task-1", "files_changed": 1},
    ):
        workflows.pr_feedback_once(settings, provider)

    handoff.assert_called_once_with(
        tmp_path / "wt",
        timeout_sec=settings.verify_command_timeout_sec,
        plan_artifacts=["openspec/changes/demo/tasks.md"],
    )
    planner_input = plan.call_args.kwargs["planner_input"]
    assert "Update the existing OpenSpec change" in planner_input
    assert "## Feedback to plan" in planner_input
    assert "Please validate openspec/changes/demo/tasks.md" in planner_input
    implementation_plan = implement.call_args.args[1]
    assert implementation_plan["openspec_artifacts"] == [
        "openspec/changes/demo/tasks.md"
    ]
    assert "Please validate openspec/changes/demo/tasks.md" in implementation_plan[
        "_pr_feedback"
    ]
    assert "_prompt_template" not in implementation_plan

    events = read_events(settings.db_path, task_id=task.id)
    feedback_events = [event for event in events if event.kind == "pr_feedback"]
    assert feedback_events[0].stage == Stage.PLAN.value
    assert feedback_events[0].payload["stage"] == Stage.PLAN.value
    plan_started = [
        event
        for event in events
        if event.stage == Stage.PLAN.value and event.kind == "stage_started"
    ]
    assert "OpenSpec planning agent" in plan_started[-1].payload["input"]["prompt"]

    prompt = _latest_implement_prompt(settings.db_path, task.id)
    assert "OpenSpec implementation agent" in prompt
    assert "## PR feedback to address" in prompt
    assert "Please validate openspec/changes/demo/tasks.md" in prompt
    assert "Human clarification from issue comments" not in prompt


def test_openspec_pr_feedback_plan_can_request_human_input(tmp_path: Path) -> None:
    settings = replace(_settings(tmp_path), openspec_mode=True)
    state.init_db(settings.db_path)
    task = _seed_task(settings.db_path)
    change = ForgeChange(
        number=7,
        title="foundry: task",
        branch="foundry/task-1",
        url="https://github.com/owner/sandbox/pull/7",
    )
    feedback = ChangeFeedback(
        items=(FeedbackItem("review-1", "Clarify expected behavior.", "reviewer"),)
    )
    provider = MagicMock()
    provider.kind = ForgeKind.GITHUB
    provider.list_changes.return_value = [change]
    provider.load_feedback.return_value = feedback

    with patch(
        "foundry.workflows._prepare_pr_feedback_worktree",
        return_value=(tmp_path / "base", tmp_path / "wt"),
    ), patch("foundry.workflows.worktree.cleanup_worktree"), patch(
        "foundry.workflows.openspec.collect_context",
        return_value={"present": True, "forced": True},
    ), patch(
        "foundry.workflows.openspec.format_context",
        return_value=["### OpenSpec", "Active change: demo"],
    ), patch(
        "foundry.workflows.openspec.build_implementation_handoff",
        return_value="Active change: demo",
    ), patch(
        "foundry.workflows.agent_plan_stage.run",
        return_value={
            "plan": "Which behavior is expected?\nNEED_VERIFICATION",
            "summary": "needs input",
            "openspec_artifacts": [],
        },
    ), patch(
        "foundry.workflows._block_for_human",
        return_value=task,
    ) as block, patch(
        "foundry.workflows.agent_implement_stage.run"
    ) as implement:
        workflows.pr_feedback_once(settings, provider)

    block.assert_called_once()
    assert block.call_args.kwargs["blocked_stage"] is Stage.PLAN
    implement.assert_not_called()


def test_pr_feedback_allows_verified_noop_fix(tmp_path: Path) -> None:
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
        items=(
            FeedbackItem(
                "review-1",
                "Validate openspec/changes/demo/tasks.md.",
                "reviewer",
            ),
        )
    )
    provider = MagicMock()
    provider.kind = ForgeKind.GITHUB
    provider.list_changes.return_value = [change]
    provider.load_feedback.return_value = feedback

    with patch(
        "foundry.workflows._prepare_pr_feedback_worktree",
        return_value=(tmp_path / "base", tmp_path / "wt"),
    ), patch("foundry.workflows.worktree.cleanup_worktree"), patch(
        "foundry.workflows.agent_implement_stage.run",
        return_value={"result": "validated", "response": ""},
    ), patch(
        "foundry.workflows.verify_stage.run",
        return_value={"passed": True, "report": "green"},
    ), patch(
        "foundry.workflows._changed_files_for_pr_feedback",
        return_value=[],
    ), patch(
        "foundry.workflows.pr_stage.commit_and_push_changes",
        return_value={
            "branch": "foundry/task-1",
            "files_changed": 0,
            "touched_files": [],
            "pushed": False,
        },
    ) as push:
        processed = workflows.pr_feedback_once(settings, provider)

    assert [t.id for t in processed] == [task.id]
    push.assert_called_once()
    assert push.call_args.kwargs["allow_no_changes"] is True
    provider.comment_change.assert_called_once()
    comment = provider.comment_change.call_args.args[2]
    assert "no follow-up commit was needed" in comment
    assert "pushed a follow-up commit" not in comment
    final = state.get_task(settings.db_path, task.id)
    assert final.status == TaskStatus.DONE
    assert final.current_stage == Stage.DONE


def test_pr_feedback_once_applies_ci_only_feedback(
    tmp_path: Path,
) -> None:
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
        failing_checks=(CheckResult("pipeline-1", "pipeline", "FAILED"),)
    )
    provider = MagicMock()
    provider.kind = ForgeKind.GITHUB
    provider.list_changes.return_value = [change]
    provider.load_feedback.return_value = feedback

    with patch(
        "foundry.workflows.pr_feedback",
        return_value=task,
    ) as apply_feedback:
        processed = workflows.pr_feedback_once(settings, provider)

    assert [t.id for t in processed] == [task.id]
    apply_feedback.assert_called_once()
    final = state.get_task(settings.db_path, task.id)
    assert final.status == TaskStatus.DONE
    assert final.current_stage == Stage.DONE
    assert state.get_repo_memory(
        settings.db_path,
        settings.target_repo,
        f"pr_feedback_hash:{task.id}",
    )


def test_pr_feedback_once_does_not_dedupe_failed_attempt(
    tmp_path: Path,
) -> None:
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
        failing_checks=(
            CheckResult(
                "pipeline-1",
                "pipeline",
                "FAILED",
                "https://gitlab.example/pipelines/1",
                "pytest: FAILED; url: https://gitlab.example/jobs/1",
            ),
        )
    )
    provider = MagicMock()
    provider.kind = ForgeKind.GITHUB
    provider.list_changes.return_value = [change]
    provider.load_feedback.return_value = feedback

    with patch(
        "foundry.workflows.pr_feedback",
        side_effect=RuntimeError("verification failed"),
    ), pytest.raises(RuntimeError, match="verification failed"):
        workflows.pr_feedback_once(settings, provider)

    assert (
        state.get_repo_memory(
            settings.db_path,
            settings.target_repo,
            f"pr_feedback_hash:{task.id}",
        )
        is None
    )
    final = state.get_task(settings.db_path, task.id)
    assert final.status == TaskStatus.FAILED
    assert final.current_stage == Stage.FAILED


def test_pr_feedback_ci_guard_rejects_ci_config_edits(tmp_path: Path) -> None:
    feedback = ChangeFeedback(
        failing_checks=(
            CheckResult("pipeline-1", "pipeline", "FAILED", details="pytest failed"),
        )
    )

    with patch(
        "foundry.workflows.shell.run",
        return_value=Result(0, ".gitlab-ci.yml\nsrc/app.py\n", ""),
    ):
        with pytest.raises(RuntimeError, match="CI/CD config files"):
            workflows._guard_pr_feedback_ci_config_edits(
                feedback,
                feedback.format(),
                tmp_path,
                "foundry/task-1",
            )


def test_pr_feedback_ci_guard_allows_explicit_ci_config_feedback(
    tmp_path: Path,
) -> None:
    feedback = ChangeFeedback(
        items=(
            FeedbackItem(
                "review-1",
                "The .gitlab-ci.yml build job uses the wrong Maven command.",
                "reviewer",
            ),
        ),
        failing_checks=(
            CheckResult("pipeline-1", "pipeline", "FAILED", details="pytest failed"),
        ),
    )

    with patch(
        "foundry.workflows.shell.run",
        return_value=Result(0, ".gitlab-ci.yml\n", ""),
    ):
        workflows._guard_pr_feedback_ci_config_edits(
            feedback,
            feedback.format(),
            tmp_path,
            "foundry/task-1",
        )


def test_pr_feedback_once_ignores_branch_match_without_persisted_pr_url(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    state.init_db(settings.db_path)
    task = Task(
        repo="owner/sandbox",
        issue_number=43,
        issue_title="new issue after db reset",
        issue_body="please",
        branch_name="foundry/task-1",
        pr_url=None,
        status=TaskStatus.RUNNING,
        current_stage=Stage.PLAN,
    )
    state.upsert_task(settings.db_path, task)
    change = ForgeChange(
        number=7,
        title="old foundry task",
        branch="foundry/task-1",
        url="https://github.com/owner/sandbox/pull/7",
    )
    provider = MagicMock()
    provider.kind = ForgeKind.GITHUB
    provider.list_changes.return_value = [change]
    provider.load_feedback.return_value = ChangeFeedback(
        failing_checks=(CheckResult("pipeline-1", "pipeline", "FAILED"),)
    )

    with patch("foundry.workflows.pr_feedback") as apply_feedback:
        processed = workflows.pr_feedback_once(settings, provider)

    assert processed == []
    apply_feedback.assert_not_called()


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
