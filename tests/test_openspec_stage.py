from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from foundry.shell import Result
from foundry.stages import openspec


def test_has_openspec_detects_root_artifacts(tmp_path: Path) -> None:
    assert openspec.has_openspec(tmp_path) is False

    (tmp_path / "openspec").mkdir()

    assert openspec.has_openspec(tmp_path) is True


def test_has_openspec_detects_codex_skills(tmp_path: Path) -> None:
    skill = tmp_path / ".codex" / "skills" / "openspec-apply-change"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("name: openspec-apply-change\n", encoding="utf-8")

    assert openspec.has_openspec(tmp_path) is True


def test_collect_context_reports_missing_cli(tmp_path: Path) -> None:
    (tmp_path / "openspec").mkdir()
    skill = tmp_path / ".codex" / "skills" / "openspec-plan-change"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("OpenSpec skill body", encoding="utf-8")

    with patch.object(openspec, "cli_available", return_value=False):
        ctx = openspec.collect_context(tmp_path, timeout_sec=10)

    assert ctx["present"] is True
    assert ctx["cli_available"] is False
    assert "not available" in str(ctx["warning"])
    assert ctx["skills"] == [
        {
            "path": ".codex/skills/openspec-plan-change/SKILL.md",
            "text": "OpenSpec skill body",
        }
    ]


def test_format_context_renders_openspec_skills_without_cli_commands(
    tmp_path: Path,
) -> None:
    skill = tmp_path / ".codex" / "skills" / "openspec-plan-change"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Use proposal.md", encoding="utf-8")

    ctx = {
        "present": True,
        "forced": True,
        "cli_available": True,
        "skills": openspec._skill_docs(tmp_path),
    }

    rendered = "\n".join(openspec.format_context(ctx))

    assert "OpenSpec mode is exclusive" in rendered
    assert "### Repository OpenSpec skills" in rendered
    assert ".codex/skills/openspec-plan-change/SKILL.md" in rendered
    assert "Use proposal.md" in rendered


def test_collect_context_runs_agent_compatible_json_commands(tmp_path: Path) -> None:
    (tmp_path / "openspec").mkdir()
    seen: list[list[str]] = []

    def _run(cmd: list[str], **kwargs: object) -> Result:
        seen.append(cmd)
        assert kwargs["cwd"] == tmp_path
        assert kwargs["timeout"] == 10
        assert kwargs["env"]["OPENSPEC_TELEMETRY"] == "0"
        if cmd[:2] == ["openspec", "status"]:
            return Result(
                returncode=0,
                stdout='{"change":"add-api","done":false}',
                stderr="",
            )
        return Result(returncode=0, stdout='{"next":["write tasks"]}', stderr="")

    with patch.object(openspec, "cli_available", return_value=True), patch.object(
        openspec.shell,
        "run",
        side_effect=_run,
    ):
        ctx = openspec.collect_context(tmp_path, timeout_sec=10)

    assert seen == [
        ["openspec", "status", "--json"],
        ["openspec", "instructions", "--json"],
    ]
    commands = ctx["commands"]
    assert isinstance(commands, dict)
    assert commands["status"]["data"] == {"change": "add-api", "done": False}
    assert commands["instructions"]["data"] == {"next": ["write tasks"]}


def test_collect_context_captures_timeout_as_command_result(tmp_path: Path) -> None:
    (tmp_path / "openspec").mkdir()

    def _run(cmd: list[str], **kwargs: object) -> Result:
        if cmd[:2] == ["openspec", "status"]:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=10)
        return Result(returncode=0, stdout="{}", stderr="")

    with patch.object(openspec, "cli_available", return_value=True), patch.object(
        openspec.shell,
        "run",
        side_effect=_run,
    ):
        ctx = openspec.collect_context(tmp_path, timeout_sec=10)

    commands = ctx["commands"]
    assert isinstance(commands, dict)
    assert commands["status"]["ok"] is False
    assert commands["status"]["error"] == "timeout after 10s"


def test_validate_command_requires_artifacts_and_cli(tmp_path: Path) -> None:
    with patch.object(openspec, "cli_available", return_value=True):
        assert openspec.validate_command(tmp_path) is None

    (tmp_path / "openspec").mkdir()
    with patch.object(openspec, "cli_available", return_value=False):
        assert openspec.validate_command(tmp_path) is None

    with patch.object(openspec, "cli_available", return_value=True):
        assert openspec.validate_command(tmp_path) == [
            "openspec",
            "validate",
            "--all",
            "--json",
        ]


def test_build_implementation_handoff_uses_artifacts_cli_and_skills(
    tmp_path: Path,
) -> None:
    tasks = tmp_path / "openspec" / "changes" / "add-api" / "tasks.md"
    tasks.parent.mkdir(parents=True)
    tasks.write_text("- [ ] implement", encoding="utf-8")
    (tmp_path / "openspec" / "changes" / "archive").mkdir()
    skill = tmp_path / ".codex" / "skills" / "openspec-apply-change"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Apply tasks only", encoding="utf-8")

    def _run(cmd: list[str], **kwargs: object) -> Result:
        assert kwargs["cwd"] == tmp_path
        if cmd[:2] == ["openspec", "status"]:
            return Result(returncode=0, stdout='{"change":"add-api"}', stderr="")
        return Result(returncode=0, stdout='{"steps":["apply"]}', stderr="")

    with patch.object(openspec, "cli_available", return_value=True), patch.object(
        openspec.shell,
        "run",
        side_effect=_run,
    ):
        handoff = openspec.build_implementation_handoff(tmp_path, timeout_sec=10)

    assert "FOUNDRY_OPENSPEC_MODE=true" in handoff
    assert "Do not use the planner transcript" in handoff
    assert "Active change: `add-api`" in handoff
    assert "`openspec status --change add-api --json`: ok" in handoff
    assert "`openspec/changes/add-api/tasks.md`" in handoff
    assert ".codex/skills/openspec-apply-change/SKILL.md" in handoff
    assert "Apply tasks only" in handoff


def test_build_implementation_handoff_can_reference_skills_without_bodies(
    tmp_path: Path,
) -> None:
    tasks = tmp_path / "openspec" / "changes" / "add-api" / "tasks.md"
    tasks.parent.mkdir(parents=True)
    tasks.write_text("- [ ] implement", encoding="utf-8")
    skill = tmp_path / ".codex" / "skills" / "openspec-apply-change"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Apply tasks only", encoding="utf-8")

    def _run(cmd: list[str], **kwargs: object) -> Result:
        return Result(returncode=0, stdout="{}", stderr="")

    with patch.object(openspec, "cli_available", return_value=True), patch.object(
        openspec.shell,
        "run",
        side_effect=_run,
    ):
        handoff = openspec.build_implementation_handoff(
            tmp_path,
            timeout_sec=10,
            include_skill_bodies=False,
        )

    assert ".codex/skills/openspec-apply-change/SKILL.md" in handoff
    assert "Read the relevant OpenSpec skill files" in handoff
    assert "Apply tasks only" not in handoff


def test_build_implementation_handoff_does_not_guess_multiple_changes(
    tmp_path: Path,
) -> None:
    for change in ("add-api", "add-ui"):
        tasks = tmp_path / "openspec" / "changes" / change / "tasks.md"
        tasks.parent.mkdir(parents=True)
        tasks.write_text("- [ ] implement", encoding="utf-8")
    seen: list[list[str]] = []

    def _run(cmd: list[str], **kwargs: object) -> Result:
        seen.append(cmd)
        return Result(returncode=0, stdout="{}", stderr="")

    with patch.object(openspec, "cli_available", return_value=True), patch.object(
        openspec.shell,
        "run",
        side_effect=_run,
    ):
        handoff = openspec.build_implementation_handoff(tmp_path, timeout_sec=10)

    assert seen == [
        ["openspec", "status", "--json"],
        ["openspec", "instructions", "--json"],
    ]
    assert "multiple changes exist" in handoff
    assert "`add-api`" in handoff
    assert "`add-ui`" in handoff


def test_summarize_truncates_large_json_payload() -> None:
    summary = openspec._summarize({"text": "x" * (openspec.MAX_CONTEXT_CHARS + 100)})
    lines = openspec.format_context(
        {
            "present": True,
            "cli_available": True,
            "commands": {
                "status": {
                    "cmd": "openspec status --json",
                    "ok": True,
                    "rc": 0,
                    "summary": summary,
                }
            },
        }
    )

    rendered = "\n".join(lines)
    assert "### OpenSpec" in rendered
    assert rendered.endswith("...")
    assert len(rendered) < openspec.MAX_CONTEXT_CHARS + 200
