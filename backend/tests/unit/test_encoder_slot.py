"""One encoder for the process, first come first served, held until its child has ended (G10 E2 / F3)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from backend.app.services.encoder_slot import EncoderSlot

pytestmark = pytest.mark.asyncio


async def test_two_callers_never_hold_it_together_and_are_served_in_order():
    slot = EncoderSlot()
    order: list = []
    running = 0
    peak = 0

    async def work(name: str, seconds: float):
        nonlocal running, peak
        ticket = await slot.acquire()
        running += 1
        peak = max(peak, running)
        order.append(name)
        try:
            await asyncio.sleep(seconds)
        finally:
            running -= 1
            slot.release(ticket)

    tasks = []
    for index in range(5):
        tasks.append(asyncio.create_task(work(f"job-{index}", 0.05)))
        await asyncio.sleep(0)  # each reaches the queue before the next is made
    await asyncio.gather(*tasks)
    assert peak == 1
    assert order == [f"job-{index}" for index in range(5)]
    assert not slot.busy and slot.waiting() == 0


async def test_a_stopped_waiter_leaves_the_queue_and_holds_nothing():
    slot = EncoderSlot()
    first = await slot.acquire()
    stop = asyncio.Event()
    waiter = asyncio.create_task(slot.acquire(stop))
    behind = asyncio.create_task(slot.acquire())
    await asyncio.sleep(0.15)
    assert slot.waiting() == 2
    stop.set()
    assert await waiter is None
    assert slot.waiting() == 1
    slot.release(first)
    ticket = await asyncio.wait_for(behind, 2)  # the one behind is not stuck behind a waiter that left
    assert ticket is not None and slot.busy
    slot.release(ticket)
    assert not slot.busy


async def test_a_cancelled_waiter_leaves_the_queue_too():
    slot = EncoderSlot()
    first = await slot.acquire()
    waiter = asyncio.create_task(slot.acquire())
    await asyncio.sleep(0.15)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert slot.waiting() == 0
    slot.release(first)
    assert not slot.busy


async def test_only_the_holder_gives_it_back():
    slot = EncoderSlot()
    ticket = await slot.acquire()
    slot.release(object())  # somebody else's ticket
    slot.release(None)
    assert slot.busy
    slot.release(ticket)
    slot.release(ticket)  # twice changes nothing
    assert not slot.busy


async def test_a_holder_that_fails_still_frees_it_for_the_next():
    slot = EncoderSlot()

    async def failing():
        ticket = await slot.acquire()
        try:
            raise RuntimeError("the encode failed")
        finally:
            slot.release(ticket)

    with pytest.raises(RuntimeError):
        await failing()
    ticket = await asyncio.wait_for(slot.acquire(), 2)
    assert ticket is not None
    slot.release(ticket)


class TestAnEncoderWhoseEndWasNotSeen:
    """Codex E2-R / F3-R: the place was given back while the unreaped child still ran."""

    async def test_the_place_stays_taken_until_the_child_has_ended(self):
        slot = EncoderSlot()
        ticket = await slot.acquire()
        ended = threading.Event()
        slot.keep_until_reaped(ticket, ended)
        slot.release(ticket)  # the holder's own finally: no longer the holder
        assert slot.busy
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(slot.acquire(), 0.3)
        ended.set()  # the child really went
        nxt = await asyncio.wait_for(slot.acquire(), 2)
        assert nxt is not None
        slot.release(nxt)
        assert not slot.busy

    async def test_without_an_end_to_wait_for_it_stays_taken(self):
        slot = EncoderSlot()
        ticket = await slot.acquire()
        slot.keep_until_reaped(ticket, None)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(slot.acquire(), 0.3)
        assert slot.busy and slot.waiting() == 0
        slot.reset()  # only a new process frees it
        assert not slot.busy

    async def test_a_ticket_that_does_not_hold_it_cannot_keep_it(self):
        slot = EncoderSlot()
        slot.keep_until_reaped(object(), None)
        slot.keep_until_reaped(None, None)
        assert not slot.busy
