from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from foundry.agents import AgentResult, AgentStage
from foundry.config import Settings
from foundry.models import Task
from foundry.stages import ui_tests


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values = {
        "source_repo": "owner/repo",
        "target_repo": "owner/repo",
        "issue_label": "agent-task",
        "worktree_root": tmp_path / "worktrees",
        "db_path": tmp_path / "data" / "foundry.sqlite",
        "poll_interval_seconds": 30,
    }
    values.update(overrides)
    return Settings(**values)


def _task() -> Task:
    return Task("owner/repo", 7, "UI change", "test it", id=3)


def _agent_writing(manifest: dict, files: dict[str, bytes | str]) -> MagicMock:
    agent = MagicMock()
    agent.name = "fake"

    def apply(*, worktree: Path, **kwargs: object) -> AgentResult:
        root = worktree / ui_tests.OUTPUT_DIR
        root.mkdir(parents=True, exist_ok=True)
        for relative, content in files.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
        (root / "result.json").write_text(json.dumps(manifest), encoding="utf-8")
        return AgentResult(AgentStage.UI_TESTS, "done", "done")

    agent.apply.side_effect = apply
    return agent


def test_ui_tests_copies_screenshot_and_bounds_log_tail(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    skill = worktree / ui_tests.DEPLOY_SKILL
    skill.parent.mkdir(parents=True)
    skill.write_text("deploy", encoding="utf-8")
    manifest = {
        "version": 1,
        "status": "failed",
        "deployed_url": "https://stand.test",
        "scenarios": [
            {
                "name": "save form",
                "status": "failed",
                "duration_ms": 12,
                "error": "button missing",
                "screenshots": ["screenshots/failure.png"],
            }
        ],
        "logs": {"core": "logs/core.log"},
    }
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 20
    agent = _agent_writing(
        manifest, {"screenshots/failure.png": png, "logs/core.log": "0123456789"}
    )
    settings = _settings(tmp_path, ui_test_log_max_chars=4)

    with patch("foundry.stages.ui_tests.make_agent", return_value=agent):
        result = ui_tests.run(
            _task(), worktree, settings, plan_text="crawl", attempt=1
        )

    assert result["failure_kind"] == "ui_crawler"
    assert result["retryable"] is True
    assert result["core_logs"] == "6789"
    assert result["screenshots"][0]["artifact_path"] == (
        "attempt-1/screenshots/failure.png"
    )
    assert (
        settings.ui_test_artifact_root
        / "task-3/attempt-1/screenshots/failure.png"
    ).read_bytes() == png
    assert not (worktree / ui_tests.OUTPUT_DIR).exists()


def test_ui_tests_rejects_path_traversal_as_infrastructure_failure(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "wt"
    skill = worktree / ui_tests.DEPLOY_SKILL
    skill.parent.mkdir(parents=True)
    skill.write_text("deploy", encoding="utf-8")
    manifest = {
        "version": 1,
        "status": "passed",
        "deployed_url": "https://stand.test",
        "scenarios": [
            {
                "name": "home",
                "status": "passed",
                "duration_ms": 1,
                "screenshots": ["../secret.png"],
            }
        ],
        "logs": {},
    }
    agent = _agent_writing(manifest, {})

    with patch("foundry.stages.ui_tests.make_agent", return_value=agent):
        result = ui_tests.run(
            _task(), worktree, _settings(tmp_path), plan_text="crawl", attempt=1
        )

    assert result["failure_kind"] == "infra"
    assert result["requires_human"] is True
    assert "unsafe artifact path" in result["report"]


def test_ui_tests_missing_deploy_skill_is_infrastructure_failure(
    tmp_path: Path,
) -> None:
    result = ui_tests.run(
        _task(), tmp_path / "wt", _settings(tmp_path), plan_text="crawl", attempt=1
    )

    assert result["failure_kind"] == "infra"
    assert result["requires_human"] is True
