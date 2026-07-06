from __future__ import annotations

from pathlib import Path

import pytest

from foundry.config import ConfigError, load_settings
from foundry.models import ForgeKind


def test_load_settings_reads_base_branch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("SOURCE_REPO", "owner/sandbox")
    monkeypatch.setenv("TARGET_REPO", "owner/sandbox")
    monkeypatch.setenv("BASE_BRANCH", "develop")

    settings = load_settings(env_path)

    assert settings.base_branch == "develop"


def test_load_settings_defaults_base_branch_to_main(
    tmp_path: Path,
    monkeypatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("SOURCE_REPO", "owner/sandbox")
    monkeypatch.setenv("TARGET_REPO", "owner/sandbox")
    monkeypatch.delenv("BASE_BRANCH", raising=False)

    settings = load_settings(env_path)

    assert settings.base_branch == "main"


def test_load_settings_selects_self_managed_gitlab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("SOURCE_REPO", "group/issues")
    monkeypatch.setenv("TARGET_REPO", "group/code")
    monkeypatch.setenv("FORGE_PROVIDER", "GiTlAb")
    monkeypatch.setenv("GITLAB_HOST", "https://gitlab.example.test/")

    settings = load_settings(env_path)

    assert settings.forge is ForgeKind.GITLAB
    assert settings.forge_host == "gitlab.example.test"


def test_load_settings_rejects_unknown_forge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("SOURCE_REPO", "owner/source")
    monkeypatch.setenv("TARGET_REPO", "owner/target")
    monkeypatch.setenv("FORGE_PROVIDER", "gitea")

    with pytest.raises(ConfigError, match="FORGE_PROVIDER"):
        load_settings(env_path)
