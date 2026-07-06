from __future__ import annotations

import traceback

import structlog

from foundry import observability, state
from foundry.config import Settings
from foundry.forges import ForgeProvider, provider_for
from foundry.models import Stage, Task, TaskStatus
from foundry.stages import fetch as fetch_stage
from foundry.workflows import dev_task

log = structlog.get_logger()

# Stages before the first side-effecting step. Failures here are almost always
# infrastructure hiccups (network, auth, worktree setup) — re-queue instead of
# marking the task terminally failed.
PRE_IMPLEMENT_STAGES = {Stage.FETCH, Stage.CONTEXT, Stage.PLAN}


def _process_tasks(
    settings: Settings, tasks: list[Task], provider: ForgeProvider | None = None
) -> list[Task]:
    active_provider = provider or provider_for(settings)
    processed: list[Task] = []
    for task in tasks:
        try:
            processed.append(dev_task(settings, task, active_provider))
        except Exception as e:
            failed_stage = task.current_stage
            tb = traceback.format_exc()
            state.append_log(
                settings.db_path,
                task.id,
                failed_stage,
                {"error": str(e), "traceback": tb},
            )
            if failed_stage in PRE_IMPLEMENT_STAGES:
                task.status = TaskStatus.PENDING
                task.current_stage = Stage.FETCH
                log.warning(
                    "task.requeued",
                    task_id=task.id,
                    stage=failed_stage.value,
                    error=str(e),
                )
            else:
                task.status = TaskStatus.FAILED
                task.current_stage = Stage.FAILED
                log.error(
                    "task.failed",
                    task_id=task.id,
                    stage=failed_stage.value,
                    error=str(e),
                )
            state.upsert_task(settings.db_path, task)
            processed.append(task)
    return processed


def run_once(settings: Settings) -> list[Task]:
    """Fetch pending tasks and run each through the `dev_task` workflow.

    Failures in one task do not stop the batch — they're persisted and the next
    task proceeds. Returns the final list of tasks touched in this run.
    """
    observability.init_langfuse()
    state.init_db(settings.db_path)
    provider = provider_for(settings)
    tasks = fetch_stage.fetch(settings, provider)
    log.info("run.fetched", count=len(tasks))

    processed = _process_tasks(settings, tasks, provider)
    observability.flush()
    return processed


def run_issue(settings: Settings, issue_number: int) -> Task:
    """Run one issue immediately, without changing the polling query."""
    observability.init_langfuse()
    state.init_db(settings.db_path)
    provider = provider_for(settings)
    task = fetch_stage.fetch_issue(settings, issue_number, provider)
    log.info("run_issue.fetched", issue_number=issue_number, task_id=task.id)
    processed = _process_tasks(settings, [task], provider)
    observability.flush()
    return processed[0]
