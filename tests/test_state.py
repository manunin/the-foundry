from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from foundry import state
from foundry.models import Stage, Task, TaskStatus


def _make_task(issue_number: int = 1) -> Task:
    return Task(
        repo="owner/repo",
        issue_number=issue_number,
        issue_title=f"issue {issue_number}",
        issue_body="body",
    )


def test_init_creates_schema(tmp_path: Path) -> None:
    db = tmp_path / "foundry.sqlite"
    state.init_db(db)
    assert db.exists()
    # running twice must be idempotent
    state.init_db(db)


def test_init_retries_transient_disk_io_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "foundry.sqlite"
    real_connect = sqlite3.connect
    attempts = 0
    sleeps: list[float] = []

    def flaky_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(state.sqlite3, "connect", flaky_connect)
    monkeypatch.setattr(state.time, "sleep", sleeps.append)

    state.init_db(db)

    assert db.exists()
    assert attempts == 2
    assert sleeps == [0.2]


def test_connect_retries_transient_commit_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    class FlakyConnection:
        def __init__(self) -> None:
            self.attempts = 0

        def commit(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(state.time, "sleep", sleeps.append)
    conn = FlakyConnection()

    state._commit_with_retry(conn)  # type: ignore[arg-type]

    assert conn.attempts == 2
    assert sleeps == [0.2]


def test_upsert_insert_then_update(tmp_path: Path) -> None:
    db = tmp_path / "f.sqlite"
    state.init_db(db)

    task = _make_task()
    saved = state.upsert_task(db, task)
    assert saved.id is not None

    saved.status = TaskStatus.RUNNING
    saved.current_stage = Stage.IMPLEMENT
    state.upsert_task(db, saved)

    fetched = state.get_task_by_issue(db, "owner/repo", 1)
    assert fetched is not None
    assert fetched.status == TaskStatus.RUNNING
    assert fetched.current_stage == Stage.IMPLEMENT


def test_list_tasks_filter_by_status(tmp_path: Path) -> None:
    db = tmp_path / "f.sqlite"
    state.init_db(db)

    state.upsert_task(db, _make_task(1))
    t2 = state.upsert_task(db, _make_task(2))
    t2.status = TaskStatus.DONE
    state.upsert_task(db, t2)

    pending = state.list_tasks(db, TaskStatus.PENDING)
    done = state.list_tasks(db, TaskStatus.DONE)
    assert [t.issue_number for t in pending] == [1]
    assert [t.issue_number for t in done] == [2]


def test_list_tasks_sorted_desc_by_id(tmp_path: Path) -> None:
    db = tmp_path / "f.sqlite"
    state.init_db(db)

    t1 = state.upsert_task(db, _make_task(1))
    t2 = state.upsert_task(db, _make_task(2))
    t3 = state.upsert_task(db, _make_task(3))

    tasks = state.list_tasks(db)

    assert [t.id for t in tasks] == [t3.id, t2.id, t1.id]


def test_append_log_accumulates(tmp_path: Path) -> None:
    db = tmp_path / "f.sqlite"
    state.init_db(db)
    task = state.upsert_task(db, _make_task())

    state.append_log(db, task.id, Stage.PLAN, {"steps": 1})
    state.append_log(db, task.id, Stage.IMPLEMENT, {"ok": True})

    fetched = state.get_task(db, task.id)
    logs = json.loads(fetched.logs_json)
    assert len(logs) == 2
    assert logs[0]["stage"] == "plan"
    assert logs[1]["stage"] == "implement"


def test_stage_results_round_trip_by_attempt(tmp_path: Path) -> None:
    db = tmp_path / "f.sqlite"
    state.init_db(db)
    task = state.upsert_task(db, _make_task())

    state.save_stage_result(db, task.id, Stage.PLAN, {"plan": "do it"})
    state.save_stage_result(
        db, task.id, Stage.IMPLEMENT, {"result": "changed"}, attempt=2
    )

    assert state.get_stage_result(db, task.id, Stage.PLAN) == {"plan": "do it"}
    assert state.get_stage_result(
        db, task.id, Stage.IMPLEMENT, attempt=2
    ) == {"result": "changed"}
    assert state.get_latest_stage_result(db, task.id, Stage.IMPLEMENT) == (
        2,
        {"result": "changed"},
    )
    assert state.list_stage_results(db, task.id, Stage.IMPLEMENT) == [
        (2, {"result": "changed"})
    ]


def test_repo_memory_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "f.sqlite"
    state.init_db(db)

    state.save_repo_memory(db, "owner/repo", "touched_files", ["src/app.py"])
    state.save_repo_memory(db, "owner/repo", "touched_files", ["src/api.py"])
    state.save_repo_memory(db, "owner/repo", "verify_commands", ["pytest -q"])

    assert state.get_repo_memory(db, "owner/repo", "touched_files") == ["src/api.py"]
    entries = state.list_repo_memory(db, "owner/repo")
    assert [(e["key"], e["value"]) for e in entries] == [
        ("touched_files", ["src/api.py"]),
        ("verify_commands", ["pytest -q"]),
    ]
    assert all(e["updated_at"] for e in entries)


def test_agent_sessions_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "f.sqlite"
    state.init_db(db)
    task = state.upsert_task(db, _make_task())

    state.save_agent_session(db, task.id, "implement", "claude_cli", "sess-1")
    state.save_agent_session(db, task.id, "implement", "claude_cli", "sess-2")

    assert (
        state.get_agent_session(db, task.id, "implement", "claude_cli") == "sess-2"
    )


def test_init_migrates_legacy_tasks_idempotently(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo TEXT NOT NULL, issue_number INTEGER NOT NULL,
                issue_title TEXT NOT NULL, issue_body TEXT NOT NULL,
                status TEXT NOT NULL, current_stage TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, worktree_path TEXT,
                branch_name TEXT, pr_url TEXT, logs_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE (repo, issue_number)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO tasks (
                repo, issue_number, issue_title, issue_body, status,
                current_stage, created_at, updated_at
            ) VALUES ('owner/repo', 7, 'legacy', '', 'done', 'done', 'now', 'now')
            """
        )

    state.init_db(db)
    state.init_db(db)

    task = state.get_task(db, 1)
    assert task is not None
    assert task.issue_title == "legacy"
    assert task.forge.value == "github"
    assert task.forge_host == "github.com"
    assert task.issue_url is None


def test_resume_with_clarification_preserves_upstream_stage(tmp_path: Path) -> None:
    db = tmp_path / "foundry.sqlite"
    state.init_db(db)
    task = state.upsert_task(
        db,
        Task(
            repo="owner/repo",
            issue_number=9,
            issue_title="Clarify",
            issue_body="Original",
            status=TaskStatus.BLOCKED,
            current_stage=Stage.PLAN,
            worktree_path="/worktrees/task-1",
            branch_name="foundry/task-1",
        ),
    )
    state.save_stage_result(db, task.id, Stage.CONTEXT, {"context": "cached"})
    state.save_stage_result(db, task.id, Stage.PLAN, {"plan": "draft"})
    state.save_stage_result(db, task.id, Stage.IMPLEMENT, {"result": "stale"})
    task.issue_body = "Original\n\nClarification"

    resumed = state.resume_task_with_clarification(db, task)

    assert resumed.status == TaskStatus.PENDING
    assert resumed.current_stage == Stage.PLAN
    assert resumed.worktree_path == "/worktrees/task-1"
    assert state.get_stage_result(db, task.id, Stage.CONTEXT) == {
        "context": "cached"
    }
    assert state.get_stage_result(db, task.id, Stage.PLAN) is None
    assert state.get_stage_result(db, task.id, Stage.IMPLEMENT) is None
