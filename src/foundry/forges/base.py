from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog

from foundry import shell
from foundry.models import ForgeKind


class ForgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class IssueQuery:
    labels: tuple[str, ...] = ()
    assignee: str | None = None
    milestone: str | None = None
    limit: int = 50


@dataclass(frozen=True)
class ForgeIssue:
    number: int
    title: str
    body: str
    labels: tuple[str, ...]
    url: str


@dataclass(frozen=True)
class ForgeComment:
    external_id: str
    body: str
    author: str
    created_at_ms: int


@dataclass(frozen=True)
class ChangeRequestInput:
    title: str
    body: str
    branch: str
    base_branch: str


@dataclass(frozen=True)
class ForgeChange:
    number: int
    title: str
    branch: str
    url: str
    head_sha: str | None = None


@dataclass(frozen=True)
class FeedbackItem:
    external_id: str
    body: str
    author: str = "unknown"
    file_path: str | None = None
    line: int | None = None

    @property
    def location(self) -> str | None:
        if not self.file_path:
            return None
        if self.line is None:
            return self.file_path
        return f"{self.file_path}:{self.line}"


@dataclass(frozen=True)
class CheckResult:
    external_id: str
    name: str
    state: str
    url: str | None = None
    details: str | None = None


TRACK_CI_FEEDBACK = True


@dataclass(frozen=True)
class ChangeFeedback:
    items: tuple[FeedbackItem, ...] = ()
    failing_checks: tuple[CheckResult, ...] = ()

    @property
    def actionable(self) -> bool:
        return bool(self.items or (TRACK_CI_FEEDBACK and self.failing_checks))

    @property
    def fingerprint(self) -> str:
        values = [
            f"item:{item.external_id}:{item.location or ''}:{item.body}"
            for item in self.items
        ]
        if TRACK_CI_FEEDBACK:
            values.extend(
                ":".join(
                    [
                        "check",
                        check.external_id,
                        check.state,
                        check.url or "",
                        check.details or "",
                    ]
                )
                for check in self.failing_checks
            )
        canonical = "\n".join(sorted(values))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def format(self) -> str:
        parts: list[str] = []
        if self.items:
            lines = ["### Requested changes"]
            for item in self.items:
                location = f" on `{item.location}`" if item.location else ""
                lines.append(f"- {item.author}{location}: {item.body}")
            parts.append("\n".join(lines))
        if TRACK_CI_FEEDBACK and self.failing_checks:
            lines = ["### Failing CI"]
            for check in self.failing_checks:
                suffix = f" ({check.url})" if check.url else ""
                lines.append(f"- {check.name}: {check.state}{suffix}")
                if check.details:
                    lines.append(f"  {check.details}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)


class ForgeProvider(Protocol):
    kind: ForgeKind
    host: str

    def list_issues(self, project: str, query: IssueQuery) -> list[ForgeIssue]: ...
    def get_issue(self, project: str, number: int) -> ForgeIssue: ...
    def comment_issue(self, project: str, number: int, body: str) -> None: ...
    def list_issue_comments(
        self, project: str, number: int
    ) -> list[ForgeComment]: ...
    def close_issue(self, project: str, number: int, comment: str) -> None: ...
    def clone(self, project: str, destination: Path) -> None: ...
    def create_change(
        self, project: str, change: ChangeRequestInput
    ) -> ForgeChange: ...
    def list_changes(self, project: str) -> list[ForgeChange]: ...
    def load_feedback(
        self, project: str, change: ForgeChange
    ) -> ChangeFeedback: ...
    def comment_change(self, project: str, number: int, body: str) -> None: ...


def run_with_retry(
    cmd: list[str], *, retries: int = 3, backoff: float = 1.0, cwd: Path | None = None
) -> shell.Result:
    for attempt in range(retries):
        try:
            if cwd is None:
                return shell.run(cmd)
            return shell.run(cmd, cwd=cwd)
        except shell.ShellError as exc:
            message = f"{exc.stderr}\n{exc.stdout}".lower()
            transient = any(
                marker in message
                for marker in (
                    "timeout",
                    "connection",
                    "temporarily unavailable",
                    "rate limit",
                    "429",
                )
            )
            if not transient or attempt == retries - 1:
                raise
            structlog.get_logger().warning(
                "forge.network_retry", attempt=attempt + 1, command=cmd[:2]
            )
            time.sleep(backoff * (attempt + 1))
    raise AssertionError("retry loop exhausted")


def parse_json(result: shell.Result, *, operation: str, project: str) -> object:
    if not result.stdout.strip():
        raise ForgeError(f"{operation} returned empty JSON for {project}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ForgeError(f"{operation} returned malformed JSON for {project}") from exc
