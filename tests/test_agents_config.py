from __future__ import annotations

import os

import pytest

from foundry.agents import AgentSettings, AgentStage


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Prevent .env in the repo root from leaking into tests.
    monkeypatch.setattr("foundry.agents.config.load_dotenv", lambda *a, **kw: False)
    for key in list(os.environ):
        if key.startswith("AGENT_") or key in {
            "CODING_AGENT",
            "CODEX_SANDBOX_MODE",
            "OPENCODE_OPENAI_API_KEY_ENV",
            "OPENCODE_OPENAI_BASE_URL",
            "OPENCODE_OPENAI_MODELS",
            "OPENCODE_OPENAI_PROVIDER",
            "SAFE_AGENT_MODE",
        }:
            monkeypatch.delenv(key, raising=False)


def test_from_env_returns_defaults_when_nothing_set() -> None:
    settings = AgentSettings.from_env(AgentStage.PLAN)

    assert settings.stage is AgentStage.PLAN
    assert settings.backend == "stub"
    assert settings.model == "haiku"
    assert settings.max_turns == 50
    assert settings.timeout_sec == 600
    assert settings.safe_agent_mode is True
    assert settings.sandbox_mode is None


def test_from_env_global_coding_agent_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_AGENT", "claude_cli")

    settings = AgentSettings.from_env(AgentStage.IMPLEMENT)

    assert settings.backend == "claude_cli"


def test_from_env_per_stage_backend_beats_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODING_AGENT", "stub")
    monkeypatch.setenv("AGENT_IMPLEMENT_BACKEND", "claude_cli")

    implement = AgentSettings.from_env(AgentStage.IMPLEMENT)
    plan = AgentSettings.from_env(AgentStage.PLAN)

    assert implement.backend == "claude_cli"
    assert plan.backend == "stub"


def test_from_env_per_stage_model_beats_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_MODEL", "sonnet")
    monkeypatch.setenv("AGENT_VERIFY_MODEL", "opus")

    verify = AgentSettings.from_env(AgentStage.VERIFY)
    plan = AgentSettings.from_env(AgentStage.PLAN)

    assert verify.model == "opus"
    assert plan.model == "sonnet"


def test_from_env_parses_integer_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PLAN_MAX_TURNS", "7")
    monkeypatch.setenv("AGENT_PLAN_TIMEOUT_SEC", "42")

    settings = AgentSettings.from_env(AgentStage.PLAN)

    assert settings.max_turns == 7
    assert settings.timeout_sec == 42


def test_from_env_default_max_turns_per_stage_differs() -> None:
    verify = AgentSettings.from_env(AgentStage.VERIFY)
    implement = AgentSettings.from_env(AgentStage.IMPLEMENT)

    assert verify.max_turns == 20
    assert implement.max_turns == 50


def test_from_env_parses_safe_agent_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAFE_AGENT_MODE", "false")

    settings = AgentSettings.from_env(AgentStage.IMPLEMENT)

    assert settings.safe_agent_mode is False


def test_from_env_parses_codex_sandbox_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_SANDBOX_MODE", "danger-full-access")

    settings = AgentSettings.from_env(AgentStage.IMPLEMENT)

    assert settings.sandbox_mode == "danger-full-access"


def test_from_env_per_stage_sandbox_mode_beats_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_SANDBOX_MODE", "danger-full-access")
    monkeypatch.setenv("AGENT_IMPLEMENT_SANDBOX_MODE", "workspace-write")

    implement = AgentSettings.from_env(AgentStage.IMPLEMENT)
    plan = AgentSettings.from_env(AgentStage.PLAN)

    assert implement.sandbox_mode == "workspace-write"
    assert plan.sandbox_mode == "danger-full-access"


def test_from_env_leaves_opencode_openai_disabled_without_base_url() -> None:
    settings = AgentSettings.from_env(AgentStage.IMPLEMENT)

    assert settings.opencode_openai is None


def test_from_env_parses_opencode_openai_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_OPENAI_BASE_URL", "https://openwebui.example/api/v1")
    monkeypatch.setenv("AGENT_MODEL", "openwebui/qwen3-coder")

    settings = AgentSettings.from_env(AgentStage.IMPLEMENT)

    assert settings.opencode_openai is not None
    assert settings.opencode_openai.provider_id == "openwebui"
    assert settings.opencode_openai.base_url == "https://openwebui.example/api/v1"
    assert settings.opencode_openai.api_key_env == "OPENAI_API_KEY"
    assert settings.opencode_openai.models == ("qwen3-coder",)
    assert settings.model == "openwebui/qwen3-coder"


def test_from_env_parses_opencode_openai_provider_key_and_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_OPENAI_BASE_URL", "http://localhost:8080/api/v1")
    monkeypatch.setenv("OPENCODE_OPENAI_PROVIDER", "local")
    monkeypatch.setenv("OPENCODE_OPENAI_API_KEY_ENV", "OPENWEBUI_API_KEY")
    monkeypatch.setenv(
        "OPENCODE_OPENAI_MODELS",
        "qwen3-coder, local/devstral, qwen3-coder",
    )
    monkeypatch.setenv("AGENT_MODEL", "local/gpt-oss")

    settings = AgentSettings.from_env(AgentStage.IMPLEMENT)

    assert settings.opencode_openai is not None
    assert settings.opencode_openai.provider_id == "local"
    assert settings.opencode_openai.base_url == "http://localhost:8080/api/v1"
    assert settings.opencode_openai.api_key_env == "OPENWEBUI_API_KEY"
    assert settings.opencode_openai.models == ("qwen3-coder", "devstral", "gpt-oss")
