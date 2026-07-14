from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from ..security import is_safe_agent_mode
from .base import AgentStage


DEFAULT_MAX_TURNS: dict[AgentStage, int] = {
    AgentStage.PLAN: 50,
    AgentStage.IMPLEMENT: 50,
    AgentStage.VERIFY: 20,
    AgentStage.UI_TESTS: 50,
}


@dataclass(frozen=True)
class OpenCodeOpenAIConfig:
    provider_id: str
    base_url: str
    api_key_env: str
    models: tuple[str, ...]


@dataclass(frozen=True)
class AgentSettings:
    """Settings for a single-stage agent.

    One agent instance is bound to one stage at construction time. To run a
    full pipeline you build three separate agents with three separate
    settings — they can differ in backend, model, turn cap, and timeout.
    """

    stage: AgentStage
    backend: str = "stub"
    timeout_sec: int = 600
    max_turns: int = 30
    model: str = "haiku"
    db_path: Path | None = None
    safe_agent_mode: bool = True
    sandbox_mode: str | None = None
    opencode_openai: OpenCodeOpenAIConfig | None = None
    prompt_template: str | None = None

    @classmethod
    def from_env(cls, stage: AgentStage, db_path: Path | None = None) -> AgentSettings:
        """Load settings for `stage` from environment.

        Per-stage env vars (e.g. `AGENT_PLAN_MODEL`) win over global ones
        (`AGENT_MODEL`); global wins over hard-coded defaults.
        """
        load_dotenv()
        key = stage.value.upper()
        model = os.getenv(f"AGENT_{key}_MODEL") or os.getenv("AGENT_MODEL", "haiku")
        timeout = int(
            os.getenv(f"AGENT_{key}_TIMEOUT_SEC")
            or os.getenv("AGENT_TIMEOUT_SEC", "600")
        )
        max_turns = int(
            os.getenv(f"AGENT_{key}_MAX_TURNS")
            or os.getenv("AGENT_MAX_TURNS", str(DEFAULT_MAX_TURNS[stage]))
        )
        sandbox_mode = (
            os.getenv(f"AGENT_{key}_SANDBOX_MODE")
            or os.getenv("AGENT_SANDBOX_MODE")
            or os.getenv("CODEX_SANDBOX_MODE")
        )
        opencode_openai = _opencode_openai_from_env(model)
        return cls(
            stage=stage,
            backend=os.getenv(f"AGENT_{key}_BACKEND") or os.getenv("CODING_AGENT", "stub"),
            timeout_sec=timeout,
            max_turns=max_turns,
            model=model,
            db_path=db_path,
            safe_agent_mode=is_safe_agent_mode(
                os.getenv(f"AGENT_{key}_SAFE_MODE")
                or os.getenv("SAFE_AGENT_MODE", "true")
            ),
            sandbox_mode=sandbox_mode,
            opencode_openai=opencode_openai,
        )


def _opencode_openai_from_env(model: str) -> OpenCodeOpenAIConfig | None:
    base_url = (os.getenv("OPENCODE_OPENAI_BASE_URL") or "").strip()
    if not base_url:
        return None
    provider_id = (os.getenv("OPENCODE_OPENAI_PROVIDER") or "").strip() or "openwebui"
    api_key_env = (
        (os.getenv("OPENCODE_OPENAI_API_KEY_ENV") or "").strip() or "OPENAI_API_KEY"
    )
    models = _opencode_openai_models(
        raw_models=os.getenv("OPENCODE_OPENAI_MODELS", ""),
        active_model=model,
        provider_id=provider_id,
    )
    return OpenCodeOpenAIConfig(
        provider_id=provider_id,
        base_url=base_url,
        api_key_env=api_key_env,
        models=models,
    )


def _opencode_openai_models(
    *,
    raw_models: str,
    active_model: str,
    provider_id: str,
) -> tuple[str, ...]:
    models: list[str] = []
    for item in raw_models.split(","):
        model = _strip_provider_prefix(item.strip(), provider_id)
        if model:
            models.append(model)
    prefix = f"{provider_id}/"
    if active_model.startswith(prefix):
        models.append(active_model.removeprefix(prefix))
    return tuple(dict.fromkeys(models))


def _strip_provider_prefix(model: str, provider_id: str) -> str:
    prefix = f"{provider_id}/"
    if model.startswith(prefix):
        return model.removeprefix(prefix)
    return model
