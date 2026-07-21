from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from langfuse import observe

from .. import security, shell
from ..agents import AgentSettings, AgentStage, AgentTask, make_agent
from ..config import Settings
from ..models import Task
from . import openspec


@observe(name="stage.implement")
def run(task: Task, plan: dict, worktree_path: Path, settings: Settings) -> dict:
    """Agent-backed implement stage: delegates to the configured implement_agent.

    Same signature as `stages.implement.run`. The plan dict may come from
    `agent_plan` (key `plan` with full text) or from the old stub (key
    `steps`); both are handled.
    """
    agent_settings = AgentSettings.from_env(
        AgentStage.IMPLEMENT,
        db_path=settings.db_path,
    )
    prompt_template = plan.get("_prompt_template")
    if prompt_template:
        agent_settings = replace(agent_settings, prompt_template=str(prompt_template))
    elif settings.openspec_mode:
        agent_settings = replace(agent_settings, prompt_template="implement_openspec")
    agent = make_agent(agent_settings)
    agent_task = AgentTask(
        id=task.id or task.issue_number,
        title=task.issue_title,
        description=task.issue_body,
    )
    plan_text = build_agent_input(plan, worktree_path, settings)
    try:
        with security.preserve_git_origin(worktree_path):
            r = agent.apply(task=agent_task, worktree=worktree_path, input=plan_text)
    except security.GitRemoteMutationError:
        raise
    except Exception as exc:
        recovered = _recover_agent_contract_violation(
            agent_name=agent.name,
            worktree_path=worktree_path,
            task=task,
            error=exc,
        )
        if recovered is None:
            raise
        return recovered
    return {
        "agent": agent.name,
        "stage": r.stage.value,
        "result": r.result,
        "response": r.response,
        "cost_usd": r.cost_usd,
        "tokens_in": r.tokens_in,
        "tokens_out": r.tokens_out,
    }


def build_agent_input(plan: dict, worktree_path: Path, settings: Settings) -> str:
    prompt_template = plan.get("_prompt_template")
    if settings.openspec_mode and not prompt_template:
        handoff = openspec.build_implementation_handoff(
            worktree_path,
            timeout_sec=settings.verify_command_timeout_sec,
            plan_artifacts=[str(path) for path in plan.get("openspec_artifacts", [])],
        )
        retry_feedback = _format_retry_feedback(plan)
        pr_feedback = str(plan.get("_pr_feedback") or "").strip()
        if pr_feedback:
            handoff = f"{handoff}\n\n## PR feedback to address\n{pr_feedback}"
        if retry_feedback:
            return f"{handoff}\n\n{retry_feedback}"
        return handoff
    return plan.get("plan") or ""


def _format_retry_feedback(plan: dict) -> str:
    previous_summary = str(plan.get("_previous_implement_summary") or "").strip()
    previous_report = str(plan.get("_previous_verification_report") or "").strip()
    if not previous_summary and not previous_report:
        return ""

    parts = ["## Previous attempt feedback"]
    if previous_summary:
        parts.extend(["", "### Previous implement summary", previous_summary])
    if previous_report:
        parts.extend(["", "### Previous verification report", previous_report])
    return "\n".join(parts)


def _recover_agent_contract_violation(
    *,
    agent_name: str,
    worktree_path: Path,
    task: Task,
    error: Exception,
) -> dict | None:
    status = shell.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        check=False,
    )
    changed_files = [
        _porcelain_path(line)
        for line in status.stdout.splitlines()
        if line.strip()
    ]
    committed_files = _committed_files_ahead_of_remote(worktree_path, task)
    if not changed_files and not committed_files:
        return None

    local_work = sorted(set(changed_files + committed_files))
    response = (
        "Agent process failed after producing local work. "
        "Foundry preserved the worktree for verification and PR orchestration.\n\n"
        f"Agent error: {type(error).__name__}: {error}\n"
        "Local files:\n"
        + "\n".join(f"- {path}" for path in local_work)
    )
    return {
        "agent": agent_name,
        "stage": AgentStage.IMPLEMENT.value,
        "result": "recovered local work after agent failure",
        "response": response,
        "cost_usd": None,
        "tokens_in": None,
        "tokens_out": None,
        "recovered_after_agent_error": True,
        "local_files": local_work,
    }


def _committed_files_ahead_of_remote(worktree_path: Path, task: Task) -> list[str]:
    branch_name = task.branch_name
    if not branch_name:
        return []
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
        check=False,
    )
    if not diff.ok:
        return []
    return [line.strip() for line in diff.stdout.splitlines() if line.strip()]


def _porcelain_path(line: str) -> str:
    path = line[3:] if len(line) > 3 else line
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip()
