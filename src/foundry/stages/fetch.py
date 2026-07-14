from __future__ import annotations

from foundry import shell, state  # noqa: F401
from foundry.config import Settings
from foundry.events import read_events, record_event
from foundry.forges import (
    ForgeComment,
    ForgeIssue,
    ForgeProvider,
    IssueQuery,
    provider_for,
)
from foundry.models import Stage, Task, TaskStatus


PRIORITY_RANK = {
    "priority/p0": 0,
    "priority/p1": 1,
}


def _issue_labels(settings: Settings) -> tuple[str, ...]:
    return settings.issue_labels or (
        (settings.issue_label,) if settings.issue_label else ()
    )


def _issue_priority(issue: ForgeIssue) -> int:
    names = {label.lower() for label in issue.labels}
    return min(
        (PRIORITY_RANK[name] for name in names if name in PRIORITY_RANK),
        default=99,
    )


def _issue_to_task(settings: Settings, issue: ForgeIssue) -> Task:
    return Task(
        repo=settings.source_repo,
        issue_number=issue.number,
        issue_title=issue.title,
        issue_body=issue.body,
        forge=settings.forge,
        forge_host=settings.forge_host,
        issue_url=issue.url,
        issue_labels=tuple(dict.fromkeys(label.strip() for label in issue.labels if label.strip())),
    )


def _upsert_issue(settings: Settings, issue: ForgeIssue) -> Task:
    incoming = _issue_to_task(settings, issue)
    existing = state.get_task_by_issue(
        settings.db_path, settings.source_repo, issue.number
    )
    if existing is None:
        return state.upsert_task(settings.db_path, incoming)
    if existing.current_stage in {Stage.FETCH, Stage.CONTEXT}:
        existing.issue_title = incoming.issue_title
        existing.issue_body = incoming.issue_body
        existing.issue_url = incoming.issue_url
        existing.issue_labels = incoming.issue_labels
        existing.forge = incoming.forge
        existing.forge_host = incoming.forge_host
        return state.upsert_task(settings.db_path, existing)
    return existing


def fetch_issue(
    settings: Settings, issue_number: int, provider: ForgeProvider | None = None
) -> Task:
    """Upsert a single issue for a manual run, bypassing label queue filters."""
    issue = (provider or provider_for(settings)).get_issue(
        settings.source_repo, issue_number
    )
    return _upsert_issue(settings, issue)


def fetch(settings: Settings, provider: ForgeProvider | None = None) -> list[Task]:
    """Pull open issues with the configured label and upsert into the DB.

    Returns the list of tasks that are ready to be processed (pending status).
    """
    active_provider = provider or provider_for(settings)
    issues = active_provider.list_issues(
        settings.source_repo,
        IssueQuery(
            labels=_issue_labels(settings),
            assignee=settings.issue_assignee,
            milestone=settings.issue_milestone,
            limit=settings.issue_limit,
        ),
    )
    issues = sorted(issues, key=_issue_priority)

    ready: list[Task] = []
    ready_ids: set[int] = set()
    for issue in issues:
        existing = state.get_task_by_issue(
            settings.db_path, settings.source_repo, issue.number
        )
        if existing is None:
            task = _upsert_issue(settings, issue)
            ready.append(task)
            if task.id is not None:
                ready_ids.add(task.id)
        else:
            existing = _upsert_issue(settings, issue)
            # Re-queue pending tasks and resume interrupted running tasks.
            if existing.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                ready.append(existing)
                if existing.id is not None:
                    ready_ids.add(existing.id)
    for task in state.list_tasks(settings.db_path, TaskStatus.BLOCKED):
        if task.repo != settings.source_repo:
            continue
        resumed = _resume_from_issue_comments(settings, task, active_provider)
        if resumed is None:
            continue
        ready.append(resumed)
        if resumed.id is not None:
            ready_ids.add(resumed.id)
    for task in state.list_tasks(settings.db_path, TaskStatus.PENDING):
        if task.repo != settings.source_repo or task.id in ready_ids:
            continue
        ready.append(task)
        if task.id is not None:
            ready_ids.add(task.id)
    for task in state.list_tasks(settings.db_path, TaskStatus.RUNNING):
        if task.repo != settings.source_repo or task.id in ready_ids:
            continue
        if task.current_stage in {
            Stage.IMPLEMENT,
            Stage.VERIFY,
            Stage.UI_TESTS,
            Stage.PR,
            Stage.CONTEXT,
            Stage.PLAN,
            Stage.FETCH,
        }:
            ready.append(task)
    return ready


def _resume_from_issue_comments(
    settings: Settings,
    task: Task,
    provider: ForgeProvider,
) -> Task | None:
    if task.id is None:
        return None
    events = read_events(settings.db_path, task.id)
    blocked_events = [
        event
        for event in events
        if event.stage == Stage.ISSUE_COMMENT.value
        and event.kind in {"stage_started", "stage_finished"}
    ]
    if not blocked_events:
        return None
    processed_ids = {
        str(comment_id)
        for event in events
        if event.kind == "human_clarification_received"
        for comment_id in event.payload.get("comment_ids", [])
    }
    comments = provider.list_issue_comments(task.repo, task.issue_number)
    clarifications = [
        comment
        for comment in comments
        if comment.created_at_ms > blocked_events[0].ts_ms
        and comment.external_id not in processed_ids
        and not _is_foundry_comment(comment)
        and comment.body
    ]
    if not clarifications:
        return None

    clarification_text = "\n\n".join(
        f"{comment.author}: {comment.body}" for comment in clarifications
    )
    continuation_parts = [task.issue_body.rstrip()]
    if task.current_stage == Stage.PLAN:
        previous_plan = state.get_stage_result(
            settings.db_path, task.id, Stage.PLAN
        )
        if previous_plan and previous_plan.get("plan"):
            continuation_parts.extend(
                [
                    "## Previous planning draft (not final)",
                    str(previous_plan["plan"]).strip(),
                ]
            )
    continuation_parts.extend(
        [
            "## Human clarification from issue comments",
            clarification_text,
            (
                "Continue the blocked stage using the previous work and this "
                "clarification. Ask again if more information is required."
            ),
        ]
    )
    task.issue_body = "\n\n".join(continuation_parts).strip()
    task = state.resume_task_with_clarification(settings.db_path, task)
    record_event(
        settings.db_path,
        task.id,
        Stage.FETCH.value,
        "human_clarification_received",
        {
            "comment_ids": [
                comment.external_id for comment in clarifications
            ],
            "authors": sorted({comment.author for comment in clarifications}),
        },
    )
    return task


def _is_foundry_comment(comment: ForgeComment) -> bool:
    return comment.body.startswith(
        "The Foundry needs human input before continuing this task."
    )
