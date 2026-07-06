from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from foundry import state
from foundry.agents import AgentSettings, AgentStage, AgentTask
from foundry.agents.opencode_cli import OpencodeCliAgent
from foundry.events import read_events


def _task(task_id: int = 1) -> AgentTask:
    return AgentTask(id=task_id, title="t", description="d")


def _settings(**overrides: object) -> AgentSettings:
    defaults: dict = {
        "stage": AgentStage.IMPLEMENT,
        "backend": "opencode_cli",
        "timeout_sec": 60,
        "max_turns": 3,
        "model": "openrouter/anthropic/claude-haiku-4.5",
    }
    defaults.update(overrides)
    return AgentSettings(**defaults)  # type: ignore[arg-type]


def test_extract_session_id_from_top_level_sessionID() -> None:
    events = [
        {"type": "step_start", "sessionID": "ses_abc"},
        {"type": "text", "sessionID": "ses_abc", "part": {"text": "hi"}},
    ]

    assert OpencodeCliAgent._extract_session_id(events) == "ses_abc"


def test_extract_session_id_falls_back_to_part_sessionID() -> None:
    events = [{"type": "text", "part": {"sessionID": "ses_from_part"}}]

    assert OpencodeCliAgent._extract_session_id(events) == "ses_from_part"


def test_extract_session_id_returns_none_when_missing() -> None:
    assert OpencodeCliAgent._extract_session_id([{"type": "text", "part": {}}]) is None


def test_extract_final_text_concatenates_text_events_in_order() -> None:
    events = [
        {"type": "step_start"},
        {"type": "text", "part": {"text": "Hello "}},
        {"type": "step_start"},
        {"type": "text", "part": {"text": "world"}},
        {"type": "step_finish"},
    ]

    assert OpencodeCliAgent._extract_final_text(events) == "Hello world"


def test_extract_final_text_returns_empty_when_no_text_events() -> None:
    events = [{"type": "step_finish"}]

    assert OpencodeCliAgent._extract_final_text(events) == ""


def test_apply_caches_session_id_and_resumes_next_call(tmp_path: Path) -> None:
    agent = OpencodeCliAgent(settings=_settings())
    task = _task(task_id=11)
    fresh = [
        {"type": "step_start", "sessionID": "ses_11"},
        {"type": "text", "sessionID": "ses_11", "part": {"text": "done"}},
    ]
    resume = [{"type": "text", "sessionID": "ses_11", "part": {"text": "again"}}]

    with patch("foundry.agents.opencode_cli.iter_cli_jsonl_with_retry") as run:
        run.side_effect = [fresh, resume]
        first = agent.apply(task=task, worktree=tmp_path, input="hi")
        second = agent.apply(task=task, worktree=tmp_path, input="more")

    assert first.response == "done"
    assert second.response == "again"
    assert agent.get_session_id(task) == "ses_11"
    fresh_cmd = run.call_args_list[0].args[0]
    resume_cmd = run.call_args_list[1].args[0]
    assert "--session" not in fresh_cmd
    assert resume_cmd[resume_cmd.index("--session") + 1] == "ses_11"


def test_apply_resumes_session_id_from_sqlite(tmp_path: Path) -> None:
    db = tmp_path / "foundry.sqlite"
    state.init_db(db)
    task = _task(task_id=12)
    settings = _settings(db_path=db)
    first_agent = OpencodeCliAgent(settings=settings)

    with patch(
        "foundry.agents.opencode_cli.iter_cli_jsonl_with_retry",
        return_value=[
            {"type": "step_start", "sessionID": "ses_db"},
            {"type": "text", "sessionID": "ses_db", "part": {"text": "done"}},
        ],
    ):
        first_agent.apply(task=task, worktree=tmp_path, input="hi")

    second_agent = OpencodeCliAgent(settings=settings)
    with patch(
        "foundry.agents.opencode_cli.iter_cli_jsonl_with_retry",
        return_value=[
            {"type": "text", "sessionID": "ses_db", "part": {"text": "again"}}
        ],
    ) as run:
        second_agent.apply(task=task, worktree=tmp_path, input="more")

    cmd = run.call_args.args[0]
    assert cmd[cmd.index("--session") + 1] == "ses_db"


def test_apply_passes_model_dir_and_format(tmp_path: Path) -> None:
    agent = OpencodeCliAgent(settings=_settings(model="openrouter/x-ai/grok"))

    with patch(
        "foundry.agents.opencode_cli.iter_cli_jsonl_with_retry",
        return_value=[{"type": "text", "sessionID": "s", "part": {"text": "ok"}}],
    ) as run:
        agent.apply(task=_task(), worktree=tmp_path, input="")

    cmd = run.call_args.args[0]
    assert cmd[:2] == ["opencode", "run"]
    assert cmd[cmd.index("--format") + 1] == "json"
    assert cmd[cmd.index("--dir") + 1] == str(tmp_path)
    assert cmd[cmd.index("-m") + 1] == "openrouter/x-ai/grok"
    assert run.call_args.kwargs["cwd"] == tmp_path


def test_extract_usage_from_metadata_tokens() -> None:
    events = [
        {"type": "text", "part": {"text": "hi"}},
        {
            "type": "step_finish",
            "metadata": {
                "tokens": {
                    "input": 200,
                    "output": 60,
                    "cache": {"read": 500, "write": 10},
                }
            },
        },
    ]

    got = OpencodeCliAgent._extract_usage(events)

    assert got == {
        "input": 200,
        "output": 60,
        "cache_read_input": 500,
        "cache_creation_input": 10,
    }


def test_extract_usage_from_top_level_tokens() -> None:
    events = [{"type": "step_finish", "tokens": {"input": 15, "output": 7}}]

    assert OpencodeCliAgent._extract_usage(events) == {"input": 15, "output": 7}


def test_extract_usage_returns_none_when_missing() -> None:
    events = [{"type": "text", "part": {"text": "hi"}}]

    assert OpencodeCliAgent._extract_usage(events) is None


def test_opencode_streams_text_tool_and_trace_events(tmp_path: Path) -> None:
    db = tmp_path / "foundry.sqlite"
    state.init_db(db)
    agent = OpencodeCliAgent(settings=_settings(db_path=db))
    task = _task(task_id=14)
    streamed = [
        {"type": "step_start", "id": "step-1", "sessionID": "ses_14"},
        {
            "type": "tool",
            "part": {
                "id": "tool-1",
                "tool": "Bash",
                "state": {"status": "running", "input": {"command": "pytest"}},
            },
        },
        {
            "type": "tool",
            "part": {
                "id": "tool-1",
                "tool": "Bash",
                "state": {"status": "completed", "input": {"command": "pytest"}},
            },
        },
        {"type": "text", "part": {"text": "done"}},
        {"type": "step_finish", "id": "step-1"},
    ]

    def stream(*_args: object, **kwargs: object) -> list[dict]:
        on_event = kwargs.get("on_event")
        if callable(on_event):
            for event in streamed:
                on_event(event)
        return streamed

    with patch(
        "foundry.agents.opencode_cli.iter_cli_jsonl_with_retry",
        side_effect=stream,
    ):
        result = agent.apply(task=task, worktree=tmp_path)

    assert result.response == "done"
    kinds = [event.kind for event in read_events(db, task_id=task.id)]
    assert "agent_tool" in kinds
    assert "agent_text" in kinds
    assert "agent_result" in kinds
    assert "agent_span_finished" in kinds
