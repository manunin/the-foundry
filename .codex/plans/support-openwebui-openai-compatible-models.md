# OpenWebUI OpenAI-Compatible Models Implementation Plan

## Goal
Allow The Foundry to run its `opencode_cli` coding backend against local or self-hosted models exposed through an OpenAI-compatible API, specifically `https://openwebui.ai.bpcbt.com/api/v1`, without requiring each target repository worktree to carry OpenCode provider config.

## Current State
- `src/foundry/agents/config.py` loads per-stage agent settings from env: backend, model, timeout, max turns, safe mode, and Codex sandbox mode.
- `src/foundry/agents/opencode_cli.py` shells out to `opencode run --format json --dir <worktree> -m <model>`, then streams NDJSON events into the existing agent event contract.
- `src/foundry/security.py` scrubs agent subprocess env. For `opencode_cli`, it already allows `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, and `OPENROUTER_API_KEY`; arbitrary extra vars can be passed through with `AGENT_ENV_ALLOWLIST`.
- `.env.example` and `README.md` document OpenCode with DeepSeek, but not self-hosted OpenAI-compatible endpoints.
- OpenCode supports custom provider configuration through JSON config, including `provider.<id>.npm="@ai-sdk/openai-compatible"` and `provider.<id>.options.baseURL`. It also supports inline config with `OPENCODE_CONFIG_CONTENT`.

## Assumptions
- Foundry should not call the OpenWebUI API directly; it should keep using `opencode_cli` as the coding-agent adapter.
- Operators will provide the actual model IDs available from OpenWebUI. Foundry cannot safely infer them from `/models` during normal agent startup because that would add network coupling to config loading.
- `OPENAI_API_KEY` is the default credential env var for the OpenWebUI/OpenAI-compatible provider. If a deployment uses another env var, it can be named via config and passed through with `AGENT_ENV_ALLOWLIST`.

## Files
- `src/foundry/agents/config.py` - add typed OpenCode OpenAI-compatible provider settings loaded from env.
- `src/foundry/agents/opencode_cli.py` - inject `OPENCODE_CONFIG_CONTENT` into the scrubbed subprocess env when the OpenAI-compatible provider settings are present.
- `tests/test_agents_config.py` - cover env parsing for provider id, base URL, credential env var, and model list.
- `tests/test_agents_opencode_cli.py` - cover generated OpenCode config JSON and env passed to `iter_cli_jsonl_with_retry`.
- `tests/test_security.py` - add coverage that custom credential env vars can still be passed by `AGENT_ENV_ALLOWLIST`; do not otherwise change the scrub policy.
- `.env.example` - document an OpenWebUI/OpenAI-compatible example using `CODING_AGENT=opencode_cli`, `OPENCODE_OPENAI_BASE_URL=https://openwebui.ai.bpcbt.com/api/v1`, `OPENCODE_OPENAI_PROVIDER=openwebui`, `AGENT_MODEL=openwebui/<model-id>`, and `OPENAI_API_KEY`.
- `README.md` - add a short operator runbook for OpenWebUI/OpenAI-compatible local models.
- `docs/ARCHITECTURE.md` - update the agent/LLM section to mention OpenAI-compatible local endpoints through OpenCode.

## Implementation Steps
1. Add a frozen dataclass in `src/foundry/agents/config.py`, for example `OpenCodeOpenAIConfig`, with fields:
   - `provider_id: str`
   - `base_url: str`
   - `api_key_env: str`
   - `models: tuple[str, ...]`
2. Extend `AgentSettings` with `opencode_openai: OpenCodeOpenAIConfig | None = None`.
3. In `AgentSettings.from_env`, build `opencode_openai` only when `OPENCODE_OPENAI_BASE_URL` is set.
   - Default `provider_id` from `OPENCODE_OPENAI_PROVIDER`, falling back to `openwebui`.
   - Default `api_key_env` from `OPENCODE_OPENAI_API_KEY_ENV`, falling back to `OPENAI_API_KEY`.
   - Build `models` from `OPENCODE_OPENAI_MODELS` plus the active `model` if it uses the configured provider prefix.
   - Store model IDs without the provider prefix in the OpenCode config, while keeping `AGENT_MODEL` as the CLI-facing `provider/model` string.
4. Add a small helper in `opencode_cli.py` to build the inline OpenCode config:
   - include `$schema`;
   - include `provider[provider_id].npm = "@ai-sdk/openai-compatible"`;
   - include `provider[provider_id].name`;
   - include `provider[provider_id].options.baseURL`;
   - include `provider[provider_id].options.apiKey = "{env:<api_key_env>}"`;
   - include `provider[provider_id].models` for each configured model.
5. Replace the direct `env=scrubbed_agent_env(self.name)` call in `OpencodeCliAgent.apply` with a method that:
   - starts from `scrubbed_agent_env(self.name)`;
   - adds `OPENCODE_CONFIG_CONTENT` when `settings.opencode_openai` is set;
   - copies the configured credential env var from `os.environ` only if it is already present in the scrubbed env or is explicitly allowed by `AGENT_ENV_ALLOWLIST`.
6. Keep project worktrees clean: do not write `opencode.json` into the target repo. Use only inline env config.
7. Update docs and `.env.example` with a concrete OpenWebUI example:
   - `CODING_AGENT=opencode_cli`
   - `OPENCODE_OPENAI_PROVIDER=openwebui`
   - `OPENCODE_OPENAI_BASE_URL=https://openwebui.ai.bpcbt.com/api/v1`
   - `OPENCODE_OPENAI_MODELS=<comma-separated model ids>`
   - `AGENT_MODEL=openwebui/<model-id>`
   - `OPENAI_API_KEY=<token or dummy value if the gateway allows it>`
8. Add focused tests for parsing and config injection, then run the full verification suite.

## Verification
- `uv run pytest tests/test_agents_config.py tests/test_agents_opencode_cli.py tests/test_security.py`
- `uv run ruff check .`
- `uv run pytest`

## Risks and Edge Cases
- OpenWebUI model IDs must match exactly what its OpenAI-compatible endpoint expects; Foundry should document this instead of guessing.
- Some OpenAI-compatible gateways require `/v1`, while the provided endpoint already ends with `/api/v1`; Foundry should pass the URL verbatim and not append path segments.
- If operators use a non-default credential env var, it must be included in `AGENT_ENV_ALLOWLIST` or it will be scrubbed before OpenCode starts.
- OpenCode/project config precedence can override inline config if the target repo has its own stronger project config. The docs should call out that Foundry's inline config is intended for normal worktrees without committed OpenCode provider config.
- This does not add support to `codex_cli`; custom OpenAI-compatible endpoints remain an `opencode_cli` feature.
