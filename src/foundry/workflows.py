"""Workflow orchestration layer.

Named workflows sit between `pipeline.run_once` (fetch + batch loop) and the
stage functions. Each workflow is a plain Python helper that drives existing
stages, emits `task_events`, and persists task state — without introducing a
second state runtime alongside SQLite/events.

Defined workflows:
- `dev_task`: full issue → context → plan → implement(loop) → verify → PR cycle.
- `pr_verify`: verification-only entrypoint against an existing worktree context.

Planner outcomes (`plan_ready`, `needs_input`, `declined`, `decompose`) are
future-facing: the orchestrator only executes the allowlisted transition and
refuses to run agent-defined branches.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import structlog
from langfuse import observe

from . import security, shell, state, worktree
from .agents import AgentSettings, AgentStage
from .agents.base import AgentTask, build_fresh_prompt
from .config import Settings, task_has_label
from .events import read_events, record_event, stage_span
from foundry.forges import (
    ChangeFeedback,
    ForgeChange,
    ForgeProvider,
    provider_for,
)
from .models import Stage, Task, TaskStatus
from .stages import agent_implement as agent_implement_stage
from .stages import agent_plan as agent_plan_stage
from .stages import context as context_stage
from .stages import openspec
from .stages import issue_comment as issue_comment_stage
from .stages import pr as pr_stage
from .stages import verify as verify_stage
from .stages import ui_tests as ui_tests_stage

log = structlog.get_logger()


class WorkflowName(StrEnum):
    DEV_TASK = "dev_task"
    PR_VERIFY = "pr_verify"
    PR_FEEDBACK = "pr_feedback"


FailureKind = Literal[
    "deterministic",
    "acceptance",
    "infra",
    "unclear",
    "dangerous",
    "ui_crawler",
]


@dataclass(frozen=True)
class VerificationDecision:
    """Normalized verifier output consumed by the workflow."""

    passed: bool
    retryable: bool
    requires_human: bool
    failure_kind: FailureKind | None
    report: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepResult:
    """Outcome of a single workflow step."""

    stage: Stage
    ok: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


ALLOWED_PLANNER_OUTCOMES: frozenset[str] = frozenset(
    {"plan_ready", "needs_input", "declined", "decompose"}
)
PlannerOutcome = Literal["plan_ready", "needs_input", "declined", "decompose"]
NEED_VERIFICATION = "NEED_VERIFICATION"


def normalize_planner_outcome(outcome: str | None) -> PlannerOutcome:
    """Map a planner's proposed outcome to an allowlisted name.

    Unknown values collapse to `plan_ready` so the orchestrator never executes
    an agent-defined branch it wasn't designed for.
    """
    if outcome in ALLOWED_PLANNER_OUTCOMES:
        return outcome  # type: ignore[return-value]
    return "plan_ready"


def needs_human_input(text: str | None) -> bool:
    """Return True when the last non-empty agent output line asks for a human."""
    if not text:
        return False
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(lines) and lines[-1] == NEED_VERIFICATION


def strip_human_input_marker(text: str | None) -> str:
    """Remove the terminal NEED_VERIFICATION marker from a human-facing comment."""
    if not text:
        return ""
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == NEED_VERIFICATION:
        lines.pop()
    return "\n".join(lines).strip()


def normalize_verification(raw: dict[str, Any]) -> VerificationDecision:
    """Normalize a raw verifier dict into a typed decision.

    Conservative defaults: if the verifier reports failure with an unrecognised
    shape, treat it as `unclear` + `requires_human` so the workflow stops
    instead of looping on an output it cannot interpret.
    """
    passed = bool(raw.get("passed", False))
    report = str(raw.get("report") or raw.get("stdout") or "")
    if passed:
        return VerificationDecision(
            passed=True,
            retryable=False,
            requires_human=False,
            failure_kind=None,
            report=report,
            raw=raw,
        )
    failure_kind = raw.get("failure_kind")
    if failure_kind not in (
        "deterministic",
        "acceptance",
        "infra",
        "dangerous",
        "ui_crawler",
    ):
        failure_kind = "unclear"
    requires_human = bool(raw.get("requires_human")) or failure_kind == "unclear"
    retryable = bool(raw.get("retryable")) and not requires_human
    return VerificationDecision(
        passed=False,
        retryable=retryable,
        requires_human=requires_human,
        failure_kind=failure_kind,
        report=report,
        raw=raw,
    )


def _mark(
    settings: Settings, task: Task, *, stage: Stage, status: TaskStatus | None = None
) -> Task:
    task.current_stage = stage
    if status is not None:
        task.status = status
    return state.upsert_task(settings.db_path, task)


def _emit_synthetic_fetch_events(settings: Settings, task: Task) -> None:
    """Emit fetch stage_started/finished for issue-driven tasks.

    `fetch` runs as a batch before the workflow, so there is no stage_span
    wrapping it. Idempotent: if a stage_finished already exists, skip.
    """
    existing = read_events(settings.db_path, task_id=task.id)
    has_finished = any(
        e.stage == Stage.FETCH.value and e.kind == "stage_finished" for e in existing
    )
    if has_finished:
        return
    record_event(
        settings.db_path,
        task.id,
        Stage.FETCH.value,
        "stage_started",
        {"input": {"issue_number": task.issue_number, "repo": task.repo}},
    )
    record_event(
        settings.db_path,
        task.id,
        Stage.FETCH.value,
        "stage_finished",
        {
            "duration_ms": 0,
            "output": {
                "issue_title": task.issue_title,
                "issue_number": task.issue_number,
            },
        },
    )


def _build_attempt_input(
    plan_text: str,
    attempt: int,
    previous_summary: str = "",
    previous_report: str = "",
) -> str:
    """Augment implement input on retry with prior attempt + verifier feedback."""
    if attempt == 1:
        return plan_text
    parts = [plan_text, f"\n\n## Attempt {attempt} — previous feedback\n"]
    if previous_summary:
        parts.append(f"\n### Previous implement summary\n{previous_summary}\n")
    if previous_report:
        parts.append(f"\n### Previous verification report\n{previous_report}\n")
    return "".join(parts)


UI_PLANNING_REQUIREMENT = """
## UI crawler planning requirement

This issue opted into the post-implementation UI crawler quality gate. Read
`.codex/skills/deploy-mac-mini-json-ui/SKILL.md` in the target worktree. The
plan must specify stand URL discovery, concrete user journeys and routes,
initial state or fixtures, viewport, assertions, browser console/network
failure rules, and screenshot checkpoints. End with `NEED_VERIFICATION` if the
skill is missing or no testable route and acceptance behavior can be identified.
""".strip()


def _ui_retry_report(result: dict[str, Any]) -> str:
    parts = [str(result.get("report") or "UI crawler failed")]
    for scenario in result.get("scenarios", []):
        if isinstance(scenario, dict) and scenario.get("status") == "failed":
            detail = f"- {scenario.get('name')}: {scenario.get('error') or 'assertion failed'}"
            parts.append(detail)
    for label, key in (
        ("Core logs", "core_logs"),
        ("UI logs", "ui_logs"),
        ("Browser logs", "browser_logs"),
    ):
        value = str(result.get(key) or "")
        if value:
            parts.extend([f"\n### {label}", value])
    return "\n".join(parts)


def _build_pr_feedback_input(
    change: ForgeChange,
    feedback: str,
    *,
    openspec_context: str | None = None,
) -> str:
    lines = [
        "Address the latest change-request feedback on the existing branch.",
        "",
        f"Change request: #{change.number} {change.title}".strip(),
        f"Branch: `{change.branch}`",
        f"URL: {change.url}",
    ]
    if openspec_context:
        lines.extend(
            [
                "",
                "## OpenSpec context",
                openspec_context.strip(),
            ]
        )
    lines.extend(
        [
            "",
            "## Feedback to address",
            feedback.strip(),
        ]
    )
    lines.extend(
        [
            "",
            "Make the minimal code changes needed to satisfy this feedback. "
            "Do not open a new change request, switch branches, commit, or push.",
        ]
    )
    return "\n".join(lines)


CI_CONFIG_PATH_MARKERS: tuple[str, ...] = (
    ".gitlab-ci.yml",
    ".github/workflows/",
)


def _guard_pr_feedback_ci_config_edits(
    feedback: ChangeFeedback,
    feedback_text: str,
    worktree_path: Path,
    branch_name: str,
) -> None:
    if not feedback.failing_checks:
        return
    changed_files = _changed_files_for_pr_feedback(worktree_path, branch_name)
    ci_config_files = [
        path for path in changed_files if _is_ci_config_path(path)
    ]
    if not ci_config_files:
        return
    if _feedback_allows_ci_config_edit(feedback_text):
        return
    raise RuntimeError(
        "refusing PR feedback fix: CI/CD feedback must fix the failing build "
        "inputs, tests, or product code, not CI configuration files. Changed "
        "CI/CD config files: " + ", ".join(ci_config_files)
    )


def _changed_files_for_pr_feedback(
    worktree_path: Path,
    branch_name: str,
) -> list[str]:
    paths: set[str] = set()
    remote_ref = f"origin/{branch_name}"
    commands = (
        ["git", "diff", "--name-only", remote_ref, "HEAD"],
        ["git", "diff", "--name-only"],
        ["git", "diff", "--name-only", "--cached"],
    )
    for cmd in commands:
        result = shell.run(cmd, cwd=worktree_path, check=False)
        if not result.ok:
            continue
        paths.update(line.strip() for line in result.stdout.splitlines() if line.strip())
    return sorted(paths)


def _is_ci_config_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return any(
        normalized == marker or normalized.startswith(marker)
        for marker in CI_CONFIG_PATH_MARKERS
    )


def _feedback_allows_ci_config_edit(feedback_text: str) -> bool:
    normalized = feedback_text.replace("\\", "/")
    return any(marker in normalized for marker in CI_CONFIG_PATH_MARKERS)


def _task_for_change(settings: Settings, change: ForgeChange) -> Task | None:
    for task in state.list_tasks(settings.db_path):
        if task.pr_url and task.pr_url == change.url:
            return task
        if task.pr_url and task.branch_name == change.branch:
            return task
    prefix = "foundry/task-"
    if change.branch.startswith(prefix):
        try:
            task = state.get_task(
                settings.db_path, int(change.branch.removeprefix(prefix))
            )
        except ValueError:
            return None
        if task is not None and task.pr_url:
            return task
    return None


def _prepare_pr_feedback_worktree(
    settings: Settings,
    task: Task,
    branch_name: str,
    provider: ForgeProvider | None = None,
) -> tuple[Path, Path]:
    active_provider = provider or provider_for(settings)
    base = worktree.ensure_base_repo(
        settings.worktree_root,
        settings.target_repo,
        settings.base_branch,
        active_provider,
    )
    wt_path = (settings.worktree_root / f"task-{task.id}-pr-feedback").resolve()
    if wt_path.exists():
        worktree.cleanup_worktree(base, wt_path)
    shell.run(["git", "branch", "-D", branch_name], cwd=base, check=False)
    shell.run(["git", "fetch", "origin", branch_name], cwd=base)
    shell.run(
        [
            "git",
            "worktree",
            "add",
            str(wt_path),
            "-B",
            branch_name,
            f"origin/{branch_name}",
        ],
        cwd=base,
    )
    return base, wt_path


def _block_for_human(
    settings: Settings,
    task: Task,
    *,
    blocked_stage: Stage,
    reason: str,
    questions: str,
    worktree_path: Path | None,
    provider: ForgeProvider | None = None,
) -> Task:
    comment = "\n".join(
        part
        for part in [
            "The Foundry needs human input before continuing this task.",
            "",
            f"Blocked at stage: `{blocked_stage.value}`.",
            "",
            questions.strip() or reason,
        ]
        if part
    )
    with stage_span(
        settings.db_path,
        task.id,
        Stage.ISSUE_COMMENT.value,
        input={"blocked_stage": blocked_stage.value},
    ) as finish:
        result = issue_comment_stage.run(
            task, settings, comment, cwd=worktree_path, provider=provider
        )
        finish(output={"issue_number": task.issue_number})
    state.append_log(
        settings.db_path,
        task.id,
        Stage.ISSUE_COMMENT,
        {"blocked_stage": blocked_stage.value, "reason": reason, **result},
    )
    task.current_stage = blocked_stage
    task.status = TaskStatus.BLOCKED
    return state.upsert_task(settings.db_path, task)


def _openspec_mode_requires_setup(settings: Settings, ctx: dict[str, Any]) -> str | None:
    if not settings.openspec_mode:
        return None
    openspec_ctx = ctx.get("openspec")
    if isinstance(openspec_ctx, dict) and openspec_ctx.get("present"):
        return None
    return (
        "FOUNDRY_OPENSPEC_MODE=true is enabled, but the target worktree does not "
        "contain OpenSpec artifacts. Initialize OpenSpec in the target repository "
        "first, then resume this task."
    )


def _prepare_dev_worktree(settings: Settings, task: Task, base: Path) -> tuple[Task, Path, str]:
    """Return a usable task worktree, repairing stale persisted paths.

    Task rows can outlive the process that created them. In Docker, an older
    host path such as `/Users/.../worktrees/task-1` is not valid inside the
    container even though the same volume is mounted at `/app/worktrees`.
    Prefer the canonical path under the current `WORKTREE_ROOT` before creating
    a fresh worktree.
    """
    if task.id is None:
        raise RuntimeError("task must be persisted before preparing a worktree")

    if task.worktree_path and task.branch_name:
        stored_path = Path(task.worktree_path)
        if stored_path.exists():
            return task, stored_path, task.branch_name

        canonical_path = (settings.worktree_root / f"task-{task.id}").resolve()
        if canonical_path.exists():
            task.worktree_path = str(canonical_path)
            task = state.upsert_task(settings.db_path, task)
            return task, canonical_path, task.branch_name

        log.warning(
            "workflow.dev_task.stale_worktree_path",
            task_id=task.id,
            stored_path=str(stored_path),
            expected_path=str(canonical_path),
        )

    wt_path, branch_name = worktree.create_worktree(
        settings.worktree_root,
        task.id,
        settings.base_branch,
    )
    task.worktree_path = str(wt_path)
    task.branch_name = branch_name
    task = state.upsert_task(settings.db_path, task)
    return task, wt_path, branch_name


@observe(name="workflow.dev_task")
def dev_task(
    settings: Settings, task: Task, provider: ForgeProvider | None = None
) -> Task:
    """Issue-driven development workflow.

    Flow: context → plan → (implement → verify) × up to N attempts → pr.
    The orchestrator — not the agent or verifier — picks transitions on each
    verification result. Terminal failures (requires_human, non-retryable,
    exhausted budget) propagate as exceptions; `pipeline.run_once` translates
    them into task status per `PRE_IMPLEMENT_STAGES` policy.
    """
    log.info("workflow.dev_task.start", task_id=task.id, issue=task.issue_number)
    active_provider = provider or provider_for(settings)

    if task.pr_url:
        log.info("task.skip_already_has_pr", task_id=task.id, pr_url=task.pr_url)
        return task

    if task.id is None:
        task = state.upsert_task(settings.db_path, task)

    task.attempts += 1
    task.status = TaskStatus.RUNNING
    task = state.upsert_task(settings.db_path, task)

    _emit_synthetic_fetch_events(settings, task)

    base = worktree.ensure_base_repo(
        settings.worktree_root,
        settings.target_repo,
        settings.base_branch,
        active_provider,
    )
    task, wt_path, branch_name = _prepare_dev_worktree(settings, task, base)

    # CONTEXT
    ctx = state.get_stage_result(settings.db_path, task.id, Stage.CONTEXT)
    if ctx is None:
        task = _mark(settings, task, stage=Stage.CONTEXT)
        with stage_span(settings.db_path, task.id, Stage.CONTEXT.value) as finish:
            ctx = context_stage.run(task, settings, repo_path=wt_path)
            finish(output=ctx)
        state.save_stage_result(settings.db_path, task.id, Stage.CONTEXT, ctx)
        state.append_log(settings.db_path, task.id, Stage.CONTEXT, {"ok": True})

    openspec_setup_error = _openspec_mode_requires_setup(settings, ctx)
    if openspec_setup_error:
        task = _mark(settings, task, stage=Stage.PLAN)
        return _block_for_human(
            settings,
            task,
            blocked_stage=Stage.PLAN,
            reason="openspec setup required",
            questions=openspec_setup_error,
            worktree_path=wt_path,
            provider=active_provider,
        )

    # PLAN
    plan = state.get_stage_result(settings.db_path, task.id, Stage.PLAN)
    if plan is None:
        task = _mark(settings, task, stage=Stage.PLAN)
        plan_agent_settings = AgentSettings.from_env(
            AgentStage.PLAN, db_path=settings.db_path
        )
        if settings.openspec_mode:
            plan_agent_settings = replace(
                plan_agent_settings,
                prompt_template="plan_openspec",
            )
        plan_agent_task = AgentTask(
            id=task.id or task.issue_number,
            title=task.issue_title,
            description=task.issue_body,
        )
        plan_input = context_stage.format_for_prompt(ctx)
        ui_tests_enabled = task_has_label(task, settings.ui_test_label)
        if ui_tests_enabled:
            plan_input = f"{plan_input}\n\n{UI_PLANNING_REQUIREMENT}"
        plan_prompt = build_fresh_prompt(
            AgentStage.PLAN,
            plan_agent_task,
            plan_input,
            template_name=plan_agent_settings.prompt_template,
        )
        with stage_span(
            settings.db_path,
            task.id,
            Stage.PLAN.value,
            input={
                "title": task.issue_title,
                "prompt": plan_prompt,
                "ui_tests_enabled": ui_tests_enabled,
            },
            agent={"name": plan_agent_settings.backend, "model": plan_agent_settings.model},
        ) as finish:
            plan = agent_plan_stage.run(
                task,
                ctx,
                wt_path,
                settings,
                planner_input=plan_input,
            )
            finish(
                output={"summary": plan.get("summary", ""), "text": plan.get("plan", "")},
                cost_usd=plan.get("cost_usd"),
                tokens_in=plan.get("tokens_in"),
                tokens_out=plan.get("tokens_out"),
            )
        state.save_stage_result(settings.db_path, task.id, Stage.PLAN, plan)
        state.append_log(
            settings.db_path, task.id, Stage.PLAN, {"summary": plan.get("summary", "")}
        )
    plan_text = plan.get("plan", "")
    if needs_human_input(plan_text):
        return _block_for_human(
            settings,
            task,
            blocked_stage=Stage.PLAN,
            reason="plan requested human verification",
            questions=strip_human_input_marker(plan_text),
            worktree_path=wt_path,
            provider=active_provider,
        )

    # IMPLEMENT → VERIFY quality-gate loop
    max_attempts = max(1, settings.max_implement_attempts)
    latest_impl = state.get_latest_stage_result(settings.db_path, task.id, Stage.IMPLEMENT)
    latest_verify = state.get_latest_stage_result(settings.db_path, task.id, Stage.VERIFY)
    impl_result: dict[str, Any] = latest_impl[1] if latest_impl else {}
    decision = VerificationDecision(
        passed=False,
        retryable=False,
        requires_human=False,
        failure_kind=None,
        report="",
        raw={},
    )
    if latest_verify:
        decision = normalize_verification(latest_verify[1])
    for attempt in range(1, max_attempts + 1):
        attempt_plan = dict(plan)
        attempt_plan["plan"] = _build_attempt_input(
            plan_text,
            attempt,
            previous_summary=impl_result.get("result", "") if attempt > 1 else "",
            previous_report=decision.report if attempt > 1 else "",
        )
        if attempt > 1:
            attempt_plan["_previous_implement_summary"] = impl_result.get("result", "")
            attempt_plan["_previous_verification_report"] = decision.report

        saved_impl = state.get_stage_result(
            settings.db_path, task.id, Stage.IMPLEMENT, attempt=attempt
        )
        if saved_impl is None:
            checkpoint_path = security.checkpoint_diff(
                worktree_path=wt_path,
                checkpoint_root=settings.db_path.parent / "checkpoints",
                task_id=task.id or task.issue_number,
                attempt=attempt,
            )
            state.append_log(
                settings.db_path,
                task.id,
                Stage.IMPLEMENT,
                {"attempt": attempt, "checkpoint": str(checkpoint_path)},
            )
            if attempt > 1:
                security.reset_task_worktree(wt_path, settings.worktree_root)

            task = _mark(settings, task, stage=Stage.IMPLEMENT)
            impl_agent_settings = AgentSettings.from_env(
                AgentStage.IMPLEMENT, db_path=settings.db_path
            )
            impl_input = agent_implement_stage.build_agent_input(
                attempt_plan,
                wt_path,
                settings,
            )
            if settings.openspec_mode:
                impl_agent_settings = replace(
                    impl_agent_settings,
                    prompt_template="implement_openspec",
                )
            impl_agent_task = AgentTask(
                id=task.id or task.issue_number,
                title=task.issue_title,
                description=task.issue_body,
            )
            impl_prompt = build_fresh_prompt(
                AgentStage.IMPLEMENT,
                impl_agent_task,
                impl_input,
                template_name=impl_agent_settings.prompt_template,
            )
            with stage_span(
                settings.db_path,
                task.id,
                Stage.IMPLEMENT.value,
                input={
                    "title": task.issue_title,
                    "prompt": impl_prompt,
                    "attempt": attempt,
                },
                agent={
                    "name": impl_agent_settings.backend,
                    "model": impl_agent_settings.model,
                },
            ) as finish:
                impl_result = agent_implement_stage.run(
                    task, attempt_plan, wt_path, settings
                )
                finish(
                    output={
                        "summary": impl_result.get("result", ""),
                        "text": impl_result.get("response", ""),
                        "attempt": attempt,
                    },
                    cost_usd=impl_result.get("cost_usd"),
                    tokens_in=impl_result.get("tokens_in"),
                    tokens_out=impl_result.get("tokens_out"),
                )
            state.save_stage_result(
                settings.db_path, task.id, Stage.IMPLEMENT, impl_result, attempt=attempt
            )
            state.append_log(
                settings.db_path,
                task.id,
                Stage.IMPLEMENT,
                {**impl_result, "attempt": attempt},
            )
        else:
            impl_result = saved_impl
        if needs_human_input(impl_result.get("response") or impl_result.get("result")):
            return _block_for_human(
                settings,
                task,
                blocked_stage=Stage.IMPLEMENT,
                reason=f"implement requested human verification on attempt {attempt}",
                questions=strip_human_input_marker(
                    impl_result.get("response") or impl_result.get("result")
                ),
                worktree_path=wt_path,
                provider=active_provider,
            )

        # VERIFY
        saved_verify = state.get_stage_result(
            settings.db_path, task.id, Stage.VERIFY, attempt=attempt
        )
        if saved_verify is None:
            task = _mark(settings, task, stage=Stage.VERIFY)
            with stage_span(
                settings.db_path,
                task.id,
                Stage.VERIFY.value,
                input={"attempt": attempt},
            ) as finish:
                verify_raw = verify_stage.run(
                    task, wt_path, settings, impl_result=impl_result
                )
                decision = normalize_verification(verify_raw)
                finish(
                    output={
                        "passed": decision.passed,
                        "retryable": decision.retryable,
                        "requires_human": decision.requires_human,
                        "failure_kind": decision.failure_kind,
                        "report": decision.report,
                        "attempt": attempt,
                    }
                )
            state.save_stage_result(
                settings.db_path, task.id, Stage.VERIFY, verify_raw, attempt=attempt
            )
            state.append_log(
                settings.db_path,
                task.id,
                Stage.VERIFY,
                {
                    "attempt": attempt,
                    "passed": decision.passed,
                    "retryable": decision.retryable,
                    "requires_human": decision.requires_human,
                    "failure_kind": decision.failure_kind,
                },
            )
        else:
            decision = normalize_verification(saved_verify)

        if decision.passed and task_has_label(task, settings.ui_test_label):
            saved_ui_tests = state.get_stage_result(
                settings.db_path, task.id, Stage.UI_TESTS, attempt=attempt
            )
            if saved_ui_tests is None:
                task = _mark(settings, task, stage=Stage.UI_TESTS)
                ui_agent_settings = AgentSettings.from_env(
                    AgentStage.UI_TESTS, db_path=settings.db_path
                )
                with stage_span(
                    settings.db_path,
                    task.id,
                    Stage.UI_TESTS.value,
                    input={
                        "attempt": attempt,
                        "plan_summary": str(plan.get("summary") or "")[:1000],
                        "skill_path": ui_tests_stage.DEPLOY_SKILL.as_posix(),
                        "artifact_policy": {
                            "max_files": settings.ui_test_artifact_max_files,
                            "max_file_bytes": settings.ui_test_artifact_max_file_bytes,
                            "max_attempt_bytes": settings.ui_test_artifact_max_attempt_bytes,
                        },
                    },
                    agent={
                        "name": ui_agent_settings.backend,
                        "model": ui_agent_settings.model,
                    },
                ) as finish:
                    saved_ui_tests = ui_tests_stage.run(
                        task,
                        wt_path,
                        settings,
                        plan_text=plan_text,
                        attempt=attempt,
                    )
                    finish(
                        output=saved_ui_tests,
                        cost_usd=saved_ui_tests.get("cost_usd"),
                        tokens_in=saved_ui_tests.get("tokens_in"),
                        tokens_out=saved_ui_tests.get("tokens_out"),
                    )
                state.save_stage_result(
                    settings.db_path,
                    task.id,
                    Stage.UI_TESTS,
                    saved_ui_tests,
                    attempt=attempt,
                )
                state.append_log(
                    settings.db_path,
                    task.id,
                    Stage.UI_TESTS,
                    {
                        "attempt": attempt,
                        "passed": saved_ui_tests.get("passed", False),
                        "failure_kind": saved_ui_tests.get("failure_kind"),
                    },
                )
            decision = normalize_verification(saved_ui_tests)
            if not decision.passed and decision.failure_kind == "ui_crawler":
                decision = replace(decision, report=_ui_retry_report(saved_ui_tests))

        if decision.passed:
            break
        if decision.requires_human:
            return _block_for_human(
                settings,
                task,
                blocked_stage=task.current_stage,
                reason=(
                    f"quality gate requires human intervention (attempt {attempt}, "
                    f"kind={decision.failure_kind})"
                ),
                questions=decision.report,
                worktree_path=wt_path,
                provider=active_provider,
            )
        if not decision.retryable:
            raise RuntimeError(
                f"quality gate failed non-retryably (attempt {attempt}, "
                f"kind={decision.failure_kind}): {decision.report}"
            )
        if attempt >= max_attempts:
            raise RuntimeError(
                f"quality gate failed after {attempt} attempts "
                f"(kind={decision.failure_kind}): {decision.report}"
            )
        # Otherwise: retryable failure with remaining budget — loop.

    # PR
    pr_result = state.get_stage_result(settings.db_path, task.id, Stage.PR)
    if pr_result is None:
        task = _mark(settings, task, stage=Stage.PR)
        with stage_span(settings.db_path, task.id, Stage.PR.value) as finish:
            pr_result = pr_stage.run(
                task,
                wt_path,
                branch_name,
                settings,
                report=decision.report,
                provider=active_provider,
            )
            task.pr_url = pr_result["pr_url"]
            finish(output={"pr_url": pr_result["pr_url"]})
        state.save_stage_result(settings.db_path, task.id, Stage.PR, pr_result)
        state.append_log(settings.db_path, task.id, Stage.PR, pr_result)
        _save_successful_pr_memory(settings, task, pr_result, ctx)
    else:
        task.pr_url = pr_result.get("pr_url")

    task = _mark(settings, task, stage=Stage.DONE, status=TaskStatus.DONE)
    worktree.cleanup_worktree(base, wt_path)
    log.info("workflow.dev_task.done", task_id=task.id, pr_url=task.pr_url)
    return task


def _save_successful_pr_memory(
    settings: Settings,
    task: Task,
    pr_result: dict[str, Any],
    ctx: dict[str, Any],
) -> None:
    if task.id is None:
        return

    touched_files = pr_result.get("touched_files") or ctx.get("files") or []
    if touched_files:
        state.save_repo_memory(
            settings.db_path, task.repo, "touched_files", touched_files
        )

    verify_commands = ctx.get("test_commands") or []
    if verify_commands:
        state.save_repo_memory(
            settings.db_path, task.repo, "verify_commands", verify_commands
        )

    common_failures = _common_verify_failures(settings.db_path, task.id)
    if common_failures:
        state.save_repo_memory(
            settings.db_path, task.repo, "common_failures", common_failures
        )


def _common_verify_failures(db_path: Path, task_id: int) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for attempt, output in state.list_stage_results(db_path, task_id, Stage.VERIFY):
        if output.get("passed") is True:
            continue
        report = str(output.get("report") or "").strip()
        failure_kind = str(output.get("failure_kind") or "unknown")
        if not report and output.get("stdout"):
            report = str(output["stdout"]).strip()
        if report:
            failures.append(
                {
                    "attempt": str(attempt),
                    "failure_kind": failure_kind,
                    "report": report[:1000],
                }
            )
    return failures[-5:]


@observe(name="workflow.pr_verify")
def pr_verify(
    settings: Settings,
    task: Task,
    worktree_path: Path,
    impl_result: dict[str, Any] | None = None,
) -> VerificationDecision:
    """PR-facing verification workflow.

    Runs the verify stage against a pre-existing worktree/task context and
    returns the normalized decision. Does NOT commit, push, open a PR, close the
    source issue, or mark the task `DONE` — those are `dev_task`'s job.
    """
    log.info("workflow.pr_verify.start", task_id=task.id)
    task = _mark(settings, task, stage=Stage.VERIFY)
    with stage_span(
        settings.db_path,
        task.id,
        Stage.VERIFY.value,
        input={"workflow": WorkflowName.PR_VERIFY.value},
    ) as finish:
        verify_raw = verify_stage.run(
            task, worktree_path, settings, impl_result=impl_result
        )
        decision = normalize_verification(verify_raw)
        finish(
            output={
                "passed": decision.passed,
                "retryable": decision.retryable,
                "requires_human": decision.requires_human,
                "failure_kind": decision.failure_kind,
                "report": decision.report,
                "workflow": WorkflowName.PR_VERIFY.value,
            }
        )
    state.append_log(
        settings.db_path,
        task.id,
        Stage.VERIFY,
        {
            "workflow": WorkflowName.PR_VERIFY.value,
            "passed": decision.passed,
            "report": decision.report,
        },
    )
    log.info(
        "workflow.pr_verify.done", task_id=task.id, passed=decision.passed
    )
    return decision


@observe(name="workflow.pr_feedback")
def pr_feedback(
    settings: Settings,
    task: Task,
    change: ForgeChange,
    feedback: ChangeFeedback,
    provider: ForgeProvider | None = None,
) -> Task:
    """Apply requested PR feedback on the existing PR branch.

    This is intentionally small: it records the external feedback, runs the
    implement agent with that feedback, verifies the result, pushes one commit
    back to the same branch, and posts a PR comment.
    """
    active_provider = provider or provider_for(settings)
    branch_name = change.branch or task.branch_name or ""
    if not branch_name:
        raise RuntimeError(f"change request #{change.number} has no head branch")
    feedback_text = feedback.format()

    log.info(
        "workflow.pr_feedback.start",
        task_id=task.id,
        change_number=change.number,
        branch=branch_name,
    )
    task.branch_name = branch_name
    task.pr_url = change.url or task.pr_url
    task.status = TaskStatus.PENDING
    task.current_stage = Stage.IMPLEMENT
    task = state.upsert_task(settings.db_path, task)
    record_event(
        settings.db_path,
        task.id,
        Stage.IMPLEMENT.value,
        "pr_feedback",
        {
            "workflow": WorkflowName.PR_FEEDBACK.value,
            "status": TaskStatus.PENDING.value,
            "stage": Stage.IMPLEMENT.value,
            "forge": active_provider.kind.value,
            "change_number": change.number,
            "change_url": change.url,
            "branch": branch_name,
            "feedback": feedback_text,
            "feedback_fingerprint": feedback.fingerprint,
        },
    )
    state.append_log(
        settings.db_path,
        task.id,
        Stage.IMPLEMENT,
        {
            "workflow": WorkflowName.PR_FEEDBACK.value,
            "change_number": change.number,
            "branch": branch_name,
            "feedback_fingerprint": feedback.fingerprint,
        },
    )

    base, wt_path = _prepare_pr_feedback_worktree(
        settings, task, branch_name, active_provider
    )
    task.worktree_path = str(wt_path)
    task.status = TaskStatus.RUNNING
    task = state.upsert_task(settings.db_path, task)

    try:
        openspec_context = (
            openspec.build_implementation_handoff(
                wt_path,
                timeout_sec=settings.verify_command_timeout_sec,
                include_skill_bodies=False,
            )
            if settings.openspec_mode
            else None
        )
        implement_input = _build_pr_feedback_input(
            change,
            feedback_text,
            openspec_context=openspec_context,
        )
        impl_agent_settings = AgentSettings.from_env(
            AgentStage.IMPLEMENT, db_path=settings.db_path
        )
        impl_agent_settings = replace(
            impl_agent_settings,
            prompt_template="pr_feedback",
        )
        impl_agent_task = AgentTask(
            id=task.id or task.issue_number,
            title=task.issue_title,
            description=task.issue_body,
        )
        impl_prompt = build_fresh_prompt(
            AgentStage.IMPLEMENT,
            impl_agent_task,
            implement_input,
            template_name=impl_agent_settings.prompt_template,
        )
        with stage_span(
            settings.db_path,
            task.id,
            Stage.IMPLEMENT.value,
            input={
                "workflow": WorkflowName.PR_FEEDBACK.value,
                "change_number": change.number,
                "prompt": impl_prompt,
            },
            agent={
                "name": impl_agent_settings.backend,
                "model": impl_agent_settings.model,
            },
        ) as finish:
            impl_result = agent_implement_stage.run(
                task,
                {"plan": implement_input, "_prompt_template": "pr_feedback"},
                wt_path,
                settings,
            )
            finish(
                output={
                    "summary": impl_result.get("result", ""),
                    "text": impl_result.get("response", ""),
                },
                cost_usd=impl_result.get("cost_usd"),
                tokens_in=impl_result.get("tokens_in"),
                tokens_out=impl_result.get("tokens_out"),
            )
        state.append_log(
            settings.db_path,
            task.id,
            Stage.IMPLEMENT,
            {**impl_result, "workflow": WorkflowName.PR_FEEDBACK.value},
        )

        if needs_human_input(impl_result.get("response") or impl_result.get("result")):
            return _block_for_human(
                settings,
                task,
                blocked_stage=Stage.IMPLEMENT,
                reason="implement requested human verification while addressing PR feedback",
                questions=strip_human_input_marker(
                    impl_result.get("response") or impl_result.get("result")
                ),
                worktree_path=wt_path,
                provider=active_provider,
            )

        task = _mark(settings, task, stage=Stage.VERIFY)
        with stage_span(
            settings.db_path,
            task.id,
            Stage.VERIFY.value,
            input={
                "workflow": WorkflowName.PR_FEEDBACK.value,
                "change_number": change.number,
            },
        ) as finish:
            verify_raw = verify_stage.run(
                task, wt_path, settings, impl_result=impl_result
            )
            decision = normalize_verification(verify_raw)
            finish(
                output={
                    "passed": decision.passed,
                    "retryable": decision.retryable,
                    "requires_human": decision.requires_human,
                    "failure_kind": decision.failure_kind,
                    "report": decision.report,
                    "workflow": WorkflowName.PR_FEEDBACK.value,
                }
            )
        state.append_log(
            settings.db_path,
            task.id,
            Stage.VERIFY,
            {
                "workflow": WorkflowName.PR_FEEDBACK.value,
                "passed": decision.passed,
                "report": decision.report,
            },
        )
        if not decision.passed:
            if decision.requires_human:
                return _block_for_human(
                    settings,
                    task,
                    blocked_stage=Stage.VERIFY,
                    reason="verify requires human intervention after PR feedback fix",
                    questions=decision.report,
                    worktree_path=wt_path,
                    provider=active_provider,
                )
            raise RuntimeError(
                "PR feedback fix did not pass verification: " + decision.report
            )

        _guard_pr_feedback_ci_config_edits(
            feedback,
            feedback_text,
            wt_path,
            branch_name,
        )
        task = _mark(settings, task, stage=Stage.PR)
        commit_message = f"foundry: address PR feedback for task #{task.issue_number}"
        with stage_span(
            settings.db_path,
            task.id,
            Stage.PR.value,
            input={
                "workflow": WorkflowName.PR_FEEDBACK.value,
                "change_number": change.number,
            },
        ) as finish:
            push_result = pr_stage.commit_and_push_changes(
                task,
                wt_path,
                branch_name,
                commit_message,
                allow_no_changes=True,
            )
            if push_result.get("pushed", True):
                summary = (
                    "The Foundry pushed a follow-up commit addressing the latest "
                    "PR feedback."
                )
            else:
                summary = (
                    "The Foundry validated the latest PR feedback and no follow-up "
                    "commit was needed."
                )
            comment = "\n".join(
                [
                    summary,
                    "",
                    "## Verification",
                    decision.report.strip() or "Verification passed.",
                ]
            )
            active_provider.comment_change(
                settings.target_repo, change.number, comment
            )
            finish(output={**push_result, "commented": True})
        state.append_log(
            settings.db_path,
            task.id,
            Stage.PR,
            {
                "workflow": WorkflowName.PR_FEEDBACK.value,
                "branch": branch_name,
                "commented": True,
            },
        )

        task = _mark(settings, task, stage=Stage.DONE, status=TaskStatus.DONE)
        log.info(
            "workflow.pr_feedback.done",
            task_id=task.id,
            change_number=change.number,
        )
        return task
    finally:
        worktree.cleanup_worktree(base, wt_path)


def pr_feedback_once(
    settings: Settings, provider: ForgeProvider | None = None
) -> list[Task]:
    """Run one PR feedback pass for open `foundry/task-*` PRs.

    Deduplication: the feedback string is hashed and stored in repo_memory as
    ``pr_feedback_hash:{task_id}``. If the hash matches the last processed run,
    the PR is skipped — avoids re-applying the same review comments on every
    polling cycle.
    """
    state.init_db(settings.db_path)
    active_provider = provider or provider_for(settings)
    processed: list[Task] = []
    for change in active_provider.list_changes(settings.target_repo):
        feedback = active_provider.load_feedback(settings.target_repo, change)
        if not feedback.actionable:
            continue
        task = _task_for_change(settings, change)
        if task is None:
            log.warning(
                "workflow.pr_feedback.no_task",
                change_number=change.number,
                branch=change.branch,
            )
            continue

        # Skip if this exact feedback was already processed
        hash_key = f"pr_feedback_hash:{task.id}"
        current_hash = feedback.fingerprint
        stored = state.get_repo_memory(settings.db_path, settings.target_repo, hash_key)
        if stored == current_hash:
            log.info(
                "workflow.pr_feedback.skip_unchanged",
                task_id=task.id,
                change_number=change.number,
            )
            continue

        try:
            result = pr_feedback(settings, task, change, feedback, active_provider)
        except Exception:
            failed_task = state.get_task(settings.db_path, task.id) or task
            failed_task.status = TaskStatus.FAILED
            failed_task.current_stage = Stage.FAILED
            state.upsert_task(settings.db_path, failed_task)
            raise
        else:
            state.save_repo_memory(
                settings.db_path, settings.target_repo, hash_key, current_hash
            )
            processed.append(result)
    return processed
