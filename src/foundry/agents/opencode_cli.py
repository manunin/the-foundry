from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import observability, state
from ..events import record_event
from ..security import scrubbed_agent_env
from .base import (
    AgentResult,
    AgentStage,
    AgentTask,
    build_fresh_prompt,
    first_line,
)
from .config import AgentSettings
from .streaming import _normalize_tool_event, iter_cli_jsonl_with_retry
from .tracing import AgentTracer, SpanType


class OpencodeCliAgent:
    """Backend shelling out to the `opencode` CLI.

    Bound to one stage at construction time. `opencode run --format json`
    emits NDJSON events; resume uses `--session <id>` on subsequent calls.
    Provider auth is supplied via env (e.g. `OPENROUTER_API_KEY`).

    NDJSON is streamed into the shared event and timing contracts.
    """

    name = "opencode_cli"

    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings
        self.stage: AgentStage = settings.stage
        self._sessions: dict[int, str] = {}

    def apply(
        self,
        task: AgentTask,
        worktree: Path,
        input: str = "",
    ) -> AgentResult:
        resume_id = self.get_session_id(task)
        if resume_id is None:
            message = build_fresh_prompt(self.stage, task, input)
        else:
            message = input

        cmd: list[str] = [
            "opencode", "run",
            "--format", "json",
            "--dir", str(worktree),
        ]
        if self._settings.model:
            cmd += ["-m", self._settings.model]
        if resume_id:
            cmd += ["--session", resume_id]
        cmd.append(message)

        tracer = AgentTracer(
            db_path=self._settings.db_path,
            task_id=task.id,
            stage=self.stage.value,
            backend=self.name,
            model=self._settings.model or None,
        )
        with tracer.run():
            with observability.track_generation(
                name="llm.opencode_cli",
                model=self._settings.model or None,
                input=message,
            ) as gen:
                events = iter_cli_jsonl_with_retry(
                    cmd,
                    cwd=worktree,
                    env=scrubbed_agent_env(self.name),
                    on_event=lambda ev: self._emit_for(task, ev, tracer),
                    on_lifecycle=tracer.handle_stream_lifecycle,
                    timeout_sec=self._settings.timeout_sec,
                )

                new_session_id = self._extract_session_id(events)
                if new_session_id:
                    self._sessions[task.id] = new_session_id
                    if self._settings.db_path is not None:
                        state.save_agent_session(
                            self._settings.db_path,
                            task.id,
                            self.stage.value,
                            self.name,
                            new_session_id,
                        )

                response = self._extract_final_text(events)
                usage = self._extract_usage(events)
                observability.update_generation(gen, output=response, usage=usage)
                self._record(
                    task,
                    kind="agent_result",
                    payload={"summary": first_line(response), "text": response},
                )

        return AgentResult(
            stage=self.stage,
            response=response,
            result=first_line(response),
            tokens_in=(usage or {}).get("input") if usage else None,
            tokens_out=(usage or {}).get("output") if usage else None,
        )

    def _emit_for(
        self,
        task: AgentTask,
        event: dict[str, Any],
        tracer: AgentTracer,
    ) -> None:
        event_type = event.get("type")
        part = event.get("part") or {}
        if not isinstance(part, dict):
            part = {}

        if event_type in {"step_start", "step_finish"}:
            step_id = str(event.get("id") or part.get("id") or "step:current")
            if event_type == "step_start":
                tracer.start_provider_span(step_id, SpanType.TURN, "step")
            else:
                tracer.finish_provider_span(step_id, SpanType.TURN, "step")
            return

        if event_type == "text":
            text = part.get("text")
            if text:
                tracer.mark_first_text()
                self._record(task, kind="agent_text", payload={"text": str(text)})
            return

        if event_type not in {"tool", "tool_use"} and part.get("type") != "tool":
            return
        state_payload = part.get("state") or event.get("state") or {}
        if not isinstance(state_payload, dict):
            state_payload = {}
        tool_name = str(
            part.get("tool")
            or event.get("tool")
            or part.get("name")
            or event.get("name")
            or "tool"
        )
        tool_id = str(
            part.get("callID")
            or part.get("id")
            or event.get("callID")
            or event.get("id")
            or f"tool:{tool_name}"
        )
        status = str(state_payload.get("status") or event.get("status") or "")
        if status in {"pending", "running", "started", ""}:
            tracer.start_provider_span(tool_id, SpanType.TOOL, tool_name)
            return
        tracer.finish_provider_span(
            tool_id,
            SpanType.TOOL,
            tool_name,
            status="failed" if status in {"error", "failed"} else "success",
        )
        tool_input = state_payload.get("input") or part.get("input") or event.get("input")
        self._record(
            task,
            kind="agent_tool",
            payload=_normalize_tool_event({"name": tool_name, "input": tool_input}),
        )

    def _record(
        self,
        task: AgentTask,
        *,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        if self._settings.db_path is None or task.id is None:
            return
        record_event(
            self._settings.db_path,
            task_id=task.id,
            stage=self.stage.value,
            kind=kind,
            payload=payload,
        )

    def get_session_id(self, task: AgentTask) -> str | None:
        if task.id in self._sessions:
            return self._sessions[task.id]
        if self._settings.db_path is None:
            return None
        session_id = state.get_agent_session(
            self._settings.db_path, task.id, self.stage.value, self.name
        )
        if session_id:
            self._sessions[task.id] = session_id
        return session_id

    @staticmethod
    def _extract_session_id(events: list[dict]) -> str | None:
        for event in events:
            sid = event.get("sessionID") or (event.get("part") or {}).get("sessionID")
            if sid:
                return str(sid)
        return None

    @staticmethod
    def _extract_final_text(events: list[dict]) -> str:
        """Concatenate all assistant text chunks in order.

        opencode emits each text block as its own `type:"text"` event with the
        full chunk in `part.text`; the final response is the concatenation.
        """
        chunks: list[str] = []
        for event in events:
            if event.get("type") != "text":
                continue
            part = event.get("part") or {}
            text = part.get("text")
            if text:
                chunks.append(str(text))
        return "".join(chunks)

    @staticmethod
    def _extract_usage(events: list[dict]) -> dict[str, int] | None:
        """Pull token counts from whichever opencode event carries them.

        Field names vary by opencode version; checks several likely paths
        (top-level `tokens`, `metadata.tokens`, `part.tokens`, message
        metadata) and returns the first hit.
        """
        for event in reversed(events):
            tokens = (
                event.get("tokens")
                or (event.get("metadata") or {}).get("tokens")
                or (event.get("part") or {}).get("tokens")
                or (event.get("message") or {}).get("metadata", {}).get("tokens")
            )
            if not isinstance(tokens, dict) or not tokens:
                continue
            out: dict[str, int] = {}
            if "input" in tokens:
                out["input"] = int(tokens["input"])
            if "output" in tokens:
                out["output"] = int(tokens["output"])
            cache = tokens.get("cache")
            if isinstance(cache, dict):
                if "read" in cache:
                    out["cache_read_input"] = int(cache["read"])
                if "write" in cache:
                    out["cache_creation_input"] = int(cache["write"])
            return out or None
        return None
