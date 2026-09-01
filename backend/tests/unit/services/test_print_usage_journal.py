"""The append-only usage journal: record/load, spool-id freezing, retention."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from backend.app.models.print_usage_event import (
    EVENT_PAUSE,
    EVENT_RUNOUT,
    EVENT_SPOOL_LOADED,
    EVENT_TRAY_CHANGE,
    KIND_PAUSE,
    PrintUsageEvent,
)
from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.services.print_usage_journal import (
    delete_for_archive,
    freeze_spool_ids,
    load_events,
    prune_finished,
    record_event,
)


@pytest.fixture
async def printer(db_session):
    p = Printer(name="P1", ip_address="10.0.0.2", serial_number="SN-journal", access_code="123")
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


async def _make_archive(db_session, printer, status="printing"):
    from backend.app.models.archive import PrintArchive

    a = PrintArchive(
        printer_id=printer.id,
        filename="j.gcode.3mf",
        file_path="",
        file_size=0,
        print_name="j",
        status=status,
    )
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)
    return a


@pytest.mark.asyncio
async def test_record_and_load_round_trip_in_order(db_session, printer):
    archive = await _make_archive(db_session, printer)
    await record_event(
        db_session,
        printer_id=printer.id,
        archive_id=archive.id,
        layer_num=0,
        event=EVENT_TRAY_CHANGE,
        global_tray_id=1,
    )
    await record_event(
        db_session,
        printer_id=printer.id,
        archive_id=archive.id,
        layer_num=42,
        event=EVENT_RUNOUT,
        kind=KIND_PAUSE,
        global_tray_id=1,
        spool_id=7,
    )
    events = await load_events(db_session, printer.id, archive.id)
    assert [(e.event, e.layer_num, e.spool_id) for e in events] == [
        ("tray_change", 0, None),
        ("runout", 42, 7),
    ]


@pytest.mark.asyncio
async def test_freeze_resolves_both_backends(db_session, printer):
    spool = Spool(material="PLA", label_weight=1000)
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    db_session.add(SpoolAssignment(printer_id=printer.id, ams_id=0, tray_id=1, spool_id=spool.id))
    db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=1, spoolman_spool_id=99))
    await db_session.commit()

    assert await freeze_spool_ids(db_session, printer.id, 1) == (spool.id, 99)
    # Unassigned slot freezes to nothing rather than guessing.
    assert await freeze_spool_ids(db_session, printer.id, 3) == (None, None)


@pytest.mark.asyncio
async def test_freeze_decodes_external_and_ht_trays(db_session, printer):
    db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=255, tray_id=0, spoolman_spool_id=5))
    db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=128, tray_id=0, spoolman_spool_id=6))
    await db_session.commit()
    assert (await freeze_spool_ids(db_session, printer.id, 254))[1] == 5
    assert (await freeze_spool_ids(db_session, printer.id, 128))[1] == 6


@pytest.mark.asyncio
async def test_prune_spares_active_prints_and_fresh_rows(db_session, printer):
    printing = await _make_archive(db_session, printer, status="printing")
    finished = await _make_archive(db_session, printer, status="completed")

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=100)
    for archive in (printing, finished):
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=1,
                event=EVENT_TRAY_CHANGE,
            )
        )
    await db_session.commit()
    # Backdate both rows past any retention window.
    await db_session.execute(text("UPDATE print_usage_events SET created_at = :old"), {"old": old})
    await db_session.commit()

    deleted = await prune_finished(db_session, retention_hours=72)
    assert deleted == 1  # only the finished archive's row

    remaining = (await db_session.execute(select(PrintUsageEvent))).scalars().all()
    assert [e.archive_id for e in remaining] == [printing.id]


@pytest.mark.asyncio
async def test_delete_for_archive_cleans_sqlite_without_fk_cascade(db_session, printer):
    archive = await _make_archive(db_session, printer, status="completed")
    db_session.add(PrintUsageEvent(printer_id=printer.id, archive_id=archive.id, layer_num=1, event=EVENT_TRAY_CHANGE))
    await db_session.commit()

    await delete_for_archive(db_session, archive.id)
    assert (await db_session.execute(select(PrintUsageEvent))).scalars().all() == []


class TestAssignmentChangeAfterRunout:
    """Tagless replacement path: assigning a spool to a slot that ran out IS
    the replacement signal — the RFID uuid-watch cannot see external holders."""

    async def _runout(self, db_session, printer, archive, spool_id, tray=254):
        from backend.app.models.print_usage_event import EVENT_RUNOUT, KIND_EXTERNAL

        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=80,
                event=EVENT_RUNOUT,
                kind=KIND_EXTERNAL,
                global_tray_id=tray,
                spool_id=spool_id,
            )
        )
        await db_session.commit()

    @pytest.mark.asyncio
    async def test_new_assignment_journals_spool_loaded(self, db_session, printer):
        from backend.app.services.print_usage_journal import note_assignment_change

        archive = await _make_archive(db_session, printer)
        await self._runout(db_session, printer, archive, spool_id=7)

        await note_assignment_change(db_session, printer_id=printer.id, ams_id=255, tray_id=0, spool_id=9, layer_num=85)

        events = await load_events(db_session, printer.id, archive.id)
        assert [(e.event, e.global_tray_id, e.spool_id, e.layer_num) for e in events[-1:]] == [
            ("spool_loaded", 254, 9, 85)
        ]

    @pytest.mark.asyncio
    async def test_reassigning_the_same_reel_is_not_a_replacement(self, db_session, printer):
        from backend.app.services.print_usage_journal import note_assignment_change

        archive = await _make_archive(db_session, printer)
        await self._runout(db_session, printer, archive, spool_id=7)

        await note_assignment_change(db_session, printer_id=printer.id, ams_id=255, tray_id=0, spool_id=7, layer_num=85)
        events = await load_events(db_session, printer.id, archive.id)
        assert [e.event for e in events] == ["runout"]

    @pytest.mark.asyncio
    async def test_second_assignment_does_not_duplicate(self, db_session, printer):
        from backend.app.services.print_usage_journal import note_assignment_change

        archive = await _make_archive(db_session, printer)
        await self._runout(db_session, printer, archive, spool_id=7)

        await note_assignment_change(db_session, printer_id=printer.id, ams_id=255, tray_id=0, spool_id=9, layer_num=85)
        await note_assignment_change(db_session, printer_id=printer.id, ams_id=255, tray_id=0, spool_id=9, layer_num=90)
        events = await load_events(db_session, printer.id, archive.id)
        assert [e.event for e in events] == ["runout", "spool_loaded"]

    @pytest.mark.asyncio
    async def test_assignment_without_a_runout_is_a_noop(self, db_session, printer):
        from backend.app.services.print_usage_journal import note_assignment_change

        archive = await _make_archive(db_session, printer)
        await note_assignment_change(db_session, printer_id=printer.id, ams_id=0, tray_id=1, spool_id=9, layer_num=85)
        assert await load_events(db_session, printer.id, archive.id) == []

    @pytest.mark.asyncio
    async def test_ams_tray_with_untagged_spool_gets_the_same_path(self, db_session, printer):
        """Regular AMS slot, no RFID: runout on AMS0-T2 (global 2), the user
        swaps the reel and re-assigns — the boundary lands on that tray."""
        from backend.app.models.print_usage_event import EVENT_RUNOUT, KIND_PAUSE
        from backend.app.services.print_usage_journal import note_assignment_change

        archive = await _make_archive(db_session, printer)
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=80,
                event=EVENT_RUNOUT,
                kind=KIND_PAUSE,
                global_tray_id=2,
                spool_id=7,
            )
        )
        await db_session.commit()

        await note_assignment_change(db_session, printer_id=printer.id, ams_id=0, tray_id=2, spool_id=9, layer_num=85)

        events = await load_events(db_session, printer.id, archive.id)
        assert [(e.event, e.global_tray_id, e.spool_id) for e in events[-1:]] == [("spool_loaded", 2, 9)]


class TestManualReplacementIntent:
    """The mid-pause prompt's "replacement" answer: a manual runout journaled
    with the OUTGOING spool frozen, so the assignment that follows becomes the
    spool_loaded boundary. Only a paused print accepts the declaration —
    everything else returns False and journals nothing."""

    def _paused_state(self, monkeypatch, state="PAUSE", layer=120):
        from types import SimpleNamespace

        from backend.app.services import printer_manager as pm_module

        monkeypatch.setattr(
            pm_module.printer_manager,
            "get_status",
            lambda printer_id: SimpleNamespace(state=state, layer_num=layer),
        )

    @pytest.mark.asyncio
    async def test_declared_replacement_journals_the_manual_boundary(self, db_session, printer, monkeypatch):
        from backend.app.services.print_usage_journal import (
            note_assignment_change,
            note_manual_replacement_intent,
        )
        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        archive = await _make_archive(db_session, printer)
        db_session.add(SpoolAssignment(spool_id=31, printer_id=printer.id, ams_id=255, tray_id=0, fingerprint_color=""))
        await db_session.commit()
        self._paused_state(monkeypatch)

        assert await note_manual_replacement_intent(db_session, printer_id=printer.id, ams_id=255, tray_id=0)
        # The very assignment the human is making — closes the episode.
        await note_assignment_change(db_session, printer_id=printer.id, ams_id=255, tray_id=0, spool_id=32)

        events = await load_events(db_session, printer.id, archive.id)
        assert [(e.event, e.kind, e.global_tray_id, e.spool_id) for e in events] == [
            ("runout", "manual", 254, 31),
            ("spool_loaded", None, 254, 32),
        ]
        assert journal_boundaries_for_tray(events, 254) == [(0, 31, None), (120, 32, None)]

    @pytest.mark.asyncio
    async def test_after_resume_the_boundary_lands_on_the_pause_layer(self, db_session, printer, monkeypatch):
        # Real workflow: pause -> swap -> resume from the printer's screen ->
        # only then the UI. The swap happened at the pause, so the boundary
        # must land on the pause layer, not on the click's layer.
        from backend.app.models.print_usage_event import EVENT_PAUSE
        from backend.app.services.print_usage_journal import note_manual_replacement_intent

        archive = await _make_archive(db_session, printer)
        # The slot must hold a spool for the declaration to mean anything —
        # a replacement charges what printed so far to the reel that came out.
        db_session.add(SpoolAssignment(spool_id=31, printer_id=printer.id, ams_id=255, tray_id=0, fingerprint_color=""))
        await record_event(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=87,
            event=EVENT_PAUSE,
            global_tray_id=254,
        )
        self._paused_state(monkeypatch, state="RUNNING", layer=140)

        assert await note_manual_replacement_intent(db_session, printer_id=printer.id, ams_id=255, tray_id=0)
        events = await load_events(db_session, printer.id, archive.id)
        runout = [e for e in events if e.event == "runout"]
        assert [(e.kind, e.layer_num, e.global_tray_id) for e in runout] == [("manual", 87, 254)]

    @pytest.mark.asyncio
    async def test_a_running_print_refuses_the_declaration(self, db_session, printer, monkeypatch):
        from backend.app.services.print_usage_journal import note_manual_replacement_intent

        archive = await _make_archive(db_session, printer)
        self._paused_state(monkeypatch, state="RUNNING")
        assert not await note_manual_replacement_intent(db_session, printer_id=printer.id, ams_id=255, tray_id=0)
        assert await load_events(db_session, printer.id, archive.id) == []

    @pytest.mark.asyncio
    async def test_no_active_print_refuses_the_declaration(self, db_session, printer, monkeypatch):
        from backend.app.services.print_usage_journal import note_manual_replacement_intent

        self._paused_state(monkeypatch)
        assert not await note_manual_replacement_intent(db_session, printer_id=printer.id, ams_id=255, tray_id=0)


class TestRunoutEpisodeRows:
    @pytest.mark.asyncio
    async def test_closed_episode_gets_a_new_row(self, db_session, printer):
        from backend.app.services.print_usage_journal import record_runout

        archive = await _make_archive(db_session, printer)
        await record_runout(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=80,
            kind="pause",
            global_tray_id=254,
            spool_id=7,
        )
        # Same open episode replayed (restart / HMS flicker) — kind upsert only.
        await record_runout(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=95,
            kind="pause",
            global_tray_id=254,
            spool_id=7,
        )
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=80,
                event="spool_loaded",
                global_tray_id=254,
                spool_id=9,
            )
        )
        await db_session.commit()
        # The replacement reel runs out too — a NEW episode, its own row.
        await record_runout(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=250,
            kind="pause",
            global_tray_id=254,
            spool_id=9,
        )
        events = await load_events(db_session, printer.id, archive.id)
        assert [(e.event, e.layer_num, e.spool_id) for e in events] == [
            ("runout", 80, 7),
            ("spool_loaded", 80, 9),
            ("runout", 250, 9),
        ]

    @pytest.mark.asyncio
    async def test_late_assignment_corrects_a_stale_spool_loaded(self, db_session, printer):
        """RFID refill race: the uuid-watch fires on the first push with the
        new tag and freezes whatever assignment exists at that instant — which
        can still be the OLD spool (auto-assign hasn't committed yet). The
        later assignment must CORRECT the spool_loaded row, not be skipped."""
        from backend.app.services.print_usage_journal import note_assignment_change

        archive = await _make_archive(db_session, printer)
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=80,
                event="runout",
                kind="external",
                global_tray_id=254,
                spool_id=26,
            )
        )
        # uuid-watch fired first, froze the stale assignment (old spool 26).
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=80,
                event="spool_loaded",
                global_tray_id=254,
                spool_id=26,
            )
        )
        await db_session.commit()

        await note_assignment_change(
            db_session, printer_id=printer.id, ams_id=255, tray_id=0, spool_id=71, layer_num=85
        )

        events = await load_events(db_session, printer.id, archive.id)
        loaded = [e for e in events if e.event == "spool_loaded"]
        assert len(loaded) == 1
        assert loaded[0].spool_id == 71  # corrected, not duplicated, not skipped

    @pytest.mark.asyncio
    async def test_late_assignment_fills_an_empty_spool_loaded(self, db_session, printer):
        from backend.app.services.print_usage_journal import note_assignment_change

        archive = await _make_archive(db_session, printer)
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=80,
                event="runout",
                kind="external",
                global_tray_id=254,
                spool_id=26,
            )
        )
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=80,
                event="spool_loaded",
                global_tray_id=254,
                spool_id=None,
            )
        )
        await db_session.commit()

        await note_assignment_change(
            db_session, printer_id=printer.id, ams_id=255, tray_id=0, spool_id=71, layer_num=85
        )
        events = await load_events(db_session, printer.id, archive.id)
        loaded = [e for e in events if e.event == "spool_loaded"]
        assert [(e.spool_id,) for e in loaded] == [(71,)]


class TestRunoutSpoolLineage:
    """The unlink race (X2D 2026-08-23): by the time the runout code fires,
    the slot's assignment can already be gone — the AMS-empty report unlinked
    it moments earlier, or a user unassigned by hand. The journal itself
    remembers which reel fed the tray; inheriting from its own prior rows is
    recorded lineage, not guessing."""

    @pytest.mark.asyncio
    async def test_runout_inherits_the_spool_from_the_start_row(self, db_session, printer):
        from backend.app.services.print_usage_journal import record_runout

        archive = await _make_archive(db_session, printer)
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=0,
                event="start",
                global_tray_id=2,
                spool_id=71,
                spoolman_spool_id=5,
            )
        )
        await db_session.commit()
        await record_runout(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=25,
            kind="pause",
            global_tray_id=2,
            spool_id=None,
        )
        events = await load_events(db_session, printer.id, archive.id)
        assert (events[-1].event, events[-1].spool_id, events[-1].spoolman_spool_id) == ("runout", 71, 5)

    @pytest.mark.asyncio
    async def test_no_lineage_stays_none(self, db_session, printer):
        from backend.app.services.print_usage_journal import record_runout

        archive = await _make_archive(db_session, printer)
        await record_runout(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=25,
            kind="pause",
            global_tray_id=2,
            spool_id=None,
        )
        events = await load_events(db_session, printer.id, archive.id)
        assert (events[-1].event, events[-1].spool_id) == ("runout", None)

    @pytest.mark.asyncio
    async def test_lineage_never_overrides_a_frozen_spool(self, db_session, printer):
        from backend.app.services.print_usage_journal import record_runout

        archive = await _make_archive(db_session, printer)
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=0,
                event="start",
                global_tray_id=2,
                spool_id=71,
            )
        )
        await db_session.commit()
        await record_runout(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=25,
            kind="pause",
            global_tray_id=2,
            spool_id=88,
        )
        events = await load_events(db_session, printer.id, archive.id)
        assert (events[-1].event, events[-1].spool_id) == ("runout", 88)


class TestTraylessRunoutFolding:
    """A second runout code the resolver could not map to a tray (the X2D
    fired 07008011 24s after the per-slot code) adds nothing when a tray-ful
    episode is already open — journal the timeline once, not per code."""

    @pytest.mark.asyncio
    async def test_trayless_runout_is_folded_into_an_open_episode(self, db_session, printer):
        from backend.app.services.print_usage_journal import record_runout

        archive = await _make_archive(db_session, printer)
        await record_runout(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=25,
            kind="pause",
            global_tray_id=2,
            spool_id=71,
        )
        await record_runout(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=25,
            kind="pause",
            global_tray_id=None,
        )
        events = await load_events(db_session, printer.id, archive.id)
        assert [(e.event, e.global_tray_id) for e in events] == [("runout", 2)]

    @pytest.mark.asyncio
    async def test_trayless_runout_alone_is_still_timeline_worthy(self, db_session, printer):
        from backend.app.services.print_usage_journal import record_runout

        archive = await _make_archive(db_session, printer)
        await record_runout(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=25,
            kind="pause",
            global_tray_id=None,
        )
        events = await load_events(db_session, printer.id, archive.id)
        assert [(e.event, e.global_tray_id) for e in events] == [("runout", None)]

    @pytest.mark.asyncio
    async def test_trayless_runout_after_a_closed_episode_is_recorded(self, db_session, printer):
        from backend.app.services.print_usage_journal import record_runout

        archive = await _make_archive(db_session, printer)
        await record_runout(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=25,
            kind="pause",
            global_tray_id=2,
            spool_id=71,
        )
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=25,
                event="spool_loaded",
                global_tray_id=2,
                spool_id=88,
            )
        )
        await db_session.commit()
        await record_runout(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=180,
            kind="external",
            global_tray_id=None,
        )
        events = await load_events(db_session, printer.id, archive.id)
        assert [(e.event, e.global_tray_id) for e in events] == [
            ("runout", 2),
            ("spool_loaded", 2),
            ("runout", None),
        ]


class TestTheReplacementWindowAsksAboutTheSLOT:
    """⚠️ "Is a replacement plausible" is not a property of the printer alone.

    The window used to answer from the print's state only — paused, or paused
    earlier — so filling an EMPTY slot mid-print raised "replacement or
    correction?", a question with no answer: a replacement charges what printed
    so far to the spool that came OUT, and an empty slot has none.
    ``freeze_spool_ids`` says so itself ("an unassigned slot freezes to nothing
    rather than guessing"), so the replacement branch would journal a boundary
    naming nobody.

    ⚠️ **The test is the ASSIGNMENT, never the tray's contents.** A spool that
    runs out mid-print leaves the tray reading empty while ``on_ams_change``
    deliberately keeps the slot linked ("the spool is still physically in the
    AMS, just consumed"), because that link is the only record of what fed the
    print. Reading emptiness off the AMS would silence the dialog in exactly
    the case it exists for.
    """

    async def _paused_print(self, db_session, printer, monkeypatch):
        from types import SimpleNamespace

        from backend.app.services import print_usage_journal as journal

        archive = await _make_archive(db_session, printer)
        monkeypatch.setattr(
            "backend.app.services.printer_manager.printer_manager.get_status",
            lambda _pid: SimpleNamespace(state="PAUSE", layer_num=42),
        )
        return archive, journal

    async def _assign(self, db_session, printer, ams_id, tray_id):
        spool = Spool(material="PETG", label_weight=1000)
        db_session.add(spool)
        await db_session.commit()
        await db_session.refresh(spool)
        db_session.add(SpoolAssignment(printer_id=printer.id, ams_id=ams_id, tray_id=tray_id, spool_id=spool.id))
        await db_session.commit()
        return spool

    async def test_a_slot_holding_a_spool_still_asks(self, db_session, printer, monkeypatch):
        _archive, journal = await self._paused_print(db_session, printer, monkeypatch)
        await self._assign(db_session, printer, 0, 1)

        window = await journal.manual_replacement_window(db_session, printer.id, ams_id=0, tray_id=1)

        assert window is not None and window["mode"] == "prompt"

    async def test_an_empty_slot_does_not_ask(self, db_session, printer, monkeypatch):
        _archive, journal = await self._paused_print(db_session, printer, monkeypatch)
        # Nothing assigned to AMS0-T3: filling it is not replacing anything.
        window = await journal.manual_replacement_window(db_session, printer.id, ams_id=0, tray_id=3)

        assert window is None

    async def test_a_slot_whose_spool_RAN_OUT_still_asks(self, db_session, printer, monkeypatch):
        """⚠️ The case that makes "empty" the wrong test.

        The reel is consumed and the tray reports empty, but the assignment is
        deliberately kept while the print runs — so the slot is still "holding"
        one as far as the books are concerned, and the question is exactly the
        one worth asking.
        """
        _archive, journal_mod = await self._paused_print(db_session, printer, monkeypatch)
        await self._assign(db_session, printer, 0, 2)

        window = await journal_mod.manual_replacement_window(db_session, printer.id, ams_id=0, tray_id=2)

        assert window is not None and window["mode"] == "prompt"

    async def test_without_a_slot_it_answers_about_the_printer_as_before(self, db_session, printer, monkeypatch):
        """Callers that ask nothing about a slot keep the old answer — the
        endpoint's own back-compat, and the shape every other caller uses."""
        _archive, journal_mod = await self._paused_print(db_session, printer, monkeypatch)

        window = await journal_mod.manual_replacement_window(db_session, printer.id)

        assert window is not None and window["mode"] == "prompt"

    async def test_the_declaration_itself_refuses_an_empty_slot(self, db_session, printer, monkeypatch):
        """Defence in depth: the flag can still arrive from an older client, and
        journaling a boundary that names nobody is worse than ignoring it."""
        _archive, journal_mod = await self._paused_print(db_session, printer, monkeypatch)

        journaled = await journal_mod.note_manual_replacement_intent(
            db_session, printer_id=printer.id, ams_id=0, tray_id=3
        )

        assert journaled is False
        rows = (await db_session.execute(select(PrintUsageEvent))).scalars().all()
        assert [r for r in rows if r.event == EVENT_RUNOUT] == []


class TestTheSeedBelievesTheSLOTSENSOR:
    """Which journaled runouts are stale replays, and which are still real.

    HMS keeps a runout status for the whole print, so a client created
    mid-print must be told what this print already recorded. But seeding a tray
    unconditionally also silences a runout that happened FOR REAL while BamDude
    was down. The printer itself settles it: a slot that physically holds
    filament cannot be running out, so a code still standing over it is stale.

    ⚠️ Presence comes from ``exists`` — decoded from the printer's own
    ``tray_exist_bits`` — and NEVER from ``tray_type``, which BamDude writes
    itself (``ams_filament_setting``) whenever a spool is assigned. Believing
    ``tray_type`` would be believing our own bookkeeping back.
    """

    @staticmethod
    def _state(trays):
        from types import SimpleNamespace

        return SimpleNamespace(raw_data={"ams": [{"id": 0, "tray": [{"id": i, "exists": e} for i, e in trays]}]})

    @staticmethod
    def _ev(event, tray, kind=None, occupied=None, eid=1):
        from types import SimpleNamespace

        return SimpleNamespace(id=eid, event=event, kind=kind, global_tray_id=tray, slot_occupied=occupied)

    def test_a_slot_that_holds_filament_seeds_and_stays_quiet(self):
        from backend.app.services.print_usage_journal import stale_runouts_to_seed

        events = [self._ev(EVENT_RUNOUT, 3, "autoswitch")]
        assert stale_runouts_to_seed(events, self._state([(3, True)])) == {3: "autoswitch"}

    def test_an_empty_slot_is_not_seeded_so_a_real_runout_survives(self):
        # The blind spot this closes: a reel that genuinely ran out while
        # BamDude was down leaves the slot EMPTY, and its runout must still be
        # recorded when we come back.
        from backend.app.services.print_usage_journal import stale_runouts_to_seed

        events = [self._ev(EVENT_RUNOUT, 3, "autoswitch")]
        assert stale_runouts_to_seed(events, self._state([(3, False)])) == {}

    def test_a_books_only_assignment_does_not_open_the_door(self):
        # ⚠️ The residual m161 closes. Assigning a spool to the empty slot in
        # the UI closes the tray's episode WITHOUT a reel going in, so the slot
        # still reads empty and the rule above would let the stale code fire
        # again. The spool_loaded row now says a reel was never there, and that
        # is the difference between bookkeeping and a refill.
        from backend.app.services.print_usage_journal import stale_runouts_to_seed

        events = [
            self._ev(EVENT_RUNOUT, 3, "autoswitch", eid=1),
            self._ev(EVENT_SPOOL_LOADED, 3, occupied=False, eid=2),
        ]
        assert stale_runouts_to_seed(events, self._state([(3, False)])) == {3: "autoswitch"}

    def test_a_real_refill_that_later_empties_is_a_new_runout(self):
        # The mirror: a reel WAS put in (spool_loaded over an occupied slot) and
        # the slot is empty now — it ran out for real while we were down.
        from backend.app.services.print_usage_journal import stale_runouts_to_seed

        events = [
            self._ev(EVENT_RUNOUT, 3, "autoswitch", eid=1),
            self._ev(EVENT_SPOOL_LOADED, 3, occupied=True, eid=2),
        ]
        assert stale_runouts_to_seed(events, self._state([(3, False)])) == {}

    def test_an_unrecorded_closure_keeps_the_old_answer(self):
        # NULL is "no reading", never "empty": rows written before m161 must
        # behave exactly as they did.
        from backend.app.services.print_usage_journal import stale_runouts_to_seed

        events = [
            self._ev(EVENT_RUNOUT, 3, "autoswitch", eid=1),
            self._ev(EVENT_SPOOL_LOADED, 3, occupied=None, eid=2),
        ]
        assert stale_runouts_to_seed(events, self._state([(3, False)])) == {}

    def test_without_a_presence_reading_it_seeds(self):
        # External holders carry no presence bit, and a state that has not
        # arrived yet carries nothing at all. Fall back to the safer, far more
        # frequent case: suppress the replay.
        from backend.app.services.print_usage_journal import stale_runouts_to_seed

        assert stale_runouts_to_seed([self._ev(EVENT_RUNOUT, 254, "external")], self._state([(3, True)])) == {
            254: "external"
        }
        assert stale_runouts_to_seed([self._ev(EVENT_RUNOUT, 3, "autoswitch")], None) == {3: "autoswitch"}

    def test_only_runouts_are_seeded_and_the_newest_kind_wins(self):
        from backend.app.services.print_usage_journal import stale_runouts_to_seed

        events = [
            self._ev(EVENT_TRAY_CHANGE, 3, eid=1),
            self._ev(EVENT_RUNOUT, 3, "ambiguous", eid=2),
            self._ev(EVENT_RUNOUT, 3, "autoswitch", eid=3),
        ]
        assert stale_runouts_to_seed(events, self._state([(3, True)])) == {3: "autoswitch"}


class TestTheJournalRecordsWhetherTheSlotHeldFilament:
    """m161. The journal reconstructs what fed a print AFTER the fact, and two
    of its questions had no recorded answer, only an inference:

    * "the mapped slot never held a spool" — deduced from an ABSENCE (no spool
      id was frozen), which collapses as soon as an operator assigns a spool to
      the empty slot mid-print and a spool id appears for another reason;
    * "this ``spool_loaded`` was a real refill" — versus bookkeeping. Assigning
      in the UI closes the episode whether or not a reel went in.

    The AMS's own presence sensor answers both, so each row now carries it.
    NULL stays "no reading", never "empty".
    """

    @staticmethod
    def _presence(monkeypatch, trays):
        from types import SimpleNamespace

        monkeypatch.setattr(
            "backend.app.services.printer_manager.printer_manager.get_status",
            lambda _pid: SimpleNamespace(
                raw_data={"ams": [{"id": 0, "tray": [{"id": i, "exists": e} for i, e in trays]}]}
            ),
        )

    @pytest.mark.asyncio
    async def test_an_empty_slot_is_recorded_as_empty(self, db_session, printer, monkeypatch):
        archive = await _make_archive(db_session, printer)
        self._presence(monkeypatch, [(2, False)])

        await record_event(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=1,
            event=EVENT_RUNOUT,
            global_tray_id=2,
        )

        events = await load_events(db_session, printer.id, archive.id)
        assert events[-1].slot_occupied is False

    @pytest.mark.asyncio
    async def test_a_filled_slot_is_recorded_as_filled(self, db_session, printer, monkeypatch):
        archive = await _make_archive(db_session, printer)
        self._presence(monkeypatch, [(2, True)])

        await record_event(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=30,
            event=EVENT_TRAY_CHANGE,
            global_tray_id=2,
        )

        events = await load_events(db_session, printer.id, archive.id)
        assert events[-1].slot_occupied is True

    @pytest.mark.asyncio
    async def test_no_reading_stays_unknown(self, db_session, printer, monkeypatch):
        # ⚠️ NULL, not False. An event with no tray (pause/resume), an external
        # holder that has no presence bit, a push that arrived without the
        # bitfield — none of them is evidence the slot was empty.
        archive = await _make_archive(db_session, printer)
        self._presence(monkeypatch, [(2, True)])

        await record_event(db_session, printer_id=printer.id, archive_id=archive.id, layer_num=5, event=EVENT_PAUSE)
        await record_event(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=6,
            event=EVENT_RUNOUT,
            global_tray_id=254,
        )

        events = await load_events(db_session, printer.id, archive.id)
        assert [e.slot_occupied for e in events[-2:]] == [None, None]

    @pytest.mark.asyncio
    async def test_an_unreachable_printer_does_not_break_the_write(self, db_session, printer, monkeypatch):
        # The journal row matters more than the extra fact on it.
        def _boom(_pid):
            raise RuntimeError("no client")

        monkeypatch.setattr("backend.app.services.printer_manager.printer_manager.get_status", _boom)
        archive = await _make_archive(db_session, printer)

        await record_event(
            db_session,
            printer_id=printer.id,
            archive_id=archive.id,
            layer_num=1,
            event=EVENT_RUNOUT,
            global_tray_id=2,
        )

        events = await load_events(db_session, printer.id, archive.id)
        assert events[-1].slot_occupied is None
