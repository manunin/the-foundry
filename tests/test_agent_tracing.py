from __future__ import annotations

from pathlib import Path

from foundry.agents.tracing import AgentTracer, SpanType
from foundry.events import read_events
from foundry.state import init_db


class _Clock:
    def __init__(self, values: list[int]) -> None:
        self._values = iter(values)

    def __call__(self) -> int:
        return next(self._values)


def test_agent_tracer_records_correlated_run_and_tool_durations(
    tmp_path: Path,
) -> None:
    db = tmp_path / "foundry.sqlite"
    init_db(db)
    tracer = AgentTracer(
        db_path=db,
        task_id=7,
        stage="implement",
        backend="codex_cli",
        model="gpt-5",
        clock_ns=_Clock([0, 10_000_000, 30_000_000, 50_000_000]),
    )

    with tracer.run() as run_span_id:
        tracer.start_provider_span("tool-1", SpanType.TOOL, "Bash")
        tracer.finish_provider_span("tool-1", SpanType.TOOL, "Bash")

    events = read_events(db, task_id=7)
    assert [event.kind for event in events] == [
        "agent_span_started",
        "agent_span_started",
        "agent_span_finished",
        "agent_span_finished",
    ]
    tool_finished = events[2].payload
    run_finished = events[3].payload
    assert tool_finished["duration_ms"] == 20
    assert tool_finished["parent_span_id"] == run_span_id
    assert run_finished["duration_ms"] == 50
    assert tool_finished["trace_id"] == run_finished["trace_id"]


def test_agent_tracer_marks_unmatched_provider_completion_as_unknown_duration(
    tmp_path: Path,
) -> None:
    db = tmp_path / "foundry.sqlite"
    init_db(db)
    tracer = AgentTracer(
        db_path=db,
        task_id=8,
        stage="plan",
        backend="codex_cli",
        model=None,
        clock_ns=_Clock([0, 1_000_000]),
    )

    with tracer.run():
        tracer.finish_provider_span("tool-1", SpanType.TOOL, "Read")

    tool_event = next(
        event
        for event in read_events(db, task_id=8)
        if event.payload.get("span_type") == "tool"
    )
    assert tool_event.payload["duration_ms"] is None
    assert tool_event.payload["timing_complete"] is False
