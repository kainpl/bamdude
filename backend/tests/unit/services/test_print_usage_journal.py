"""The append-only usage journal: record/load, spool-id freezing, retention."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from backend.app.models.print_usage_event import (
    EVENT_RUNOUT,
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
