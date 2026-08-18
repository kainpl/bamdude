"""Work aimed at a printer class wakes a printer, instead of waiting for ever.

Ported from upstream `14d0d143` (#2786). Their shape: power-on lived inside
``if item.printer_id:`` and class-targeted items carry no printer id. Ours
differs and the outcome is identical — the per-printer loop always has a printer
so power-on always applies there, while the class tier is the auto-queue, and
its matcher drops a disconnected candidate with ``continue``.

⚠️ That gate has to stay: routing matches the filament actually loaded, which is
live MQTT state, and a printer that is off reports none. What was missing is the
step taken when the gate is the *only* thing in the way.

⚠️ ``offline_candidates_for`` deliberately shares its query with the matcher.
Waking a printer the matcher would have rejected leaves the job just as stuck,
with the printer now drawing power.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.app.services.auto_queue_eligibility import offline_candidates_for


def _printer(pid: int, name: str = "P"):
    return SimpleNamespace(id=pid, name=f"{name}{pid}")


class _FakePM:
    def __init__(self, connected: set[int], awaiting: set[int] = frozenset()):
        self._connected = connected
        self._awaiting = awaiting

    def is_connected(self, pid: int) -> bool:
        return pid in self._connected

    def is_awaiting_plate_clear(self, pid: int) -> bool:
        return pid in self._awaiting


async def _candidates(printers, *, connected, awaiting=frozenset(), busy=frozenset()):
    with (
        patch(
            "backend.app.services.auto_queue_eligibility.printers_for_item",
            return_value=(printers, "P1S", ""),
        ),
        patch("backend.app.services.printer_manager.printer_manager", _FakePM(connected, awaiting)),
    ):
        return await offline_candidates_for(None, SimpleNamespace(id=1), set(busy))


@pytest.mark.asyncio
class TestWhoMayBeWoken:
    async def test_a_printer_that_is_simply_off(self):
        got = await _candidates([_printer(1)], connected=set())

        assert [p.id for p in got] == [1]

    async def test_never_one_that_is_already_online(self):
        """It is not eligible for some other reason — filament, most likely —
        and switching a plug that is already on achieves nothing."""
        got = await _candidates([_printer(1)], connected={1})

        assert got == []

    async def test_never_one_awaiting_plate_clear(self):
        """It would boot into IDLE and be held by that gate anyway. The flag is
        ours and persisted, so it reads while the printer is still off."""
        got = await _candidates([_printer(1)], connected=set(), awaiting={1})

        assert got == []

    async def test_never_one_already_claimed_this_pass(self):
        got = await _candidates([_printer(1), _printer(2)], connected=set(), busy={1})

        assert [p.id for p in got] == [2]

    async def test_a_job_with_no_eligible_printers_wakes_nothing(self):
        assert await _candidates([], connected=set()) == []


@pytest.mark.asyncio
class TestTheCandidateSetIsTheMatchersOwn:
    async def test_it_asks_printers_for_item_rather_than_its_own_query(self):
        """The guarantee that a woken printer is one the job could have run on.
        A second query would drift, and the drift that matters is powering up a
        machine the file can never legally print on."""
        with (
            patch(
                "backend.app.services.auto_queue_eligibility.printers_for_item",
                return_value=([], "P1S", ""),
            ) as spy,
            patch("backend.app.services.printer_manager.printer_manager", _FakePM(set())),
        ):
            await offline_candidates_for(None, SimpleNamespace(id=1), set())

        assert spy.called
