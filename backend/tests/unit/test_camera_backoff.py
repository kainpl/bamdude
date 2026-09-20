"""RTSP reconnect pacing stays bounded and does not synchronize a whole farm."""

from __future__ import annotations

import asyncio
import random

import pytest

from backend.app.api.routes.camera import compute_rtsp_reconnect_delay, wait_for_rtsp_reconnect
from backend.app.services.camera_profiles import DEFAULT_PROFILE


def test_first_rtsp_reconnect_is_immediate():
    assert compute_rtsp_reconnect_delay(1, DEFAULT_PROFILE, jitter_random=0.5) == 0


@pytest.mark.parametrize("attempt", [2, 3, 4, 20])
def test_rtsp_reconnect_delay_is_capped_even_after_jitter(attempt):
    delay = compute_rtsp_reconnect_delay(attempt, DEFAULT_PROFILE, jitter_random=1.0)
    assert 0 < delay <= DEFAULT_PROFILE.rtsp_reconnect_cap


def test_fifty_printers_receive_different_retry_offsets():
    """A deterministic simulation proves the policy does not create a herd."""
    delays = {
        compute_rtsp_reconnect_delay(4, DEFAULT_PROFILE, jitter_random=random.Random(printer_id).random())
        for printer_id in range(50)
    }
    assert len(delays) > 40


@pytest.mark.asyncio
async def test_disconnect_interrupts_backoff_without_waiting_for_the_delay():
    disconnected = asyncio.Event()
    task = asyncio.create_task(wait_for_rtsp_reconnect(10, disconnected))
    await asyncio.sleep(0)
    disconnected.set()
    assert await asyncio.wait_for(task, timeout=0.2) is False


@pytest.mark.asyncio
async def test_cancellation_interrupts_backoff_immediately():
    task = asyncio.create_task(wait_for_rtsp_reconnect(10, None))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
