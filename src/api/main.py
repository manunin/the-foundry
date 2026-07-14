from __future__ import annotations

from collections import Counter
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from foundry import state
from foundry.config import ConfigError, load_settings
from foundry.events import read_events
from foundry.models import Stage, TaskStatus

from .projections import UiEvent, UiMemoryEntry, UiTask, alias_stage, project_task
from .sse import router as sse_router

app = FastAPI(title="The Foundry API")
app.include_router(sse_router)


@app.get("/")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


def _settings_or_raise():
    try:
        return load_settings()
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=f"Configuration error: {exc}")


@app.get("/api/tasks", response_model=list[UiTask])
async def get_tasks() -> list[UiTask]:
    """List all tasks with aggregated stage projections (no events)."""
    settings = _settings_or_raise()
    state.init_db(settings.db_path)

    tasks = state.list_tasks(settings.db_path)
    result: list[UiTask] = []
    for task in tasks:
        events = read_events(settings.db_path, task.id) if task.id is not None else []
        memory = state.list_repo_memory(settings.db_path, task.repo)
        result.append(
            project_task(
                task,
                events,
                include_events=False,
                memory=memory,
                ui_test_label=settings.ui_test_label,
            )
        )
    return result


@app.get("/api/tasks/{task_id}", response_model=UiTask)
async def get_task(task_id: int) -> UiTask:
    """Full task projection including the last 200 events."""
    settings = _settings_or_raise()
    state.init_db(settings.db_path)

    task = state.get_task(settings.db_path, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    events = read_events(settings.db_path, task_id)
    memory = state.list_repo_memory(settings.db_path, task.repo)
    return project_task(
        task,
        events,
        include_events=True,
        events_limit=200,
        memory=memory,
        ui_test_label=settings.ui_test_label,
    )


@app.get("/api/tasks/{task_id}/artifacts/{artifact_path:path}")
async def get_task_artifact(task_id: int, artifact_path: str) -> FileResponse:
    settings = _settings_or_raise()
    state.init_db(settings.db_path)
    task = state.get_task(settings.db_path, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    relative = Path(artifact_path)
    if relative.is_absolute() or ".." in relative.parts or not artifact_path:
        raise HTTPException(status_code=404, detail="Artifact not found")
    allowed = {
        str(item.get("artifact_path"))
        for _, result in state.list_stage_results(
            settings.db_path, task_id, Stage.UI_TESTS
        )
        for item in result.get("screenshots", [])
        if isinstance(item, dict) and isinstance(item.get("artifact_path"), str)
    }
    normalized = relative.as_posix()
    if normalized not in allowed:
        raise HTTPException(status_code=404, detail="Artifact not found")
    task_root = (settings.ui_test_artifact_root / f"task-{task_id}").resolve()
    target = (task_root / relative).resolve()
    if not target.is_relative_to(task_root) or not target.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(target.suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(target, media_type=media_type)


@app.get("/api/tasks/{task_id}/event-history", response_model=list[UiEvent])
async def get_task_event_history(
    task_id: int,
    before_seq: int | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[UiEvent]:
    """Return the latest page before a sequence boundary, in chronological order."""
    settings = _settings_or_raise()
    state.init_db(settings.db_path)
    if state.get_task(settings.db_path, task_id) is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    events = read_events(settings.db_path, task_id)
    if before_seq is not None:
        events = [event for event in events if event.seq < before_seq]
    return [
        UiEvent(
            seq=event.seq,
            stage=alias_stage(event.stage),
            kind=event.kind,
            ts_ms=event.ts_ms,
            payload=event.payload,
        )
        for event in events[-limit:]
    ]


@app.post("/api/tasks/{task_id}/reset", response_model=UiTask)
async def reset_task(task_id: int) -> UiTask:
    """Reset a task to pending/fetch so the worker can retry it."""
    return _set_task_pending(task_id, clear_execution=True)


@app.post("/api/tasks/{task_id}/resume", response_model=UiTask)
async def resume_task(task_id: int) -> UiTask:
    """Resume a human-blocked task after someone answered in the issue."""
    return _set_task_pending(task_id, clear_execution=False)


def _set_task_pending(task_id: int, *, clear_execution: bool) -> UiTask:
    settings = _settings_or_raise()
    state.init_db(settings.db_path)

    task = state.get_task(settings.db_path, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if task.status == TaskStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Running tasks cannot be reset")

    if clear_execution:
        task = state.reset_task_execution(settings.db_path, task)
    else:
        task.status = TaskStatus.PENDING
        task.current_stage = Stage.FETCH
        task.pr_url = None
        task = state.upsert_task(settings.db_path, task)

    events = read_events(settings.db_path, task_id)
    memory = state.list_repo_memory(settings.db_path, task.repo)
    return project_task(
        task,
        events,
        include_events=True,
        events_limit=200,
        memory=memory,
        ui_test_label=settings.ui_test_label,
    )


@app.get("/api/repos")
async def get_repos() -> list[dict]:
    """Aggregate task counts per repo, grouped by status."""
    settings = _settings_or_raise()
    state.init_db(settings.db_path)

    tasks = state.list_tasks(settings.db_path)
    per_repo: dict[str, Counter[str]] = {}
    for task in tasks:
        per_repo.setdefault(task.repo, Counter())[task.status.value.upper()] += 1

    out: list[dict] = []
    for repo in sorted(per_repo.keys()):
        counts = per_repo[repo]
        out.append(
            {
                "repo": repo,
                "counts": {
                    "RUNNING": counts.get("RUNNING", 0),
                    "BLOCKED": counts.get("BLOCKED", 0),
                    "DONE": counts.get("DONE", 0),
                    "FAILED": counts.get("FAILED", 0),
                    "PENDING": counts.get("PENDING", 0),
                },
            }
        )
    return out


@app.post("/api/fetch")
async def trigger_fetch() -> dict:
    """Pull open issues from the configured forge and upsert new tasks."""
    from foundry.stages.fetch import fetch

    settings = _settings_or_raise()
    state.init_db(settings.db_path)
    tasks = fetch(settings)
    return {"fetched": len(tasks)}


@app.get("/api/repos/{repo:path}/memory", response_model=list[UiMemoryEntry])
async def get_repo_memory(repo: str) -> list[UiMemoryEntry]:
    """List repo-level memory entries."""
    settings = _settings_or_raise()
    state.init_db(settings.db_path)

    entries = state.list_repo_memory(settings.db_path, repo)
    return [UiMemoryEntry(**entry) for entry in entries]
