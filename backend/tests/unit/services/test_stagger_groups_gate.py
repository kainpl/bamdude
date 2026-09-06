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


class TestQueuePreRegister:
    """The queue path takes its slot before spawning the dispatch task, and the
    id it takes it under is what every group question later reads."""

    def test_the_slot_is_taken_under_the_printers_id(self, scheduler):
        printer = SimpleNamespace(id=7, stagger_interval_minutes=0)
        scheduler._pre_register_stagger_slot(printer, 300)
        assert [(s.printer_id, s.interval_seconds) for s in scheduler._stagger_slots] == [(7, 300)]

    def test_the_per_printer_interval_beats_the_farm_default(self, scheduler):
        printer = SimpleNamespace(id=7, stagger_interval_minutes=2)
        scheduler._pre_register_stagger_slot(printer, 300)
        assert scheduler._stagger_slots[0].interval_seconds == 120

    def test_the_phase_it_occupies_is_the_printers_own(self, scheduler):
        """Registered under anything but the printer's id — the queue row's id,
        say — the slot would be counted against whatever groups THAT id wears.
        Here an untagged id would be a wildcard and shut every phase; printer 1
        is on phase 1, so phase 2 must stay open.
        """
        r = _phases()
        scheduler._pre_register_stagger_slot(SimpleNamespace(id=1, stagger_interval_minutes=0), 300)
        assert scheduler._can_start_staggered(1, 4, r) is False  # phase 1 is P1's, and it is full
        assert scheduler._can_start_staggered(1, 2, r) is True  # phase 2 was never touched


class TestSelfAndGlobal:
    def test_a_printer_never_blocks_itself(self, scheduler):
        r = _phases()
        _gate(scheduler, r, 1, [1])
        assert scheduler._can_start_staggered(1, 1, r) is True  # its own slot, not another's

    def test_the_self_exclusion_holds_on_the_global_resolver_too(self, scheduler):
        """The queue tick asks about a printer; the legacy call shape asks about
        the farm. Same one slot, opposite answers — and it must stay that way,
        or cap 1 refuses the very print that took the slot.
        """
        scheduler._register_stagger_start(1, 120)
        assert scheduler._can_start_staggered(1, 1, StaggerGroupResolver.global_only()) is True
        assert scheduler._can_start_staggered(1) is False  # no printer named: nobody is excluded

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


def _phases_with_limits(tag_limits: dict[int, int]) -> StaggerGroupResolver:
    return StaggerGroupResolver(
        StaggerSplit(by_tags=True, tag_ids=frozenset(TAGS), tag_limits=tag_limits),
        tags_by_printer={k: frozenset(v) for k, v in LINKS.items()},
        tag_names=TAGS,
        location_by_printer={},
        parent_by_location={},
        location_names={},
    )


class TestPerGroupLimit:
    def test_a_tag_limit_below_the_global_cap_holds_that_phase_only(self, scheduler):
        first_tag = min(TAGS)
        r = _phases_with_limits({first_tag: 1})
        on_first = [pid for pid, tags in LINKS.items() if first_tag in tags]
        # Two printers of the limited phase: only one starts; a printer of another phase still starts.
        other = next(pid for pid, tags in LINKS.items() if first_tag not in tags)
        started = _gate(scheduler, r, 2, [on_first[0], on_first[1], other])
        assert started == [on_first[0], other]

    def test_a_wildcard_is_held_by_the_tightest_group(self, scheduler):
        first_tag = min(TAGS)
        r = _phases_with_limits({first_tag: 1})
        on_first = next(pid for pid, tags in LINKS.items() if first_tag in tags)
        untagged = max(LINKS) + 1000  # no links → wildcard
        assert _gate(scheduler, r, 2, [on_first, untagged]) == [on_first]


@pytest.mark.asyncio
async def test_the_snapshot_reports_each_groups_own_cap(scheduler, monkeypatch):
    first_tag = min(TAGS)
    r = _phases_with_limits({first_tag: 1})

    async def _settings(_db):
        return True, 2, 120, True

    async def _resolver(_db):
        return r

    monkeypatch.setattr(scheduler, "_get_stagger_settings", _settings)
    monkeypatch.setattr(scheduler, "_load_stagger_resolver", _resolver)
    snapshot = await scheduler.get_stagger_state_snapshot(db=None)
    caps = {g["tag_id"]: g["cap"] for g in snapshot["groups"]}
    assert caps[first_tag] == 1
    assert all(cap == 2 for tag_id, cap in caps.items() if tag_id != first_tag)
    assert all(g["free_slots"] == g["cap"] - g["occupied"] for g in snapshot["groups"])
