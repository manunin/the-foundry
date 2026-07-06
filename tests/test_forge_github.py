from __future__ import annotations

import json
from pathlib import Path

from foundry.forges import IssueQuery
from foundry.forges.github import GitHubProvider
from foundry.shell import Result


def test_github_maps_issues_and_preserves_filters(monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs) -> Result:
        commands.append(cmd)
        return Result(
            0,
            json.dumps([{
                "number": 3, "title": "Fix", "body": None,
                "labels": [{"name": "priority/p0"}],
            }]),
            "",
        )

    monkeypatch.setattr("foundry.forges.base.shell.run", fake_run)
    issues = GitHubProvider().list_issues(
        "owner/repo",
        IssueQuery(("agent-task",), "octocat", "v1", 25),
    )

    assert issues[0].labels == ("priority/p0",)
    assert issues[0].url == "https://github.com/owner/repo/issues/3"
    assert commands[0][-6:] == [
        "--label", "agent-task", "--assignee", "octocat", "--milestone", "v1"
    ]


def test_github_clone_uses_cli(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        "foundry.forges.base.shell.run",
        lambda cmd, **kwargs: commands.append(cmd) or Result(0, "", ""),
    )

    GitHubProvider().clone("owner/repo", tmp_path / "base")

    assert commands == [[
        "gh", "repo", "clone", "owner/repo", str(tmp_path / "base"),
        "--", "--no-checkout",
    ]]


def test_github_maps_issue_comments(monkeypatch) -> None:
    monkeypatch.setattr(
        "foundry.forges.base.shell.run",
        lambda cmd, **kwargs: Result(
            0,
            json.dumps({
                "comments": [{
                    "id": "IC_1",
                    "body": "Use test-foundry.md",
                    "createdAt": "2026-07-01T17:00:00Z",
                    "author": {"login": "maintainer"},
                }]
            }),
            "",
        ),
    )

    comments = GitHubProvider().list_issue_comments("owner/repo", 3)

    assert len(comments) == 1
    assert comments[0].external_id == "IC_1"
    assert comments[0].author == "maintainer"
