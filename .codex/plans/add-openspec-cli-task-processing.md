# OpenSpec CLI Task Processing Implementation Plan

## Goal
Enable Foundry task processing to use OpenSpec-aware target repositories by installing the OpenSpec CLI in Docker images and surfacing OpenSpec context, instructions, and validation during the existing PLAN/IMPLEMENT/VERIFY flow.

## Current State
- Agent backends are pluggable under `src/foundry/agents/`; `CODING_AGENT` currently supports `stub`, `claude_cli`, `codex_cli`, and `opencode_cli` via `src/foundry/agents/factory.py`.
- Task processing flows through `CONTEXT -> PLAN -> IMPLEMENT -> VERIFY` in `src/foundry/workflows.py`; the `CONTEXT` output is rendered into the planner prompt by `src/foundry/stages/context.py`.
- `src/foundry/stages/verify.py` auto-detects deterministic commands from repository markers such as `pyproject.toml`, `package.json`, and `Cargo.toml`.
- Docker already supports optional LLM CLI installation with `INSTALL_CLAUDE_CLI`, `INSTALL_CODEX_CLI`, and `INSTALL_OPENCODE_CLI` in `Dockerfile` and `docker-compose.yml`.
- Compose mounts `${HOST_CODEX_DIR:-./.docker/codex}` into `/root/.codex`, so Codex can see target-repo `.codex/skills/openspec-*` files once the cloned worktree contains them, but the OpenSpec CLI itself is not installed in the image.
- OpenSpec documentation identifies `openspec list`, `openspec show`, `openspec status`, `openspec instructions`, `openspec templates`, `openspec schemas`, and `openspec validate --all --json` as agent-compatible commands. `openspec init` remains interactive and should not run during task processing.

## Assumptions
- The target repositories already contain initialized OpenSpec artifacts, such as `openspec/` and `.codex/skills/openspec-*`; Foundry should consume them, not initialize OpenSpec inside task worktrees.
- OpenSpec CLI should be a repository tooling dependency, not a new `CODING_AGENT` backend. The existing LLM backend still plans and implements code.
- Docker installation should mirror existing optional CLI flags and use `npm install -g @fission-ai/openspec@latest` behind an `INSTALL_OPENSPEC_CLI` build arg.
- OpenSpec telemetry should be disabled inside Foundry containers with `OPENSPEC_TELEMETRY=0` to keep automated processing predictable.

## Files
- `Dockerfile` — add `INSTALL_OPENSPEC_CLI` and install `@fission-ai/openspec@latest` when enabled.
- `docker-compose.yml` — pass `INSTALL_OPENSPEC_CLI` to backend service builds and set `OPENSPEC_TELEMETRY=0` for `api`, `worker`, and `pr-feedback`.
- `.env.example` — document `INSTALL_OPENSPEC_CLI`, OpenSpec task-processing behavior, and the expectation that target repos are already initialized.
- `README.md` — document Docker usage, smoke commands, and how OpenSpec skills in target repos are picked up by task processing.
- `docs/ARCHITECTURE.md` — add OpenSpec as optional target-repo tooling used by context and verify stages.
- `src/foundry/stages/openspec.py` — new helper for detecting OpenSpec roots, checking CLI availability, running bounded JSON commands, and formatting compact results.
- `src/foundry/stages/context.py` — detect OpenSpec artifacts, collect `openspec status --json` and `openspec instructions --json`, and include a concise OpenSpec section in planner context.
- `src/foundry/stages/verify.py` — append `openspec validate --all --json` to detected deterministic checks when an OpenSpec root exists and the CLI is available.
- `tests/test_context_stage.py` — cover formatted OpenSpec context in planner prompt.
- `tests/test_verify_stage.py` — cover OpenSpec validation auto-detection and absence when no OpenSpec root exists.
- `tests/test_docker_compose.py` — assert compose passes the new build arg and telemetry env to backend services.
- `tests/test_openspec_stage.py` — cover detection, command parsing, missing CLI behavior, and output truncation/formatting.

## Implementation Steps
1. Add `src/foundry/stages/openspec.py` with:
   - `has_openspec(root: Path) -> bool`, returning true for `root / "openspec"` and optionally `.codex/skills/openspec-*`.
   - `cli_available() -> bool`, based on `shutil.which("openspec")`.
   - `collect_context(root: Path, timeout_sec: int) -> dict[str, object]`, running only non-interactive agent-compatible commands with `--json`.
   - `validate_command(root: Path) -> list[str] | None`, returning `["openspec", "validate", "--all", "--json"]` only when OpenSpec is present.
   - Formatting helpers that cap captured JSON/output to avoid oversized planner prompts.
2. Update `context.run` to call the OpenSpec helper after normal manifest/test detection. Store the result under `ctx["openspec"]`.
3. Update `context.format_for_prompt` to add `### OpenSpec` with status, active change/task hints, and next-step instructions when present. If OpenSpec artifacts exist but the CLI is missing, include a short warning so the planner knows not to rely on CLI output.
4. Update `verify._detect_verify_commands` to append `openspec validate --all --json` after language/framework checks when an OpenSpec root exists. Keep explicit `VERIFY_COMMANDS` override behavior unchanged.
5. Add `INSTALL_OPENSPEC_CLI=false` to `Dockerfile` build args and install via npm only when true.
6. Add `INSTALL_OPENSPEC_CLI: ${INSTALL_OPENSPEC_CLI:-false}` to each backend build block in `docker-compose.yml`. Set `OPENSPEC_TELEMETRY: "0"` in backend service environments.
7. Update `.env.example`, `README.md`, and `docs/ARCHITECTURE.md` to document:
   - `INSTALL_OPENSPEC_CLI=true docker compose build api worker pr-feedback`
   - target repo must already contain OpenSpec artifacts and skills
   - Foundry uses OpenSpec during context and verify, while the selected `CODING_AGENT` still performs LLM work
   - `openspec init` is intentionally not run by the orchestrator.
8. Add focused pytest coverage for OpenSpec helper behavior, context rendering, verify auto-detection, and compose build args.

## Verification
- `uv run pytest tests/test_openspec_stage.py tests/test_context_stage.py tests/test_verify_stage.py tests/test_docker_compose.py`
- `uv run ruff check .`
- `uv run pytest`

## Risks and Edge Cases
- OpenSpec JSON output may change between releases. Keep parsing defensive and include raw summarized output when fields are unknown.
- Missing `openspec` should not break repositories that merely contain stale OpenSpec skill files; it should produce context warning and skip validation command unless configured otherwise.
- `openspec validate --all --json` failures should be deterministic verification failures, not infra failures, because invalid specs should block PR creation.
- Agent-compatible commands are safe for automation, but `openspec init`, `openspec view`, and editor-opening commands are interactive and must stay out of task processing.
- Installing `@fission-ai/openspec@latest` makes Docker builds use the latest CLI. If reproducibility becomes more important than freshness, follow-up work should add an `OPENSPEC_CLI_VERSION` build arg.
