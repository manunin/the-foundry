from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from langfuse import observe

from ..agents import AgentSettings, AgentStage, AgentTask, make_agent
from .. import security
from ..config import Settings
from ..models import Task
from . import openspec
from .context import format_for_prompt


@observe(name="stage.plan")
def run(
    task: Task,
    ctx: dict,
    worktree_path: Path,
    settings: Settings,
    *,
    planner_input: str | None = None,
) -> dict:
    """Agent-backed plan stage: delegates to the configured plan_agent.

    Returns {"plan": <full agent response>, "summary": <first line>}.
    """
    agent_settings = AgentSettings.from_env(AgentStage.PLAN, db_path=settings.db_path)
    if settings.openspec_mode:
        agent_settings = replace(agent_settings, prompt_template="plan_openspec")
    agent = make_agent(agent_settings)
    agent_task = AgentTask(
        id=task.id or task.issue_number,
        title=task.issue_title,
        description=task.issue_body,
    )
    with security.preserve_git_origin(worktree_path):
        r = agent.apply(
            task=agent_task,
            worktree=worktree_path,
            input=planner_input if planner_input is not None else format_for_prompt(ctx),
        )
    result = {
        "agent": agent.name,
        "stage": r.stage.value,
        "plan": r.response,
        "summary": r.result,
        "cost_usd": r.cost_usd,
        "tokens_in": r.tokens_in,
        "tokens_out": r.tokens_out,
    }
    if settings.openspec_mode:
        result["openspec_artifacts"] = openspec.changed_artifact_paths(worktree_path)
    return result
