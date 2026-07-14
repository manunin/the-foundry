from __future__ import annotations

import json
from pathlib import Path

from foundry.forges import ForgeChange, IssueQuery
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


def test_github_get_issue_requests_and_preserves_labels(monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs) -> Result:
        commands.append(cmd)
        return Result(
            0,
            json.dumps(
                {
                    "number": 9,
                    "title": "Manual",
                    "body": "Body",
                    "labels": [{"name": "ui-agent-test"}],
                }
            ),
            "",
        )

    monkeypatch.setattr("foundry.forges.base.shell.run", fake_run)

    issue = GitHubProvider().get_issue("owner/repo", 9)

    assert issue.labels == ("ui-agent-test",)
    assert commands[0][-2:] == ["--json", "number,title,body,labels"]


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


def test_github_feedback_includes_check_urls(monkeypatch) -> None:
    monkeypatch.setattr(
        "foundry.forges.base.shell.run",
        lambda cmd, **kwargs: Result(
            0,
            json.dumps({
                "comments": [],
                "reviews": [],
                "statusCheckRollup": [
                    {
                        "id": "check-1",
                        "name": "pytest",
                        "conclusion": "FAILURE",
                        "detailsUrl": "https://github.example/checks/1",
                        "workflowName": "CI",
                    },
                    {"id": "check-2", "name": "ruff", "conclusion": "SUCCESS"},
                ],
            }),
            "",
        ),
    )

    feedback = GitHubProvider().load_feedback(
        "owner/repo",
        ForgeChange(7, "Change", "foundry/task-1", "https://example/pr/7", "head"),
    )

    assert [check.external_id for check in feedback.failing_checks] == ["check-1"]
    assert feedback.failing_checks[0].url == "https://github.example/checks/1"
    assert feedback.failing_checks[0].details == "workflow: CI"
    assert "https://github.example/checks/1" in feedback.format()
