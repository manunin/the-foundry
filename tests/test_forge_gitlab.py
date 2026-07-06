from __future__ import annotations

import json

from foundry.forges import ForgeChange, IssueQuery
from foundry.forges.gitlab import GitLabProvider
from foundry.shell import Result


def test_gitlab_encodes_nested_project_and_maps_string_labels(monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs) -> Result:
        commands.append(cmd)
        return Result(0, json.dumps([{
            "iid": 4, "title": "Fix", "description": "Body",
            "labels": ["agent-task", "priority/p1"],
            "web_url": "https://gitlab.example/group/sub/repo/-/issues/4",
        }]), "")

    monkeypatch.setattr("foundry.forges.base.shell.run", fake_run)
    issues = GitLabProvider("gitlab.example").list_issues(
        "group/sub/repo", IssueQuery(("agent-task", "queue/backend"), limit=20)
    )

    assert issues[0].number == 4
    assert issues[0].labels == ("agent-task", "priority/p1")
    assert "/projects/group%2Fsub%2Frepo/issues?" in commands[0][-1]
    assert "labels=agent-task%2Cqueue%2Fbackend" in commands[0][-1]


def test_gitlab_feedback_uses_unresolved_discussions_and_current_sha(
    monkeypatch,
) -> None:
    responses = iter([
        [
            {"id": "thread-1", "notes": [{
                "id": 10, "body": "Add a test", "resolvable": True,
                "resolved": False, "system": False,
                "author": {"username": "reviewer"},
            }]},
            {"id": "thread-2", "notes": [{
                "id": 11, "body": "Already fixed", "resolvable": True,
                "resolved": True, "system": False,
            }]},
        ],
        [
            {"id": 20, "sha": "old", "status": "failed"},
            {"id": 21, "sha": "head", "status": "canceled"},
            {"id": 22, "sha": "head", "status": "running"},
        ],
    ])

    def fake_run(cmd: list[str], **kwargs) -> Result:
        return Result(0, json.dumps(next(responses)), "")

    monkeypatch.setattr("foundry.forges.base.shell.run", fake_run)
    feedback = GitLabProvider().load_feedback(
        "group/repo",
        ForgeChange(7, "Change", "foundry/task-1", "https://example/mr/7", "head"),
    )

    assert [item.external_id for item in feedback.items] == ["10"]
    assert [check.external_id for check in feedback.failing_checks] == ["21"]
    assert "Add a test" in feedback.format()


def test_gitlab_maps_issue_comments_and_skips_system_notes(monkeypatch) -> None:
    def fake_run(cmd: list[str], **kwargs) -> Result:
        return Result(0, json.dumps([
            {
                "id": 11,
                "body": "Use test-foundry.md",
                "created_at": "2026-07-01T17:00:00Z",
                "system": False,
                "author": {"username": "maintainer"},
            },
            {
                "id": 12,
                "body": "changed title",
                "created_at": "2026-07-01T17:01:00Z",
                "system": True,
            },
        ]), "")

    monkeypatch.setattr("foundry.forges.base.shell.run", fake_run)

    comments = GitLabProvider().list_issue_comments("group/repo", 3)

    assert len(comments) == 1
    assert comments[0].external_id == "11"
    assert comments[0].author == "maintainer"
    assert comments[0].created_at_ms == 1_782_925_200_000
