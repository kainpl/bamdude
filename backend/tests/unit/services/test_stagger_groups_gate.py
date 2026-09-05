"""Three phases, cap one: one printer heats per phase, the fourth waits on its own phase."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.stagger_groups import GLOBAL, StaggerGroupResolver, StaggerSplit

TAGS = {1: "Фаза 1", 2: "Фаза 2", 3: "Фаза 3"}
# printers 1,4 on phase 1; 2,5 on phase 2; 3 on phase 3; 9 untagged
LINKS = {1: {1}, 4: {1}, 2: {2}, 5: {2}, 3: {3}}


class _FakeManager:
    def __init__(self, states=None):
        self._states = states or {}

    def get_status(self, printer_id):
        state = self._states.get(printer_id)
        return SimpleNamespace(state=state, temperatures={}, raw_data={}) if state else None

    def get_printer(self, printer_id):
        return SimpleNamespace(name=f"P{printer_id}")


def _phases() -> StaggerGroupResolver:
    return StaggerGroupResolver(
        StaggerSplit(by_tags=True, tag_ids=frozenset(TAGS)),
        tags_by_printer={k: frozenset(v) for k, v in LINKS.items()},
        tag_names=TAGS,
        location_by_printer={},
        parent_by_location={},
        location_names={},
    )


@pytest.fixture
def scheduler():
    return PrintScheduler()


def _gate(scheduler, resolver, concurrent, printer_ids):
    started = []
    for pid in printer_ids:
        if scheduler._can_start_staggered(concurrent, pid, resolver):
            scheduler._register_stagger_start(pid, 120)
            started.append(pid)
    return started


class TestPerGroupCap:
    def test_one_start_per_phase(self, scheduler):
        assert _gate(scheduler, _phases(), 1, [1, 2, 3, 4, 5]) == [1, 2, 3]

    def test_a_free_phase_passes_while_another_is_full(self, scheduler):
        r = _phases()
        _gate(scheduler, r, 1, [1, 4])  # phase 1 full, 4 waits
        assert scheduler._can_start_staggered(1, 4, r) is False
        assert scheduler._can_start_staggered(1, 3, r) is True

    def test_the_reason_names_the_full_phase_and_who_is_heating(self, scheduler):
        r = _phases()
        _gate(scheduler, r, 1, [1, 2])
        with patch("backend.app.services.print_scheduler.printer_manager", _FakeManager()):
            reason = scheduler._stagger_reason(True, 1, 4, r)
        assert reason == "Staggered start [Фаза 1]: waiting for P1 to heat up"

    def test_the_interval_reason_carries_the_group_too(self, scheduler):
        r = _phases()
        _gate(scheduler, r, 1, [1])
        assert scheduler._stagger_reason(False, 1, 4, r) == "Staggered start [Фаза 1]: waiting for interval"


class TestWildcard:
    def test_it_waits_while_any_phase_is_full(self, scheduler):
        r = _phases()
        _gate(scheduler, r, 1, [2])  # only phase 2 busy
        assert scheduler._can_start_staggered(1, 9, r) is False
        assert scheduler._can_start_staggered(2, 9, r) is True

    def test_its_slot_counts_in_every_phase(self, scheduler):
        r = _phases()
        _gate(scheduler, r, 1, [9])
        assert _gate(scheduler, r, 1, [1, 2, 3]) == []
        with patch("backend.app.services.print_scheduler.printer_manager", _FakeManager()):
            assert scheduler._stagger_reason(True, 1, 1, r) == "Staggered start [Фаза 1]: waiting for P9 to heat up"


class TestSelfAndGlobal:
    def test_a_printer_never_blocks_itself(self, scheduler):
        r = _phases()
        _gate(scheduler, r, 1, [1])
        assert scheduler._can_start_staggered(1, 1, r) is True  # its own slot, not another's

    def test_the_global_resolver_is_todays_gate(self, scheduler):
        r = StaggerGroupResolver.global_only()
        assert _gate(scheduler, r, 2, [1, 2, 3]) == [1, 2]
        assert scheduler._can_start_staggered(2) is False  # the legacy call shape still answers
        with patch("backend.app.services.print_scheduler.printer_manager", _FakeManager()):
            assert scheduler._stagger_reason(True) == "Staggered start: waiting for P1, P2 to heat up"
            assert scheduler._stagger_reason(True, 2, 3, r) == "Staggered start: waiting for P1, P2 to heat up"
        assert r.groups_for(3) == {GLOBAL}


@pytest.mark.asyncio
async def test_the_snapshot_reports_one_entry_per_group(scheduler):
    r = _phases()
    _gate(scheduler, r, 1, [1, 9])  # phase 1 by P1; then P9 waits — register it anyway to see the wildcard flag
    scheduler._register_stagger_start(9, 120)
    with (
        patch.object(PrintScheduler, "_get_stagger_settings", AsyncMock(return_value=(True, 1, 300, True))),
        patch("backend.app.services.print_scheduler.StaggerSplit.from_settings", AsyncMock(return_value=r.split)),
        patch("backend.app.services.print_scheduler.StaggerGroupResolver.load", AsyncMock(return_value=r)),
        patch("backend.app.services.print_scheduler.printer_manager", _FakeManager()),
    ):
        snap = await scheduler.get_stagger_state_snapshot(db=None)

    assert snap["split"] == {"by_tags": True, "by_location": False}
    assert [g["label"] for g in snap["groups"]] == ["Фаза 1", "Фаза 2", "Фаза 3"]
    phase1, phase2, _ = snap["groups"]
    assert phase1["occupied"] == 2 and phase1["free_slots"] == 0
    assert sorted(s["printer_id"] for s in phase1["slots"]) == [1, 9]
    assert {s["printer_id"]: s["wildcard"] for s in phase1["slots"]} == {1: False, 9: True}
    assert phase2["occupied"] == 1 and phase2["slots"][0]["printer_id"] == 9
    assert "slots" not in snap and "free_slots" not in snap  # the flat shape is gone (decision 10)


@pytest.mark.asyncio
async def test_stagger_blocks_answers_for_the_printer_asked(scheduler):
    r = _phases()
    _gate(scheduler, r, 1, [1])
    with (
        patch("backend.app.services.print_scheduler.async_session"),
        patch.object(PrintScheduler, "_get_stagger_settings", AsyncMock(return_value=(True, 1, 300, True))),
        patch.object(PrintScheduler, "_load_stagger_resolver", AsyncMock(return_value=r)),
        patch("backend.app.services.print_scheduler.printer_manager", _FakeManager({1: "PREPARE"})),
    ):
        assert await scheduler.stagger_blocks(4) is True  # phase 1 is full
        assert await scheduler.stagger_blocks(2) is False  # phase 2 is free
        assert await scheduler.stagger_blocks(1) is False  # its own slot
