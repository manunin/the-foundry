from __future__ import annotations

from api.projections import project_task
from foundry.models import Event, ForgeKind, Stage, Task, TaskStatus


def _task(**overrides) -> Task:
    defaults = dict(
        repo="owner/repo",
        issue_number=42,
        issue_title="Test issue",
        issue_body="Body",
    )
    defaults.update(overrides)
    t = Task(**defaults)
    t.id = 1
    return t


def _ev(seq: int, stage: str, kind: str, payload: dict) -> Event:
    return Event(
        id=seq,
        task_id=1,
        seq=seq,
        stage=stage,
        kind=kind,
        ts_ms=1_000 * seq,
        payload=payload,
    )


def test_project_task_stages_from_events() -> None:
    # Arrange
    task = _task()
    events = [
        _ev(1, "plan", "stage_started", {"agent": {"name": "stub"}}),
        _ev(2, "plan", "stage_finished", {"duration_ms": 100, "cost_usd": 0.05}),
        _ev(3, "implement", "stage_started", {}),
        _ev(4, "implement", "stage_failed", {"error": "boom", "duration_ms": 50}),
    ]

    # Act
    ui = project_task(task, events)

    # Assert
    assert ui.stages["agent_plan"].status == "done"
    assert ui.stages["agent_plan"].duration_ms == 100
    assert ui.stages["agent_plan"].cost_usd == 0.05
    assert ui.stages["agent_plan"].agent == {"name": "stub"}

    assert ui.stages["agent_implement"].status == "failed"
    assert "boom" in (ui.stages["agent_implement"].error or "")

    assert ui.total_cost_usd == 0.05


def test_project_task_aliases_current_stage() -> None:
    # Arrange
    task = _task()
    task.current_stage = Stage.PLAN

    # Act
    ui = project_task(task, events=[])

    # Assert
    assert ui.current_stage == "agent_plan"


def test_project_task_tokens_aggregate() -> None:
    # Arrange
    task = _task()
    events = [
        _ev(1, "plan", "stage_started", {}),
        _ev(
            2,
            "plan",
            "stage_finished",
            {"duration_ms": 10, "tokens_in": 10, "tokens_out": 20},
        ),
        _ev(3, "implement", "stage_started", {}),
        _ev(
            4,
            "implement",
            "stage_finished",
            {"duration_ms": 20, "tokens_in": 50, "tokens_out": 60},
        ),
    ]

    # Act
    ui = project_task(task, events)

    # Assert
    assert ui.tokens_in_total == 60
    assert ui.tokens_out_total == 80
    assert ui.duration_ms_total == 30


def test_project_task_events_included_when_requested() -> None:
    # Arrange
    task = _task()
    events = [
        _ev(1, "plan", "stage_started", {}),
        _ev(2, "plan", "agent_text", {"text": "hello"}),
    ]

    # Act
    ui = project_task(task, events, include_events=True)

    # Assert
    assert ui.events is not None
    assert len(ui.events) == 2
    assert ui.events[0].stage == "agent_plan"
    assert ui.events[1].kind == "agent_text"


def test_project_task_events_excluded_by_default() -> None:
    # Arrange
    task = _task()
    events = [_ev(1, "plan", "stage_started", {})]

    # Act
    ui = project_task(task, events)

    # Assert
    assert ui.events is None


def test_project_task_status_pending_when_no_events() -> None:
    # Arrange
    task = _task()
    task.status = TaskStatus.PENDING

    # Act
    ui = project_task(task, events=[])

    # Assert
    assert ui.status == "pending"
    assert ui.stages == {}
    assert ui.total_cost_usd == 0.0


def test_project_task_aggregates_agent_trace_timings() -> None:
    task = _task()
    events = [
        _ev(1, "implement", "stage_started", {}),
        _ev(
            2,
            "implement",
            "agent_span_finished",
            {
                "span_type": "attempt",
                "name": "attempt 2",
                "attempt": 2,
                "duration_ms": 80,
                "time_to_first_event_ms": 12,
            },
        ),
        _ev(
            3,
            "implement",
            "agent_span_finished",
            {"span_type": "tool", "name": "Bash", "duration_ms": 30},
        ),
        _ev(
            4,
            "implement",
            "agent_span_finished",
            {"span_type": "backoff", "name": "backoff", "duration_ms": 20},
        ),
        _ev(
            5,
            "implement",
            "agent_span_finished",
            {
                "span_type": "run",
                "name": "codex_cli",
                "duration_ms": 105,
                "time_to_first_text_ms": 25,
            },
        ),
        _ev(6, "implement", "stage_finished", {"duration_ms": 110}),
    ]

    trace = project_task(task, events).stages["agent_implement"].trace

    assert trace is not None
    assert trace.run_duration_ms == 105
    assert trace.tool_duration_ms == 30
    assert trace.backoff_duration_ms == 20
    assert trace.unattributed_duration_ms == 5
    assert trace.retry_count == 1
    assert trace.time_to_first_event_ms == 12
    assert trace.time_to_first_text_ms == 25
    assert trace.slowest_tools[0].name == "Bash"


def test_project_task_builds_legacy_gitlab_issue_url_and_mr_kind() -> None:
    task = _task(
        forge=ForgeKind.GITLAB,
        forge_host="gitlab.example",
        issue_url=None,
        pr_url="https://gitlab.example/owner/repo/-/merge_requests/3",
    )

    ui = project_task(task, events=[])

    assert ui.issue_url == "https://gitlab.example/owner/repo/-/issues/42"
    assert ui.change_kind == "MR"