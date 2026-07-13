# Forced OpenSpec Mode Implementation Plan

## Goal
Allow operators to run Foundry in an explicit OpenSpec mode where every task uses the OpenSpec proposal/task workflow automatically. The pipeline should proceed from PLAN to IMPLEMENT without a human approval gate, while still using the existing `NEED_VERIFICATION` block only for real ambiguity or missing setup.

## Current State
- `src/foundry/config.py` loads process-wide settings from env into `Settings`; there is no OpenSpec mode flag today.
- The current OpenSpec work in `src/foundry/stages/openspec.py`, `context.py`, and `verify.py` detects artifacts, injects context, installs/runs CLI validation, and documents `INSTALL_OPENSPEC_CLI`, but it does not force the agent to follow OpenSpec workflow.
- The PLAN stage is already a persistent stage result in `stage_results`; `workflows.dev_task` continues to IMPLEMENT unless the plan text ends with `NEED_VERIFICATION`.
- Agent prompts in `src/foundry/agents/prompts/plan.md` and `implement.md` are generic and do not require OpenSpec commands or spec/task files.
- OpenCode does not consume `.codex/skills/openspec-*` as Codex skills, so Foundry must inject explicit OpenSpec instructions for non-Codex backends.

## Assumptions
- The env option will be `FOUNDRY_OPENSPEC_MODE=true` for a clear Foundry-owned namespace.
- In OpenSpec mode, target repos are expected to already contain `openspec/` or `.codex/skills/openspec-*`. If missing, Foundry should block at PLAN with a clear setup message instead of silently running the old generic workflow.
- The PLAN output in OpenSpec mode should be an OpenSpec change proposal/tasks summary, not a generic implementation plan.
- IMPLEMENT should immediately follow PLAN and should implement the OpenSpec change/tasks produced during PLAN.

## Files
- `src/foundry/config.py` — add `openspec_mode: bool` to `Settings` and parse `FOUNDRY_OPENSPEC_MODE`.
- `src/foundry/stages/openspec.py` — add helpers to render strict OpenSpec planner/implement instructions and report forced-mode setup state.
- `src/foundry/stages/context.py` — include explicit OpenSpec-mode instructions in planner context when enabled, and expose missing-artifact state clearly.
- `src/foundry/workflows.py` — block before implementation only when forced OpenSpec mode is enabled but repo setup is missing; otherwise let PLAN proceed to IMPLEMENT normally.
- `src/foundry/agents/prompts/plan.md` — add instruction: when repository context says OpenSpec mode is enabled, use the OpenSpec workflow and produce/modify OpenSpec proposal/tasks only.
- `src/foundry/agents/prompts/implement.md` — add instruction: when OpenSpec mode is enabled in the supplied plan/context, implement only the OpenSpec change/tasks and do not create a parallel generic plan.
- `.env.example` — document `FOUNDRY_OPENSPEC_MODE=true`, required target repo artifacts, and no-approval behavior.
- `README.md` — document operator workflow: enable flag, run task, Foundry creates/uses OpenSpec proposal/tasks, then implements automatically.
- `docs/ARCHITECTURE.md` — describe OpenSpec mode as an optional forced workflow from PLAN through VERIFY.
- `tests/test_config.py` — cover parsing/default for `FOUNDRY_OPENSPEC_MODE`.
- `tests/test_context_stage.py` — cover OpenSpec-mode context rendering and missing-artifact warning.
- `tests/test_workflows.py` — cover forced OpenSpec mode blocks only for missing setup and otherwise continues to IMPLEMENT.
- `tests/test_agents_base.py` — cover prompt templates include OpenSpec-mode directives.

## Implementation Steps
1. Add `openspec_mode: bool = False` to `Settings` and parse `FOUNDRY_OPENSPEC_MODE` with a local boolean parser accepting `true/false`, `1/0`, `yes/no`, and failing fast on invalid values.
2. Extend `context.run` to pass `settings.openspec_mode` into OpenSpec context. In forced mode:
   - include `forced: True` under `ctx["openspec"]`;
   - if no OpenSpec artifacts exist, include `present: False`, `forced: True`, and a blocking warning.
3. Extend `context.format_for_prompt` / `openspec.format_context` to emit a strongly worded section:
   - `OpenSpec mode is enabled by FOUNDRY_OPENSPEC_MODE=true.`
   - PLAN must use OpenSpec CLI/artifacts and create or update a change proposal/tasks.
   - PLAN must not implement product code directly.
   - IMPLEMENT will run automatically after PLAN and must follow the OpenSpec change/tasks.
4. Add a workflow helper in `workflows.py`, for example `_openspec_mode_requires_setup(ctx) -> str | None`, that returns a human-readable setup error only when `settings.openspec_mode` is true and no OpenSpec artifacts are present.
5. In `workflows.dev_task`, after CONTEXT is available and before or immediately after PLAN:
   - if forced OpenSpec setup is missing, call `_block_for_human(... blocked_stage=Stage.PLAN, reason="openspec setup required", questions=...)`;
   - otherwise keep the existing PLAN -> IMPLEMENT transition unchanged.
6. Update `agent_plan.run` or the prompt/context path only as needed; prefer keeping the prompt generic and driving mode through context, so all backends receive the same OpenSpec-mode text.
7. Add tests:
   - config parses `FOUNDRY_OPENSPEC_MODE`.
   - forced OpenSpec mode missing artifacts blocks before implementation.
   - forced OpenSpec mode with artifacts continues to IMPLEMENT without approval.
   - generic mode behavior remains unchanged.
8. Update docs and env examples with the exact operator flow and clarify there is no human approval gate.

## Verification
- `uv run pytest tests/test_config.py tests/test_context_stage.py tests/test_workflows.py tests/test_agents_base.py`
- `uv run pytest tests/test_openspec_stage.py tests/test_verify_stage.py`
- `uv run ruff check .`
- `uv run pytest`

## Risks and Edge Cases
- If the PLAN agent ignores OpenSpec instructions, the pipeline will still continue. Mitigate by making context/prompt instructions explicit and relying on `openspec validate --all --json` in VERIFY to catch invalid OpenSpec artifacts.
- If forced mode is enabled for a repo without OpenSpec artifacts, blocking at PLAN is intentional; the operator must initialize OpenSpec in the target repo first.
- PLAN may modify OpenSpec files before IMPLEMENT. This is acceptable in forced mode because the proposal/task files are part of the workflow and remain in the task worktree.
- OpenCode will not execute Codex skills; prompt instructions and OpenSpec CLI context are mandatory for OpenCode.
- Existing `NEED_VERIFICATION` behavior must remain unchanged for true ambiguity.
