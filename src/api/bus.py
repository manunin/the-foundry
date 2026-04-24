from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import structlog

from foundry.events import read_events, subscribe_writer
from foundry.models import Event

log = structlog.get_logger(__name__)


class EventBus:
    """In-process pubsub over `record_event`.

    Each subscriber owns an `asyncio.Queue`. `publish()` is safe to call from
    any thread — it marshals to the event loop captured on first subscribe via
    `loop.call_soon_threadsafe`. Catch-up phase reads persisted events from
    SQLite, live phase delivers only events with `seq > last_seen` for the
    requested task.
    """

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[Event]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._hook_installed: bool = False

    def _ensure_hook(self) -> None:
        if not self._hook_installed:
            subscribe_writer(self.publish)
            self._hook_installed = True

    async def subscribe(
        self,
        db_path: Path,
        task_id: int,
        after_seq: int | None = None,
    ) -> AsyncIterator[Event]:
        """Catch-up from SQLite, then live. Yields events strictly in seq order."""
        self._ensure_hook()
        # Always capture the current running loop; across test runs or app
        # restarts the loop object changes, and stale loops make
        # call_soon_threadsafe raise RuntimeError.
        self._loop = asyncio.get_running_loop()

        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1024)
        self._queues.add(queue)

        last_seen = after_seq or 0
        try:
            for ev in read_events(db_path, task_id, after_seq=after_seq):
                last_seen = max(last_seen, ev.seq)
                yield ev

            while True:
                ev = await queue.get()
                if ev.task_id != task_id:
                    continue
                if ev.seq <= last_seen:
                    continue
                last_seen = ev.seq
                yield ev
        finally:
            self._queues.discard(queue)

    def publish(self, event: Event) -> None:
        """Fan-out to all live subscriber queues. Called from any thread."""
        if not self._queues:
            return
        loop = self._loop
        for queue in list(self._queues):
            if loop is not None and loop.is_running():
                try:
                    loop.call_soon_threadsafe(self._put_nowait, queue, event)
                except RuntimeError as exc:
                    log.warning("bus.publish_schedule_failed", error=repr(exc))
            else:
                self._put_nowait(queue, event)

    @staticmethod
    def _put_nowait(queue: asyncio.Queue[Event], event: Event) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("bus.queue_full_drop", task_id=event.task_id, seq=event.seq)


bus = EventBus()
