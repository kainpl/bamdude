"""Coalesced printer listings with an answer that says what it proves (G10 A, M03)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.app.services.printer_files.base import RemoteFile
from backend.app.services.printer_files.listing import (
    LISTING_COMPLETE,
    LISTING_MISSING,
    LISTING_SLOW,
    LISTING_UNAVAILABLE,
    LISTING_UNREADABLE,
    ListingCoalescer,
)


def _printer(**over):
    base = {"id": 1, "connector_identity": "p-1", "ip_address": "10.0.0.5", "access_code": "12345678"}
    base.update(over)
    return SimpleNamespace(**base)


class GatedTransport:
    def __init__(self):
        self.calls = 0
        self.gate = asyncio.Event()
        self.answer = (LISTING_COMPLETE, [RemoteFile(name="a.3mf", path="/a.3mf", size=1)], None)

    async def list_directory(self, path, file_type="model"):
        self.calls += 1
        await self.gate.wait()
        return self.answer


@pytest.mark.asyncio
async def test_a_refresh_joins_the_listing_still_running():
    coalescer, transport = ListingCoalescer(), GatedTransport()
    first = asyncio.create_task(coalescer.list(_printer(), "external", "/", "model", transport, wait=5))
    second = asyncio.create_task(coalescer.list(_printer(), "external", "/", "model", transport, wait=5))
    await asyncio.sleep(0.05)
    transport.gate.set()
    answers = await asyncio.gather(first, second)
    assert transport.calls == 1
    assert [a.status for a in answers] == [LISTING_COMPLETE, LISTING_COMPLETE]


@pytest.mark.asyncio
async def test_a_cancelled_subscriber_does_not_cancel_its_peer():
    coalescer, transport = ListingCoalescer(), GatedTransport()
    leaving = asyncio.create_task(coalescer.list(_printer(), "external", "/", "model", transport, wait=5))
    staying = asyncio.create_task(coalescer.list(_printer(), "external", "/", "model", transport, wait=5))
    await asyncio.sleep(0.05)
    leaving.cancel()
    await asyncio.gather(leaving, return_exceptions=True)
    transport.gate.set()
    assert (await staying).status == LISTING_COMPLETE
    assert transport.calls == 1


@pytest.mark.asyncio
async def test_a_wait_that_runs_out_is_slow_with_the_last_listing_not_an_empty_one():
    coalescer, transport = ListingCoalescer(), GatedTransport()
    transport.gate.set()
    assert (await coalescer.list(_printer(), "external", "/", "model", transport, wait=5)).status == LISTING_COMPLETE
    transport.gate.clear()
    slow = await coalescer.list(_printer(), "external", "/", "model", transport, wait=0.05)
    assert slow.status == LISTING_SLOW
    assert slow.stale is True
    assert [f.name for f in slow.files] == ["a.3mf"]
    # The slow answer opened no second listing: the one still running is joined.
    again = await coalescer.list(_printer(), "external", "/", "model", transport, wait=0.05)
    assert again.status == LISTING_SLOW
    assert transport.calls == 2
    transport.gate.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [LISTING_UNREADABLE, LISTING_MISSING, LISTING_UNAVAILABLE])
async def test_every_non_answer_keeps_its_own_name(status):
    coalescer, transport = ListingCoalescer(), GatedTransport()
    transport.gate.set()
    transport.answer = (status, [], "550" if status == LISTING_UNREADABLE else None)
    answer = await coalescer.list(_printer(), "external", "/record", "model", transport, wait=5)
    assert answer.status == status
    assert answer.files == []


@pytest.mark.asyncio
async def test_an_unavailable_printer_shows_the_last_listing_as_stale():
    coalescer, transport = ListingCoalescer(), GatedTransport()
    transport.gate.set()
    await coalescer.list(_printer(), "external", "/", "model", transport, wait=5)
    transport.answer = (LISTING_UNAVAILABLE, [], "timeout")
    answer = await coalescer.list(_printer(), "external", "/", "model", transport, wait=5)
    assert (answer.status, answer.stale, [f.name for f in answer.files]) == (LISTING_UNAVAILABLE, True, ["a.3mf"])


@pytest.mark.asyncio
async def test_a_changed_endpoint_never_joins_an_old_listing():
    coalescer, transport = ListingCoalescer(), GatedTransport()
    a = asyncio.create_task(coalescer.list(_printer(), "external", "/", "model", transport, wait=5))
    b = asyncio.create_task(
        coalescer.list(_printer(access_code="87654321"), "external", "/", "model", transport, wait=5)
    )
    c = asyncio.create_task(
        coalescer.list(_printer(ip_address="10.0.0.6"), "external", "/", "model", transport, wait=5)
    )
    d = asyncio.create_task(coalescer.list(_printer(), "internal", "/", "model", transport, wait=5))
    await asyncio.sleep(0.05)
    transport.gate.set()
    await asyncio.gather(a, b, c, d)
    assert transport.calls == 4


@pytest.mark.asyncio
async def test_a_transport_that_raises_reads_as_unavailable():
    class Broken:
        async def list_directory(self, path, file_type="model"):
            raise RuntimeError("bug")

    answer = await ListingCoalescer().list(_printer(), "external", "/", "model", Broken(), wait=5)
    assert answer.status == LISTING_UNAVAILABLE
