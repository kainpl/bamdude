"""RTSP reconnect pacing stays bounded and does not synchronize a whole farm."""

from __future__ import annotations

import asyncio
import random

import pytest

from backend.app.services import external_camera
from backend.app.services.camera_profiles import DEFAULT_PROFILE


def test_first_rtsp_reconnect_is_immediate():
    assert external_camera._rtsp_reconnect_delay(1, DEFAULT_PROFILE) == 0


@pytest.mark.parametrize("attempt", [2, 3, 4, 20])
def test_rtsp_reconnect_delay_is_capped_even_after_jitter(attempt, monkeypatch):
    monkeypatch.setattr(external_camera.random, "random", lambda: 1.0)
    delay = external_camera._rtsp_reconnect_delay(attempt, DEFAULT_PROFILE)
    assert 0 < delay <= DEFAULT_PROFILE.rtsp_reconnect_cap


def test_fifty_printers_receive_different_retry_offsets(monkeypatch):
    """A deterministic simulation proves the policy does not create a herd."""
    delays = set()
    for printer_id in range(50):
        monkeypatch.setattr(external_camera.random, "random", random.Random(printer_id).random)
        delays.add(external_camera._rtsp_reconnect_delay(4, DEFAULT_PROFILE))
    assert len(delays) > 40


@pytest.mark.asyncio
async def test_disconnect_interrupts_backoff_without_waiting_for_the_delay():
    disconnected = asyncio.Event()
    task = asyncio.create_task(external_camera._wait_for_rtsp_retry(10, disconnected))
    await asyncio.sleep(0)
    disconnected.set()
    assert await asyncio.wait_for(task, timeout=0.2) is False


@pytest.mark.asyncio
async def test_cancellation_interrupts_backoff_immediately():
    task = asyncio.create_task(external_camera._wait_for_rtsp_retry(10, None))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
