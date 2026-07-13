from __future__ import annotations

from foundry.agents import AgentStage, AgentTask, first_line
from foundry.agents.base import build_fresh_prompt


def test_first_line_returns_first_non_empty_line() -> None:
    result = first_line("\n\n  hello world  \nsecond\n")

    assert result == "hello world"


def test_first_line_truncates_to_limit() -> None:
    result = first_line("a" * 500, limit=10)

    assert result == "a" * 10


def test_first_line_on_empty_input_returns_empty_string() -> None:
    assert first_line("") == ""
    assert first_line("\n   \n") == ""


def test_build_fresh_prompt_substitutes_title_description_and_input() -> None:
    task = AgentTask(id=1, title="My title", description="Do X")

    prompt = build_fresh_prompt(AgentStage.PLAN, task, input="hints here")

    assert "My title" in prompt
    assert "Do X" in prompt
    assert "hints here" in prompt


def test_build_fresh_prompt_loads_different_templates_per_stage() -> None:
    task = AgentTask(id=1, title="T", description="D")

    plan_prompt = build_fresh_prompt(AgentStage.PLAN, task, input="")
    verify_prompt = build_fresh_prompt(AgentStage.VERIFY, task, input="")

    assert plan_prompt != verify_prompt


def test_default_prompt_templates_do_not_include_openspec_mode_directives() -> None:
    task = AgentTask(id=1, title="T", description="D")

    plan_prompt = build_fresh_prompt(AgentStage.PLAN, task, input="")
    implement_prompt = build_fresh_prompt(AgentStage.IMPLEMENT, task, input="")

    assert "FOUNDRY_OPENSPEC_MODE=true" not in plan_prompt
    assert "OpenSpec mode" not in plan_prompt
    assert "FOUNDRY_OPENSPEC_MODE=true" not in implement_prompt
    assert "OpenSpec mode" not in implement_prompt


def test_build_fresh_prompt_can_use_openspec_templates() -> None:
    task = AgentTask(id=1, title="T", description="D")

    plan_prompt = build_fresh_prompt(
        AgentStage.PLAN,
        task,
        input="FOUNDRY_OPENSPEC_MODE=true",
        template_name="plan_openspec",
    )
    implement_prompt = build_fresh_prompt(
        AgentStage.IMPLEMENT,
        task,
        input="handoff",
        template_name="implement_openspec",
    )

    assert "OpenSpec planning agent" in plan_prompt
    assert "parallel generic implementation plan" in plan_prompt
    assert "OpenSpec implementation agent" in implement_prompt
    assert "PLAN-stage narration" in implement_prompt
