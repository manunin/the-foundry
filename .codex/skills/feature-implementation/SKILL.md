---
name: feature-implementation
description: Implement and verify features in The Foundry, either from a direct request or a plan under `.codex/plans`. Use when the user asks to build, add, complete, execute, or apply a feature in this repository.
---

# Feature Implementation

Implement the requested behavior in the current task worktree and leave the changes ready for the orchestrator to verify and publish.

## Workflow

1. Read `AGENTS.md` and run `git status --short --branch`.
2. Read a referenced `.codex/plans/*.md` plan. If the user names a feature rather than a path, look for a matching plan; otherwise implement from the direct request.
3. Read `docs/ARCHITECTURE.md` and any matching file under `docs/specs/`. For observability, events, API projections, SSE, or live UI work, read `docs/specs/observability-ui.md`.
4. Inspect all files to be changed and the closest tests before editing. Re-check a saved plan against current code.
5. Implement the smallest complete change:
   - preserve unrelated user changes;
   - follow existing boundaries across `src/foundry/`, `src/api/`, and `web/`;
   - reuse established helpers and component patterns;
   - add or update pytest coverage for new behavior;
   - update docs only when a public contract or operator workflow changes.
6. Verify narrowly while iterating, then run the applicable project checks.
7. Review `git diff --check`, `git diff --stat`, and the final diff. Confirm no forbidden paths or unrelated files changed and the total remains at or below 40 files.
8. Report behavior changed, key files, checks run, and any exact blocker.

If a critical ambiguity makes implementation unsafe, end the response with:

```text
NEED_VERIFICATION
<questions>
```

## Repository Constraints

- Work only inside the current worktree. Do not read or edit outside `cwd`.
- Never commit, push, create or switch branches, or open a PR. The Foundry orchestrator owns the PR stage.
- Do not edit `src/foundry/security.py`, `src/foundry/worktree.py`, or `.env` unless the task explicitly names that area.
- Do not change more than 40 files.
- Do not install or add dependencies unless the feature genuinely requires them. Add Python dependencies with `uv add`.
- Preserve the append-only event model and FSM invariants when touching orchestration.
- Handle errors at system boundaries such as subprocesses and external APIs; trust internal invariants.
- Add comments only for non-obvious rationale.

## Python Standards

- Target Python 3.11+.
- Add `from __future__ import annotations` to every annotated Python file.
- Use absolute imports such as `from foundry.models import Task`.
- Annotate public functions and dataclass fields.
- Use `StrEnum` for string enums.
- Put pytest tests under `tests/` and name each test after its scenario.

## Frontend Standards

- Keep TypeScript strict and do not use `any`.
- Put reusable React components under `web/src/components/`.
- Preserve loading, empty, error, and reconnect behavior where the affected UI flow needs them.
- Keep API types synchronized with `src/api/projections.py` and route responses.

## Verification

Run focused tests first when possible, then:

```text
uv run ruff check .
uv run pytest
```

For changes under `web/`, also run:

```text
npm --prefix web run build
npm --prefix web run lint
```

Do not hide a failed check. If a dependency, service, credential, or environment issue prevents a check, report the command and exact blocker.

