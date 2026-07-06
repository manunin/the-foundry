from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

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


class GitLabProvider:
    kind = ForgeKind.GITLAB

    def __init__(self, host: str = "gitlab.com") -> None:
        self.host = host

    def _api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        fields: dict[str, str] | None = None,
    ) -> object:
        cmd = ["glab", "api", "--hostname", self.host, "--method", method, endpoint]
        for key, value in (fields or {}).items():
            cmd.extend(["--raw-field", f"{key}={value}"])
        return parse_json(
            run_with_retry(cmd),
            operation=f"{method} {endpoint.split('?')[0]}",
            project=endpoint,
        )

    @staticmethod
    def _project(project: str) -> str:
        return quote(project, safe="")

    def _paged(self, endpoint: str) -> list[object]:
        values: list[object] = []
        page = 1
        separator = "&" if "?" in endpoint else "?"
        while True:
            data = self._api(f"{endpoint}{separator}per_page=100&page={page}")
            if not isinstance(data, list):
                raise ForgeError(f"paginated response is invalid for {endpoint}")
            values.extend(data)
            if len(data) < 100:
                return values
            page += 1

    def list_issues(self, project: str, query: IssueQuery) -> list[ForgeIssue]:
        results: list[ForgeIssue] = []
        page = 1
        while len(results) < query.limit:
            params: list[tuple[str, str]] = [
                ("state", "opened"),
                ("per_page", str(min(100, query.limit))),
                ("page", str(page)),
            ]
            if query.labels:
                params.append(("labels", ",".join(query.labels)))
            if query.assignee:
                params.append(("assignee_username", query.assignee))
            if query.milestone:
                params.append(("milestone", query.milestone))
            endpoint = f"/projects/{self._project(project)}/issues?{urlencode(params)}"
            data = self._api(endpoint)
            if not isinstance(data, list):
                raise ForgeError(f"list issues returned invalid JSON for {project}")
            results.extend(self._issue(item, project) for item in data)
            if len(data) < min(100, query.limit):
                break
            page += 1
        return results[:query.limit]

    def get_issue(self, project: str, number: int) -> ForgeIssue:
        return self._issue(
            self._api(f"/projects/{self._project(project)}/issues/{number}"), project
        )

    def _issue(self, value: object, project: str) -> ForgeIssue:
        if not isinstance(value, dict):
            raise ForgeError(f"issue response is invalid for {project}")
        try:
            labels_value = value.get("labels") or []
            if not isinstance(labels_value, list):
                raise TypeError
            return ForgeIssue(
                int(value["iid"]),
                str(value["title"]),
                str(value.get("description") or ""),
                tuple(str(label) for label in labels_value),
                str(value["web_url"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ForgeError(f"issue response missing fields for {project}") from exc

    def comment_issue(self, project: str, number: int, body: str) -> None:
        self._api(
            f"/projects/{self._project(project)}/issues/{number}/notes",
            method="POST", fields={"body": body},
        )

    def list_issue_comments(
        self, project: str, number: int
    ) -> list[ForgeComment]:
        endpoint = (
            f"/projects/{self._project(project)}/issues/{number}/notes"
            "?sort=asc&order_by=created_at"
        )
        comments: list[ForgeComment] = []
        for value in self._paged(endpoint):
            if not isinstance(value, dict) or value.get("system"):
                continue
            author = value.get("author") or {}
            comments.append(
                ForgeComment(
                    external_id=str(value.get("id") or ""),
                    body=str(value.get("body") or "").strip(),
                    author=(
                        str(author.get("username") or "unknown")
                        if isinstance(author, dict)
                        else "unknown"
                    ),
                    created_at_ms=_timestamp_ms(value.get("created_at"), project),
                )
            )
        return comments

    def close_issue(self, project: str, number: int, comment: str) -> None:
        self.comment_issue(project, number, comment)
        self._api(
            f"/projects/{self._project(project)}/issues/{number}",
            method="PUT", fields={"state_event": "close"},
        )

    def clone(self, project: str, destination: Path) -> None:
        run_with_retry(
            [
                "glab",
                "repo",
                "clone",
                project,
                str(destination),
                "--",
                "--no-checkout",
            ]
        )

    def create_change(
        self, project: str, change: ChangeRequestInput
    ) -> ForgeChange:
        value = self._api(
            f"/projects/{self._project(project)}/merge_requests",
            method="POST",
            fields={
                "source_branch": change.branch,
                "target_branch": change.base_branch,
                "title": change.title,
                "description": change.body,
            },
        )
        return self._change(value, project)

    def list_changes(self, project: str) -> list[ForgeChange]:
        endpoint = f"/projects/{self._project(project)}/merge_requests?state=opened"
        data = self._paged(endpoint)
        changes = [self._change(item, project) for item in data]
        return [change for change in changes if change.branch.startswith("foundry/task-")]

    def _change(self, value: object, project: str) -> ForgeChange:
        if not isinstance(value, dict):
            raise ForgeError(f"change response is invalid for {project}")
        try:
            diff_refs = value.get("diff_refs") or {}
            sha = value.get("sha") or (
                diff_refs.get("head_sha") if isinstance(diff_refs, dict) else None
            )
            return ForgeChange(
                int(value["iid"]),
                str(value["title"]),
                str(value["source_branch"]),
                str(value["web_url"]),
                str(sha) if sha else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ForgeError(f"change response missing fields for {project}") from exc

    def load_feedback(
        self, project: str, change: ForgeChange
    ) -> ChangeFeedback:
        base = f"/projects/{self._project(project)}/merge_requests/{change.number}"
        discussions = self._paged(f"{base}/discussions")
        pipelines = self._paged(f"{base}/pipelines")
        items: list[FeedbackItem] = []
        for discussion in discussions:
            if not isinstance(discussion, dict):
                continue
            for note in discussion.get("notes", []):
                if not isinstance(note, dict):
                    continue
                if not note.get("resolvable") or note.get("resolved") or note.get("system"):
                    continue
                author = note.get("author") or {}
                items.append(
                    FeedbackItem(
                        str(note.get("id") or discussion.get("id")),
                        str(note.get("body") or "requested changes").strip(),
                        str(author.get("username") or "unknown")
                        if isinstance(author, dict)
                        else "unknown",
                    )
                )
        checks: list[CheckResult] = []
        for pipeline in pipelines:
            if not isinstance(pipeline, dict):
                continue
            state = str(pipeline.get("status") or "").lower()
            sha = str(pipeline.get("sha") or "")
            if (
                state in {"failed", "canceled"}
                and change.head_sha
                and sha == change.head_sha
            ):
                checks.append(
                    CheckResult(
                        str(pipeline.get("id") or sha), "pipeline", state.upper()
                    )
                )
        return ChangeFeedback(tuple(items), tuple(checks))

    def comment_change(self, project: str, number: int, body: str) -> None:
        self._api(
            f"/projects/{self._project(project)}/merge_requests/{number}/notes",
            method="POST", fields={"body": body},
        )


def _timestamp_ms(value: object, project: str) -> int:
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ForgeError(
            f"issue comment has invalid timestamp for {project}"
        ) from exc
    return int(timestamp.timestamp() * 1000)
