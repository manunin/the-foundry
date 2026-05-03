# DEBUG.md

Verified runbook for local development in this repo. Keep this file practical:
commands here should be useful for debugging the current code, not for
preserving old architecture history.

## Runtime

The project has three local processes:

- `foundry` Python CLI (`src/foundry`, entrypoint `foundry = "foundry.cli:main"`)
- FastAPI backend (`src/api/main.py`)
- Vite/React UI (`web/`)

External tools used by the worker are `gh`, `git`, and the selected coding
agent CLI (`claude`, `codex`, or `opencode`) when `CODING_AGENT` is not `stub`.
State lives in SQLite at `DB_PATH` (`./data/foundry.sqlite` by default).

## Setup

```bash
uv sync
cd web && npm install
cp .env.example .env
```

At minimum, fill `SOURCE_REPO` and `TARGET_REPO` in `.env`. GitHub auth is
handled by `gh auth login`; `GITHUB_TOKEN` is intentionally unused by the worker.

## CLI

```bash
uv run foundry --help
uv run foundry status
uv run foundry reset <task_id>
uv run foundry run --once
uv run foundry run
uv run foundry run-issue <issue_number>
uv run foundry pr-feedback --once
uv run foundry pr-feedback
```

`foundry run` and `foundry pr-feedback` run continuously by default. Add
`--once` for a single pass. Use `--interval <seconds>` to override
`POLL_INTERVAL_SECONDS`.

## Local Services

Run these in separate terminals:

```bash
uv run foundry run
uv run uvicorn api.main:app --reload
cd web && npm run dev
```

API health check:

```bash
curl -fsS http://localhost:8000/
```

Important endpoints:

- `GET /api/tasks`
- `GET /api/tasks/{id}`
- `GET /api/tasks/{id}/events` (SSE, supports `Last-Event-ID`)
- `POST /api/tasks/{id}/reset`
- `POST /api/tasks/{id}/resume`
- `GET /api/repos`
- `GET /api/repos/{repo}/memory`

## Fast Feedback

Use `uv run python -c '...'` for small probes. The package is installed editable,
so source changes are picked up on the next `uv run`.

Seed a task without GitHub:

```bash
SOURCE_REPO=demo/x TARGET_REPO=demo/x DB_PATH=/tmp/foundry-probe.sqlite \
uv run python -c "
from pathlib import Path
from foundry import state
from foundry.models import Task
db = Path('/tmp/foundry-probe.sqlite')
state.init_db(db)
t = state.upsert_task(db, Task(repo='demo/x', issue_number=1, issue_title='probe', issue_body=''))
print('inserted id=', t.id)
"
```

Inspect it:

```bash
SOURCE_REPO=demo/x TARGET_REPO=demo/x DB_PATH=/tmp/foundry-probe.sqlite \
uv run foundry status
```

## Workflows

Named workflows live in `src/foundry/workflows.py`.

- `dev_task`: full issue cycle,
  `fetch -> context -> plan -> (implement -> verify) x N -> pr -> done`.
- `pr_verify`: verification-only entrypoint against an existing task/worktree;
  it does not commit, push, open a PR, close the issue, or mark the task done.
- `pr_feedback`: scans open `foundry/task-*` PRs, formats review/CI feedback,
  applies fixes on the existing branch, verifies, pushes, and comments.

Verification results are normalized to:

```python
{
    "passed": bool,
    "retryable": bool,
    "requires_human": bool,
    "failure_kind": "deterministic" | "acceptance" | "infra" | "unclear" | "dangerous",
    "report": "...",
}
```

Planner/implementer output ending with `NEED_VERIFICATION` blocks the task and
posts a question back to the issue. The API `resume` endpoint or CLI `reset`
returns it to `pending/fetch` after a human answers.

## Tests

Current collected test count:

```bash
uv run pytest --collect-only -q  # 193 tests collected
uv run pytest
```

Focused runs:

```bash
uv run pytest tests/test_workflows.py -v
uv run pytest tests/test_agents_claude_cli.py -v
uv run pytest tests/test_api.py tests/test_sse.py -v
```

Tests are offline. GitHub, git worktrees, and real CLI agents are mocked where
needed; `stub` is the deterministic backend for local smoke paths.

Frontend checks:

```bash
cd web
npx tsc --noEmit
npm run build
```

## Real Pipeline Safety

`foundry run` performs live side effects: clone/fetch, worktree creation,
agent subprocesses, commit, push, PR creation, issue comments, and issue close.
Before running it against GitHub:

1. Use a disposable sandbox repo for `SOURCE_REPO` and `TARGET_REPO`.
2. Keep `SAFE_AGENT_MODE=true` unless the sandbox has no valuable secrets.
3. Point `WORKTREE_ROOT` and `DB_PATH` at disposable locations for experiments.
4. Confirm `gh auth status` is logged in with repo access.

The worker stores checkpoints before implement retries at
`data/checkpoints/task-{id}-attempt-{n}-pre.diff`. Failed task worktrees are not
cleaned automatically, so useful diffs can be inspected manually.
