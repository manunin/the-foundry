---
name: commit-and-push
description: Prepare The Foundry task-worktree changes for the orchestrator's commit and push stage. Use when the user asks a task agent to commit, push, publish, or prepare changes for PR; this repository forbids agents from performing those git mutations directly.
---

# Commit and Push Handoff

Do not run `git commit`, `git push`, create or switch branches, or open a PR. In The Foundry, the orchestrator owns these operations after verification.

## Workflow

1. Read `AGENTS.md`.
2. Inspect without mutating:
   - `git status --short --branch`
   - `git diff --stat`
   - `git diff --check`
   - `git diff`
3. Confirm:
   - no more than 40 files changed;
   - `src/foundry/security.py`, `src/foundry/worktree.py`, and `.env` are untouched unless explicitly authorized;
   - no dependency folders, build output, caches, logs, credentials, private keys, or machine-local files are present;
   - changes are scoped to the task and unrelated user work remains preserved.
4. Run applicable verification:

```text
uv run ruff check .
uv run pytest
```

For changes under `web/`, also run:

```text
npm --prefix web run build
npm --prefix web run lint
```

5. Report the handoff:
   - suggested imperative commit subject;
   - changed files and scope;
   - validation commands and results;
   - files intentionally left out or blockers.

## Credential Check

Search changed and untracked content for likely credentials without printing secret values. Treat `.env`, private keys, credential JSON, cloud credentials, SSH material, and literal access tokens as blockers. Report only the path, line, and finding type.

Use a targeted local search such as:

```text
rg -n --hidden --glob '!.git' --glob '!node_modules' --glob '!dist' --glob '!build' --glob '!coverage' "(api[_-]?key|secret|token|password|passwd|private[_-]?key|client[_-]?secret|access[_-]?key|refresh[_-]?token|BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY|AWS_SECRET_ACCESS_KEY|GITHUB_TOKEN|OPENAI_API_KEY)"
```

Distinguish configuration variable names and documented placeholders from literal credentials by reviewing the matching diff context.

## Guardrails

- Do not stage files. Staging is part of the orchestrator-owned PR stage.
- Do not amend, squash, rebase, tag, force-push, reset, clean, or delete branches.
- If there are no changes, report that there is nothing to hand off.
- If the user explicitly insists on a commit or push, state that `AGENTS.md` reserves it for the orchestrator and provide the prepared handoff instead.

