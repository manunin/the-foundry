from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from api.bus import EventBus
from foundry import state
from foundry.events import record_event
from foundry.models import Event


@pytest.mark.asyncio
async def test_bus_subscribe_replays_from_sqlite(tmp_path: Path) -> None:
    # Arrange
    db = tmp_path / "f.sqlite"
    state.init_db(db)
    for i in range(3):
        record_event(db, 1, "plan", "agent_text", {"text": f"msg-{i}"})

    local_bus = EventBus()

    # Act
    received: list[Event] = []
    agen = local_bus.subscribe(db, task_id=1)
    try:
        for _ in range(3):
            received.append(await asyncio.wait_for(agen.__anext__(), timeout=1.0))
    finally:
        await agen.aclose()

    # Assert
    assert [ev.seq for ev in received] == [1, 2, 3]
    assert received[0].payload == {"text": "msg-0"}


@pytest.mark.asyncio
async def test_bus_subscribe_with_after_seq_skips_old(tmp_path: Path) -> None:
    # Arrange
    db = tmp_path / "f.sqlite"
    state.init_db(db)
    for i in range(5):
        record_event(db, 1, "plan", "agent_text", {"text": f"m{i}"})

    local_bus = EventBus()

    # Act
    received: list[Event] = []
    agen = local_bus.subscribe(db, task_id=1, after_seq=3)
    try:
        for _ in range(2):
            received.append(await asyncio.wait_for(agen.__anext__(), timeout=1.0))
    finally:
        await agen.aclose()

    # Assert
    assert [ev.seq for ev in received] == [4, 5]


@pytest.mark.asyncio
async def test_bus_publish_delivers_to_subscribers(tmp_path: Path) -> None:
    # Arrange
    db = tmp_path / "f.sqlite"
    state.init_db(db)
    local_bus = EventBus()

    # Act: first drain catch-up (none), then publish live.
    agen = local_bus.subscribe(db, task_id=1)

    async def consume_one() -> Event:
        return await asyncio.wait_for(agen.__anext__(), timeout=1.0)

    consumer_task = asyncio.create_task(consume_one())
    await asyncio.sleep(0.05)  # allow catch-up to drain, get into the queue wait

    live_event = Event(
        id=0, task_id=1, seq=1, stage="plan", kind="agent_text",
        ts_ms=0, payload={"text": "live"},
    )
    local_bus.publish(live_event)
    got = await consumer_task

    # Assert
    assert got.seq == 1
    assert got.payload == {"text": "live"}

    await agen.aclose()


@pytest.mark.asyncio
async def test_bus_publish_filters_by_task_id(tmp_path: Path) -> None:
    # Arrange
    db = tmp_path / "f.sqlite"
    state.init_db(db)
    local_bus = EventBus()

    agen = local_bus.subscribe(db, task_id=1)

    async def consume_one() -> Event:
        return await asyncio.wait_for(agen.__anext__(), timeout=1.0)

    consumer_task = asyncio.create_task(consume_one())
    await asyncio.sleep(0.05)

    # Act: publish wrong task, then right task.
    local_bus.publish(Event(
        id=0, task_id=2, seq=1, stage="plan", kind="agent_text",
        ts_ms=0, payload={"text": "wrong-task"},
    ))
    local_bus.publish(Event(
        id=0, task_id=1, seq=1, stage="plan", kind="agent_text",
        ts_ms=0, payload={"text": "ok"},
    ))

    got = await consumer_task

    # Assert
    assert got.task_id == 1
    assert got.payload == {"text": "ok"}

    await agen.aclose()
