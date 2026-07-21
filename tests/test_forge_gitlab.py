from __future__ import annotations

import json
from unittest.mock import MagicMock

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


def test_gitlab_http_host_uses_direct_api_without_glab(monkeypatch) -> None:
    monkeypatch.setenv("GITLAB_HOST", "http://gitlab.example")
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps([
        {
            "iid": 4,
            "title": "Fix",
            "description": "Body",
            "labels": ["agent-task"],
            "web_url": "http://gitlab.example/group/repo/-/issues/4",
        }
    ]).encode()
    requests = []

    def fake_urlopen(request, timeout: int):
        requests.append(request)
        assert timeout == 30
        return response

    monkeypatch.setattr("foundry.forges.gitlab.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "foundry.forges.base.shell.run",
        lambda cmd, **kwargs: (_ for _ in ()).throw(AssertionError("glab called")),
    )

    issues = GitLabProvider("gitlab.example").list_issues(
        "group/repo",
        IssueQuery(("agent-task",), limit=50),
    )

    assert issues[0].number == 4
    assert requests[0].full_url == (
        "http://gitlab.example/api/v4/projects/group%2Frepo/issues"
        "?state=opened&per_page=50&page=1&labels=agent-task"
    )
    assert requests[0].headers["Private-token"] == "token"


def test_gitlab_issue_includes_text_attachment_content(monkeypatch) -> None:
    provider = GitLabProvider("gitlab.example")
    monkeypatch.setattr(
        provider,
        "_api",
        lambda endpoint: {
            "iid": 4,
            "title": "Fix failure",
            "description": (
                "See [error.log](/uploads/abc123/error.log) and "
                "[again](/uploads/abc123/error.log)."
            ),
            "labels": ["agent-task"],
            "web_url": "https://gitlab.example/group/repo/-/issues/4",
        },
    )
    endpoints: list[str] = []

    def raw_api(endpoint: str) -> str:
        endpoints.append(endpoint)
        return "NullPointerException at Handler.java:42"

    monkeypatch.setattr(provider, "_raw_api", raw_api)

    issue = provider.get_issue("group/sub repo", 4)

    assert "## GitLab issue attachments" in issue.body
    assert "### `error.log`" in issue.body
    assert "NullPointerException at Handler.java:42" in issue.body
    assert endpoints == [
        "/projects/group%2Fsub%20repo/uploads/abc123/error.log"
    ]


def test_gitlab_issue_marks_binary_attachment_without_downloading(
    monkeypatch,
) -> None:
    provider = GitLabProvider("gitlab.example")
    raw_api = MagicMock()
    monkeypatch.setattr(provider, "_raw_api", raw_api)

    issue = provider._issue(
        {
            "iid": 4,
            "title": "Screenshot",
            "description": "![failure](/uploads/abc123/failure.png)",
            "labels": ["agent-task"],
            "web_url": "https://gitlab.example/group/repo/-/issues/4",
        },
        "group/repo",
    )

    assert "Binary or unsupported attachment" in issue.body
    assert "failure.png" in issue.body
    raw_api.assert_not_called()


def test_gitlab_issue_truncates_large_text_attachment(monkeypatch) -> None:
    provider = GitLabProvider("gitlab.example")
    monkeypatch.setattr(
        provider,
        "_raw_api",
        lambda endpoint: "x" * 60_000,
    )

    issue = provider._issue(
        {
            "iid": 4,
            "title": "Large log",
            "description": "[large.log](/uploads/abc123/large.log)",
            "labels": ["agent-task"],
            "web_url": "https://gitlab.example/group/repo/-/issues/4",
        },
        "group/repo",
    )

    assert "Attachment content truncated by The Foundry" in issue.body
    assert "x" * 50_000 in issue.body


def test_gitlab_feedback_uses_unresolved_discussions_and_current_sha(
    monkeypatch,
) -> None:
    responses = iter([
        [
            {"id": "thread-1", "notes": [{
                "id": 10, "body": "Add a test", "resolvable": True,
                "resolved": False, "system": False,
                "author": {"username": "reviewer"},
                "position": {
                    "new_path": "openspec/changes/digital-ruble-xml-over-http/tasks.md",
                    "new_line": 12,
                },
            }]},
            {"id": "thread-2", "notes": [{
                "id": 11, "body": "Already fixed", "resolvable": True,
                "resolved": True, "system": False,
            }]},
        ],
        [
            {"id": 20, "sha": "old", "status": "failed", "web_url": "old-url"},
            {
                "id": 21,
                "sha": "head",
                "status": "canceled",
                "web_url": "https://gitlab.example/pipelines/21",
            },
            {"id": 22, "sha": "head", "status": "running"},
        ],
        [
            {
                "id": 31,
                "name": "pytest",
                "status": "failed",
                "stage": "test",
                "failure_reason": "script_failure",
                "web_url": "https://gitlab.example/jobs/31",
            },
            {
                "id": 32,
                "name": "ruff",
                "status": "canceled",
                "stage": "lint",
                "web_url": "https://gitlab.example/jobs/32",
            },
        ],
        "Compiling module\n[ERROR] cannot find symbol Foo\nJob failed",
        "Running tests\nAssertionError: expected 1 got 2\nJob failed",
    ])

    def fake_run(cmd: list[str], **kwargs) -> Result:
        response = next(responses)
        stdout = response if isinstance(response, str) else json.dumps(response)
        return Result(0, stdout, "")

    monkeypatch.setattr("foundry.forges.base.shell.run", fake_run)
    feedback = GitLabProvider().load_feedback(
        "group/repo",
        ForgeChange(7, "Change", "foundry/task-1", "https://example/mr/7", "head"),
    )

    assert [item.external_id for item in feedback.items] == ["10"]
    assert feedback.items[0].location == (
        "openspec/changes/digital-ruble-xml-over-http/tasks.md:12"
    )
    assert [check.external_id for check in feedback.failing_checks] == ["21"]
    assert feedback.failing_checks[0].url == "https://gitlab.example/pipelines/21"
    assert feedback.failing_checks[0].details is not None
    assert "pytest: FAILED in stage `test`" in feedback.failing_checks[0].details
    assert "pytest trace excerpt:" in feedback.failing_checks[0].details
    assert "[ERROR] cannot find symbol Foo" in feedback.failing_checks[0].details
    assert "ruff trace excerpt:" in feedback.failing_checks[0].details
    assert "AssertionError: expected 1 got 2" in feedback.failing_checks[0].details
    assert "https://gitlab.example/jobs/31" in feedback.format()
    assert (
        "- reviewer on `openspec/changes/digital-ruble-xml-over-http/tasks.md:12`: "
        "Add a test"
    ) in feedback.format()


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
