"""The preset-mirror loop does not sync while cloud credential admission is closed.

Start-up closes admission, runs the application lifespan -- which starts the loop and pokes it -- and
opens admission only afterwards. The poked tick therefore asked for a credential snapshot behind a closed
door and logged ``503: Cloud credential admission is closed`` with a traceback on every start, and the
first real sync came five minutes later (farm log, 2026-11-18). A closed door is "not yet", not an
error: the tick is skipped, and start-up pokes the loop again once admission is open.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from backend.app.services import cloud_credentials, filament_preset_sync


@pytest.fixture
def admission():
    was_open = cloud_credentials.admission_open()
    yield
    (cloud_credentials.open_admission if was_open else cloud_credentials.close_admission)()


async def _run_one_poke(monkeypatch) -> list[int]:
    calls: list[int] = []

    async def _sync():
        calls.append(1)

    monkeypatch.setattr(filament_preset_sync, "_sync_all_connected", _sync)
    # The module's event binds to the first loop that waits on it; each test has its own loop.
    monkeypatch.setattr(filament_preset_sync, "_sync_wakeup", asyncio.Event())
    task = asyncio.create_task(filament_preset_sync.filament_preset_sync_loop())
    try:
        filament_preset_sync.request_sync_soon()
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    return calls


async def test_a_tick_behind_a_closed_door_is_skipped_quietly(monkeypatch, caplog, admission):
    cloud_credentials.close_admission()

    with caplog.at_level(logging.WARNING, logger=filament_preset_sync.logger.name):
        calls = await _run_one_poke(monkeypatch)

    assert calls == []
    assert caplog.records == []


async def test_a_tick_runs_once_admission_is_open(monkeypatch, admission):
    cloud_credentials.open_admission()

    assert await _run_one_poke(monkeypatch) == [1]


def test_startup_pokes_the_loop_after_it_opens_admission():
    """The poke made inside the application lifespan lands before the door opens."""
    import inspect

    from backend.app import main

    source = inspect.getsource(main.lifespan)
    opened = source.index("open_cloud_admission()")
    assert "request_sync_soon()" in source[opened:], "start-up must poke the preset sync after opening admission"
