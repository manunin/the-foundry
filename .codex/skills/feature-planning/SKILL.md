---
name: feature-planning
description: Create repository-specific implementation plans for The Foundry and save them under `.codex/plans`. Use when the user asks to plan, design, scope, or break down a feature before implementation.
---

# Feature Planning

Create a concrete implementation plan based on the current worktree. Do not edit product code while planning unless the user explicitly requests implementation too.

## Workflow

1. Read `AGENTS.md` and run `git status --short --branch`.
2. Read `docs/ARCHITECTURE.md`. Read a matching file under `docs/specs/` when the feature affects a documented contract; for observability, events, the API, or the live UI, read `docs/specs/observability-ui.md`.
3. Inspect the relevant implementation and tests with `rg --files`, `rg`, and targeted file reads. Trace the full contract across:
   - `src/foundry/` for the FSM, agents, persistence, events, and orchestration;
   - `src/api/` for FastAPI routes, SSE, and projections;
   - `web/` for the React/Vite UI;
   - `tests/` for established pytest patterns and behavioral contracts.
4. Resolve decisions that would materially change behavior, architecture, data compatibility, security, or UX. Use reasonable assumptions for minor details.
5. Create `.codex/plans/` when needed and save the plan using a concise kebab-case name such as `.codex/plans/add-task-cancel.md`. Read an existing same-feature plan before updating it.
6. Report the saved path and the next implementation step.

If ambiguity makes safe planning impossible, end the response with:

```text
NEED_VERIFICATION
<questions>
```

## Plan Format

```markdown
# <Feature Name> Implementation Plan

## Goal
<User-visible or operator-visible outcome.>

## Current State
- <Relevant repository facts and paths.>

## Assumptions
- <Only assumptions that affect implementation. Omit when empty.>

## Files
- `<path>` — <why it changes>.

## Implementation Steps
1. <Ordered, concrete change.>

## Verification
- `<exact command>`

## Risks and Edge Cases
- <Compatibility, concurrency, persistence, security, or UX concern.>
```

## Repository Constraints

- Stay inside the current worktree. Do not use `../` or paths outside it.
- Do not plan `git commit`, `git push`, branch operations, or PR creation as agent steps; the orchestrator owns them.
- Keep the change at or below 40 files.
- Do not include changes to `src/foundry/security.py`, `src/foundry/worktree.py`, or `.env` unless the task explicitly requires them.
- For Python, require Python 3.11+, absolute package imports, public-function and dataclass-field annotations, `from __future__ import annotations`, and `StrEnum` for string enums.
- For TypeScript, preserve strict typing, avoid `any`, and place reusable components under `web/src/components/`.
- Require a pytest test for new behavior. Name tests after the scenario.
- Add Python dependencies only when necessary and through `uv add`.

## Verification Selection

Always plan:

```text
uv run ruff check .
uv run pytest
```

For changes under `web/`, also plan:

```text
npm --prefix web run build
npm --prefix web run lint
```

Add focused pytest commands before the full suite when the affected test module is known.

