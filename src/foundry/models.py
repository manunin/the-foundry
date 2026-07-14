from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class ForgeKind(StrEnum):
    GITHUB = "github"
    GITLAB = "gitlab"


class Stage(StrEnum):
    FETCH = "fetch"
    CONTEXT = "context"
    PLAN = "plan"
    IMPLEMENT = "implement"
    VERIFY = "verify"
    UI_TESTS = "ui_tests"
    ISSUE_COMMENT = "issue_comment"
    PR = "pr"
    DONE = "done"
    FAILED = "failed"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Task:
    repo: str
    issue_number: int
    issue_title: str
    issue_body: str
    id: int | None = None
    status: TaskStatus = TaskStatus.PENDING
    current_stage: Stage = Stage.FETCH
    attempts: int = 0
    worktree_path: str | None = None
    branch_name: str | None = None
    pr_url: str | None = None
    logs_json: str = "[]"
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    forge: ForgeKind = ForgeKind.GITHUB
    forge_host: str = "github.com"
    issue_url: str | None = None
    issue_labels: tuple[str, ...] = ()


@dataclass
class Event:
    id: int
    task_id: int
    seq: int
    stage: str
    kind: str
    ts_ms: int
    payload: dict[str, Any]
