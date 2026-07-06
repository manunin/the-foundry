from __future__ import annotations

from datetime import datetime
from pathlib import Path

from foundry.forges.base import (
    ChangeFeedback,
    ChangeRequestInput,
    CheckResult,
    FeedbackItem,
    ForgeChange,
    ForgeComment,
    ForgeError,
    ForgeIssue,
    IssueQuery,
    parse_json,
    run_with_retry,
)
from foundry.models import ForgeKind


class GitHubProvider:
    kind = ForgeKind.GITHUB

    def __init__(self, host: str = "github.com") -> None:
        self.host = host

    def _host_args(self) -> list[str]:
        return []

    def list_issues(self, project: str, query: IssueQuery) -> list[ForgeIssue]:
        cmd = [
            "gh", "issue", "list", "--repo", project, "--state", "open",
            "--json", "number,title,body,labels", "--limit", str(query.limit),
        ]
        for label in query.labels:
            cmd.extend(["--label", label])
        if query.assignee:
            cmd.extend(["--assignee", query.assignee])
        if query.milestone:
            cmd.extend(["--milestone", query.milestone])
        cmd.extend(self._host_args())
        data = parse_json(run_with_retry(cmd), operation="list issues", project=project)
        if not isinstance(data, list):
            raise ForgeError(f"list issues returned invalid JSON for {project}")
        return [self._issue(item, project) for item in data]

    def get_issue(self, project: str, number: int) -> ForgeIssue:
        cmd = [
            "gh", "issue", "view", str(number), "--repo", project,
            "--json", "number,title,body", *self._host_args(),
        ]
        return self._issue(
            parse_json(run_with_retry(cmd), operation="get issue", project=project),
            project,
        )

    def _issue(self, value: object, project: str) -> ForgeIssue:
        if not isinstance(value, dict):
            raise ForgeError(f"issue response is invalid for {project}")
        try:
            labels = tuple(
                str(label["name"]) for label in value.get("labels", []) if isinstance(label, dict)
            )
            return ForgeIssue(
                number=int(value["number"]),
                title=str(value["title"]),
                body=str(value.get("body") or ""),
                labels=labels,
                url=str(
                    value.get("url")
                    or f"https://{self.host}/{project}/issues/{value['number']}"
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ForgeError(f"issue response missing fields for {project}") from exc

    def comment_issue(self, project: str, number: int, body: str) -> None:
        run_with_retry([
            "gh", "issue", "comment", str(number), "--repo", project,
            "--body", body, *self._host_args(),
        ])

    def list_issue_comments(
        self, project: str, number: int
    ) -> list[ForgeComment]:
        cmd = [
            "gh",
            "issue",
            "view",
            str(number),
            "--repo",
            project,
            "--json",
            "comments",
            *self._host_args(),
        ]
        data = parse_json(
            run_with_retry(cmd), operation="list issue comments", project=project
        )
        if not isinstance(data, dict) or not isinstance(data.get("comments"), list):
            raise ForgeError(f"issue comments response is invalid for {project}")
        comments: list[ForgeComment] = []
        for index, value in enumerate(data["comments"]):
            if not isinstance(value, dict):
                continue
            author = value.get("author") or {}
            comments.append(
                ForgeComment(
                    external_id=str(value.get("id") or f"comment-{index}"),
                    body=str(value.get("body") or "").strip(),
                    author=(
                        str(author.get("login") or "unknown")
                        if isinstance(author, dict)
                        else "unknown"
                    ),
                    created_at_ms=_timestamp_ms(value.get("createdAt"), project),
                )
            )
        return comments

    def close_issue(self, project: str, number: int, comment: str) -> None:
        run_with_retry([
            "gh", "issue", "close", str(number), "--repo", project,
            "--comment", comment, *self._host_args(),
        ])

    def clone(self, project: str, destination: Path) -> None:
        run_with_retry([
            "gh", "repo", "clone", project, str(destination),
            *self._host_args(), "--", "--no-checkout",
        ])

    def create_change(
        self, project: str, change: ChangeRequestInput
    ) -> ForgeChange:
        result = run_with_retry([
            "gh", "pr", "create", "--repo", project, "--head", change.branch,
            "--base", change.base_branch, "--title", change.title, "--body", change.body,
            *self._host_args(),
        ])
        url = result.stdout.strip().splitlines()[-1]
        if not url:
            raise ForgeError(f"create change returned no URL for {project}")
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
        return ForgeChange(number, change.title, change.branch, url)

    def list_changes(self, project: str) -> list[ForgeChange]:
        cmd = [
            "gh", "pr", "list", "--repo", project, "--state", "open",
            "--json", "number,title,headRefName,url,headRefOid", "--limit", "100",
            *self._host_args(),
        ]
        data = parse_json(run_with_retry(cmd), operation="list changes", project=project)
        if not isinstance(data, list):
            raise ForgeError(f"list changes returned invalid JSON for {project}")
        changes = [self._change(item, project) for item in data]
        return [change for change in changes if change.branch.startswith("foundry/task-")]

    def _change(self, value: object, project: str) -> ForgeChange:
        if not isinstance(value, dict):
            raise ForgeError(f"change response is invalid for {project}")
        try:
            return ForgeChange(
                int(value["number"]), str(value["title"]), str(value["headRefName"]),
                str(value["url"]), str(value.get("headRefOid") or "") or None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ForgeError(f"change response missing fields for {project}") from exc

    def load_feedback(
        self, project: str, change: ForgeChange
    ) -> ChangeFeedback:
        cmd = [
            "gh", "pr", "view", str(change.number), "--repo", project, "--json",
            "number,title,headRefName,url,headRefOid,reviews,comments,statusCheckRollup",
            *self._host_args(),
        ]
        data = parse_json(run_with_retry(cmd), operation="load feedback", project=project)
        if not isinstance(data, dict):
            raise ForgeError(f"feedback response is invalid for {project}")
        comments = [
            str(item.get("body") or "").strip()
            for item in data.get("comments", [])
            if isinstance(item, dict) and str(item.get("body") or "").strip()
        ][-5:]
        items: list[FeedbackItem] = []
        for index, review in enumerate(data.get("reviews", [])):
            if not isinstance(review, dict) or str(review.get("state")).upper() != "CHANGES_REQUESTED":
                continue
            body = str(review.get("body") or "requested changes").strip()
            if comments:
                body += "\n\nContext:\n" + "\n\n".join(comments)
            author = review.get("author") or {}
            items.append(FeedbackItem(
                str(review.get("id") or f"review-{index}"), body,
                str(author.get("login") or "unknown") if isinstance(author, dict) else "unknown",
            ))
        checks: list[CheckResult] = []
        failure_states = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "ERROR", "FAILED"}
        for index, check in enumerate(data.get("statusCheckRollup", [])):
            if not isinstance(check, dict):
                continue
            state = str(check.get("conclusion") or check.get("status") or check.get("state") or "").upper()
            if state in failure_states:
                name = str(check.get("name") or check.get("context") or check.get("workflowName") or "check")
                checks.append(CheckResult(str(check.get("id") or f"{name}-{index}"), name, state))
        return ChangeFeedback(tuple(items), tuple(checks))

    def comment_change(self, project: str, number: int, body: str) -> None:
        run_with_retry([
            "gh", "pr", "comment", str(number), "--repo", project,
            "--body", body, *self._host_args(),
        ])


def _timestamp_ms(value: object, project: str) -> int:
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForgeError(
            f"issue comment has invalid timestamp for {project}"
        ) from exc
    return int(timestamp.timestamp() * 1000)
