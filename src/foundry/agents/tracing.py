from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterator

from foundry.events import record_event


class SpanType(StrEnum):
    RUN = "run"
    ATTEMPT = "attempt"
    TURN = "turn"
    TOOL = "tool"
    BACKOFF = "backoff"


class StreamLifecycleKind(StrEnum):
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_FINISHED = "attempt_finished"
    ATTEMPT_FAILED = "attempt_failed"
    BACKOFF_STARTED = "backoff_started"
    BACKOFF_FINISHED = "backoff_finished"


@dataclass(frozen=True)
class StreamLifecycleEvent:
    kind: StreamLifecycleKind
    attempt: int
    duration_ms: int | None = None
    delay_ms: int | None = None
    event_count: int | None = None
    time_to_first_event_ms: int | None = None
    error: str | None = None


@dataclass
class _OpenSpan:
    span_type: SpanType
    name: str
    parent_span_id: str | None
    started_ns: int
    payload: dict[str, Any]


class AgentTracer:
    """Persist correlated timing spans for one agent invocation."""

    def __init__(
        self,
        *,
        db_path: Path | None,
        task_id: int,
        stage: str,
        backend: str,
        model: str | None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._db_path = db_path
        self._task_id = task_id
        self._stage = stage
        self._backend = backend
        self._model = model
        self._clock_ns = clock_ns
        self.trace_id = str(uuid.uuid4())
        self._open: dict[str, _OpenSpan] = {}
        self._provider_spans: dict[str, str] = {}
        self._root_span_id: str | None = None
        self._run_started_ns: int | None = None
        self._time_to_first_text_ms: int | None = None

    @property
    def enabled(self) -> bool:
        return self._db_path is not None

    @contextmanager
    def run(self) -> Iterator[str]:
        run_span_id = self.start_span(
            SpanType.RUN,
            self._backend,
            payload={"backend": self._backend, "model": self._model},
        )
        self._root_span_id = run_span_id
        self._run_started_ns = self._open[run_span_id].started_ns
        try:
            yield run_span_id
        except Exception as exc:
            self._close_children(run_span_id)
            self.finish_span(
                run_span_id,
                status="failed",
                payload={
                    "error": repr(exc),
                    "time_to_first_text_ms": self._time_to_first_text_ms,
                },
            )
            raise
        self._close_children(run_span_id)
        self.finish_span(
            run_span_id,
            payload={"time_to_first_text_ms": self._time_to_first_text_ms},
        )

    def mark_first_text(self) -> None:
        if self._time_to_first_text_ms is not None or self._run_started_ns is None:
            return
        self._time_to_first_text_ms = max(
            0,
            (self._clock_ns() - self._run_started_ns) // 1_000_000,
        )

    def start_span(
        self,
        span_type: SpanType,
        name: str,
        *,
        parent_span_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        span_id = str(uuid.uuid4())
        span_payload = dict(payload or {})
        self._open[span_id] = _OpenSpan(
            span_type=span_type,
            name=name,
            parent_span_id=parent_span_id,
            started_ns=self._clock_ns(),
            payload=span_payload,
        )
        self._record(
            "agent_span_started",
            self._span_payload(
                span_id,
                span_type,
                name,
                parent_span_id,
                span_payload,
            ),
        )
        return span_id

    def finish_span(
        self,
        span_id: str,
        *,
        status: str = "success",
        payload: dict[str, Any] | None = None,
    ) -> None:
        opened = self._open.pop(span_id, None)
        if opened is None:
            return
        duration_ms = max(0, (self._clock_ns() - opened.started_ns) // 1_000_000)
        final_payload = dict(opened.payload)
        final_payload.update(payload or {})
        final_payload["duration_ms"] = duration_ms
        final_payload["status"] = status
        kind = "agent_span_failed" if status == "failed" else "agent_span_finished"
        self._record(
            kind,
            self._span_payload(
                span_id,
                opened.span_type,
                opened.name,
                opened.parent_span_id,
                final_payload,
            ),
        )

    def start_provider_span(
        self,
        provider_id: str,
        span_type: SpanType,
        name: str,
        *,
        parent_span_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        existing = self._provider_spans.get(provider_id)
        if existing is not None:
            return existing
        span_id = self.start_span(
            span_type,
            name,
            parent_span_id=parent_span_id or self._root_span_id,
            payload={"provider_id": provider_id, **(payload or {})},
        )
        self._provider_spans[provider_id] = span_id
        return span_id

    def finish_provider_span(
        self,
        provider_id: str,
        span_type: SpanType,
        name: str,
        *,
        status: str = "success",
        payload: dict[str, Any] | None = None,
    ) -> None:
        span_id = self._provider_spans.pop(provider_id, None)
        if span_id is not None:
            self.finish_span(span_id, status=status, payload=payload)
            return
        self._record_completed_span(
            span_type,
            name,
            status=status,
            payload={
                "provider_id": provider_id,
                "timing_complete": False,
                **(payload or {}),
            },
        )

    def handle_stream_lifecycle(self, event: StreamLifecycleEvent) -> None:
        provider_id = f"attempt:{event.attempt}"
        if event.kind == StreamLifecycleKind.ATTEMPT_STARTED:
            self.start_provider_span(
                provider_id,
                SpanType.ATTEMPT,
                f"attempt {event.attempt}",
                payload={"attempt": event.attempt},
            )
            return
        if event.kind in {
            StreamLifecycleKind.ATTEMPT_FINISHED,
            StreamLifecycleKind.ATTEMPT_FAILED,
        }:
            self.finish_provider_span(
                provider_id,
                SpanType.ATTEMPT,
                f"attempt {event.attempt}",
                status=(
                    "failed"
                    if event.kind == StreamLifecycleKind.ATTEMPT_FAILED
                    else "success"
                ),
                payload={
                    "attempt": event.attempt,
                    "event_count": event.event_count,
                    "time_to_first_event_ms": event.time_to_first_event_ms,
                    "error": event.error,
                },
            )
            return
        backoff_id = f"backoff:{event.attempt}"
        if event.kind == StreamLifecycleKind.BACKOFF_STARTED:
            self.start_provider_span(
                backoff_id,
                SpanType.BACKOFF,
                "rate limit backoff",
                payload={"attempt": event.attempt, "delay_ms": event.delay_ms},
            )
            return
        self.finish_provider_span(
            backoff_id,
            SpanType.BACKOFF,
            "rate limit backoff",
            payload={"attempt": event.attempt, "delay_ms": event.delay_ms},
        )

    def _record_completed_span(
        self,
        span_type: SpanType,
        name: str,
        *,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        span_id = str(uuid.uuid4())
        completed_payload = {
            **payload,
            "duration_ms": None,
            "status": status,
        }
        kind = "agent_span_failed" if status == "failed" else "agent_span_finished"
        self._record(
            kind,
            self._span_payload(
                span_id,
                span_type,
                name,
                self._root_span_id,
                completed_payload,
            ),
        )

    def _close_children(self, root_span_id: str) -> None:
        child_ids = [
            span_id
            for span_id, opened in self._open.items()
            if span_id != root_span_id
        ]
        for span_id in child_ids:
            self.finish_span(
                span_id,
                status="incomplete",
                payload={"timing_complete": False},
            )
        self._provider_spans.clear()

    def _span_payload(
        self,
        span_id: str,
        span_type: SpanType,
        name: str,
        parent_span_id: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "span_type": span_type.value,
            "name": name,
            "backend": self._backend,
            "model": self._model,
            **payload,
        }

    def _record(self, kind: str, payload: dict[str, Any]) -> None:
        if self._db_path is None:
            return
        record_event(
            self._db_path,
            task_id=self._task_id,
            stage=self._stage,
            kind=kind,
            payload=payload,
        )
