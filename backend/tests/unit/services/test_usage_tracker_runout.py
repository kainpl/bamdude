"""Runout zero-point accounting: journal-driven segmentation + corrections.

The journal owns mid-print attribution (spool ids frozen at event time); the
completion path splits each slot at ITS tray's boundaries, charges frozen
spools, and only then closes unambiguous runouts out to exactly label_weight.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.core.config import settings as app_settings
from backend.app.models.print_usage_event import (
    EVENT_RUNOUT,
    EVENT_SPOOL_LOADED,
    EVENT_START,
    EVENT_TRAY_CHANGE,
    KIND_AMBIGUOUS,
    KIND_AUTOSWITCH,
    KIND_PAUSE,
    PrintUsageEvent,
)
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.usage_tracker import RUNOUT_STATUS, PrintSession, _active_sessions, on_print_complete


@pytest.fixture(autouse=True)
def _clean_memory():
    _active_sessions.clear()
    yield
    _active_sessions.clear()


async def _make_printer(db_session, name="RP1"):
    from backend.app.models.printer import Printer

    p = Printer(name=name, ip_address="10.0.0.7", serial_number=f"SN-{name}", access_code="1")
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


async def _make_archive(db_session, printer, tmp_path):
    from backend.app.models.archive import PrintArchive

    (tmp_path / "archives").mkdir(exist_ok=True)
    (tmp_path / "archives" / "test.3mf").write_bytes(b"stub")
    a = PrintArchive(
        printer_id=printer.id,
        filename="test.3mf",
        file_path="archives/test.3mf",
        file_size=4,
        print_name="test_print",
        status="printing",
        filament_used_grams=300.0,
    )
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)
    return a


async def _make_spool(db_session, *, label_weight=1000, weight_used=0.0):
    s = Spool(material="PLA", label_weight=label_weight, weight_used=weight_used, rgba="FF0000FF")
    db_session.add(s)
    await db_session.commit()
    await db_session.refresh(s)
    return s


async def _journal(db_session, printer, archive, rows):
    """rows: (event, kind, tray, layer, spool_id)"""
    for event, kind, tray, layer, spool_id in rows:
        db_session.add(
            PrintUsageEvent(
                printer_id=printer.id,
                archive_id=archive.id,
                layer_num=layer,
                event=event,
                kind=kind,
                global_tray_id=tray,
                spool_id=spool_id,
            )
        )
    await db_session.commit()


def _pm(total_layers=200, ams=None, progress=100):
    state = MagicMock()
    state.raw_data = {"ams": ams or []}
    state.progress = progress
    state.layer_num = 0
    state.total_layers = total_layers
    state.tray_now = 255
    state.last_loaded_tray = -1
    state.tray_change_log = []
    pm = MagicMock()
    pm.get_status.return_value = state
    return pm


def _session(printer_id, **kw):
    args = {
        "printer_id": printer_id,
        "print_name": "test_print",
        "started_at": datetime.now(timezone.utc),
        "tray_remain_start": {},
        "tray_now_at_start": -1,
        "spool_assignments": {},
        "ams_mapping": [0],
    }
    args.update(kw)
    return PrintSession(**args)


def _patched_3mf(usage, layer_usage=None):
    return (
        patch("backend.app.utils.threemf_tools.extract_filament_usage_from_3mf", return_value=usage),
        patch("backend.app.utils.threemf_tools.extract_layer_filament_usage_from_3mf", return_value=layer_usage),
    )


async def _history(db_session):
    rows = (await db_session.execute(select(SpoolUsageHistory).order_by(SpoolUsageHistory.id))).scalars().all()
    return [(r.spool_id, r.weight_used, r.status) for r in rows]


class TestRunoutZeroPoint:
    @pytest.mark.asyncio
    async def test_same_slot_refill_splits_at_the_runout_layer(self, db_session, tmp_path, monkeypatch):
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await _make_printer(db_session)
        archive = await _make_archive(db_session, printer, tmp_path)
        spool_a = await _make_spool(db_session, weight_used=700)
        spool_b = await _make_spool(db_session, weight_used=0)
        await _journal(
            db_session,
            printer,
            archive,
            [
                (EVENT_START, None, 0, 0, spool_a.id),
                (EVENT_RUNOUT, KIND_PAUSE, 0, 140, spool_a.id),
                (EVENT_SPOOL_LOADED, None, 0, 140, spool_b.id),
            ],
        )
        _active_sessions[printer.id] = _session(printer.id)

        p1, p2 = _patched_3mf([{"slot_id": 1, "used_g": 300.0, "type": "PLA", "color": "#FF0000"}])
        with p1, p2:
            results = await on_print_complete(
                printer.id, {"status": "completed"}, _pm(total_layers=200), db_session, archive_id=archive.id
            )

        await db_session.refresh(spool_a)
        await db_session.refresh(spool_b)
        # Linear split: A 140/200 × 300 = 210 g, B the remaining 90 g.
        assert spool_b.weight_used == pytest.approx(90.0)
        # A: 700 + 210 = 910, then the zero correction tops it to label 1000.
        assert spool_a.weight_used == pytest.approx(1000.0)
        history = await _history(db_session)
        assert (spool_a.id, 210.0, "completed") in history
        assert (spool_b.id, 90.0, "completed") in history
        assert (spool_a.id, 90.0, RUNOUT_STATUS) in history
        # The correction is broadcast but carries no slot for the colour rewrite.
        assert any(r.get("status") == RUNOUT_STATUS for r in results)

    @pytest.mark.asyncio
    async def test_an_open_episode_never_closes_the_books(self, db_session, tmp_path, monkeypatch):
        """Runout WITHOUT a spool_loaded: the same reel was reinserted (AMS
        without backup, or a cut-filament simulation) and kept printing — it
        is demonstrably not empty. Closing it at label invented 606 g on a
        half-full spool (X2D archive 734, measured live 2026-08-24)."""
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await _make_printer(db_session)
        archive = await _make_archive(db_session, printer, tmp_path)
        spool_a = await _make_spool(db_session, weight_used=200)
        await _journal(
            db_session,
            printer,
            archive,
            [
                (EVENT_START, None, 0, 0, spool_a.id),
                (EVENT_RUNOUT, KIND_PAUSE, 0, 140, spool_a.id),
            ],
        )
        _active_sessions[printer.id] = _session(printer.id)

        p1, p2 = _patched_3mf([{"slot_id": 1, "used_g": 300.0, "type": "PLA", "color": "#FF0000"}])
        with p1, p2:
            results = await on_print_complete(
                printer.id, {"status": "completed"}, _pm(total_layers=200), db_session, archive_id=archive.id
            )

        await db_session.refresh(spool_a)
        # the full print is charged, and nothing is topped up to the label
        assert spool_a.weight_used == pytest.approx(500.0)
        history = await _history(db_session)
        assert not any(status == RUNOUT_STATUS for (_sid, _g, status) in history)
        assert not any(r.get("status") == RUNOUT_STATUS for r in results)

    @pytest.mark.asyncio
    async def test_overdrafted_books_clamp_silently(self, db_session, tmp_path, monkeypatch):
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await _make_printer(db_session)
        archive = await _make_archive(db_session, printer, tmp_path)
        spool_a = await _make_spool(db_session, weight_used=950)
        spool_a.low_stock_notified = True
        await db_session.commit()
        await _journal(
            db_session,
            printer,
            archive,
            [(EVENT_RUNOUT, KIND_PAUSE, 0, 140, spool_a.id)],
        )
        _active_sessions[printer.id] = _session(printer.id)

        p1, p2 = _patched_3mf([{"slot_id": 1, "used_g": 300.0, "type": "PLA", "color": ""}])
        with p1, p2:
            await on_print_complete(
                printer.id, {"status": "completed"}, _pm(total_layers=200), db_session, archive_id=archive.id
            )

        await db_session.refresh(spool_a)
        # 950 + 300 = 1250 books over label → silent clamp to exactly 1000.
        assert spool_a.weight_used == pytest.approx(1000.0)
        assert spool_a.low_stock_notified is False  # re-armed
        assert [(s, w, st) for s, w, st in await _history(db_session) if st == RUNOUT_STATUS] == []

    @pytest.mark.asyncio
    async def test_ambiguous_runout_never_corrects(self, db_session, tmp_path, monkeypatch):
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await _make_printer(db_session)
        archive = await _make_archive(db_session, printer, tmp_path)
        spool_a = await _make_spool(db_session, weight_used=100)
        await _journal(
            db_session,
            printer,
            archive,
            [(EVENT_RUNOUT, KIND_AMBIGUOUS, 0, 140, spool_a.id)],
        )
        # An ambiguous event is not a boundary — the slot resolves through the
        # ordinary assignment snapshot, as any journal-less print would.
        _active_sessions[printer.id] = _session(printer.id, spool_assignments={(0, 0): spool_a.id})

        p1, p2 = _patched_3mf([{"slot_id": 1, "used_g": 300.0, "type": "PLA", "color": ""}])
        with p1, p2:
            await on_print_complete(
                printer.id, {"status": "completed"}, _pm(total_layers=200), db_session, archive_id=archive.id
            )

        await db_session.refresh(spool_a)
        assert spool_a.weight_used == pytest.approx(400.0)  # print grams only, no zero-out
        assert [(s, w, st) for s, w, st in await _history(db_session) if st == RUNOUT_STATUS] == []

    @pytest.mark.asyncio
    async def test_manual_replacement_never_corrects(self, db_session, tmp_path, monkeypatch):
        # A preventively swapped reel is not empty — the declared mid-pause
        # replacement shares the ambiguous contract on the zero-out side.
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await _make_printer(db_session)
        archive = await _make_archive(db_session, printer, tmp_path)
        spool_a = await _make_spool(db_session, weight_used=100)
        await _journal(
            db_session,
            printer,
            archive,
            [(EVENT_RUNOUT, "manual", 0, 140, spool_a.id)],
        )
        _active_sessions[printer.id] = _session(printer.id, spool_assignments={(0, 0): spool_a.id})

        p1, p2 = _patched_3mf([{"slot_id": 1, "used_g": 300.0, "type": "PLA", "color": ""}])
        with p1, p2:
            await on_print_complete(
                printer.id, {"status": "completed"}, _pm(total_layers=200), db_session, archive_id=archive.id
            )

        await db_session.refresh(spool_a)
        assert spool_a.weight_used == pytest.approx(400.0)  # print grams only, no zero-out
        assert [(s, w, st) for s, w, st in await _history(db_session) if st == RUNOUT_STATUS] == []

    @pytest.mark.asyncio
    async def test_multicolor_runout_splits_only_the_runout_slot(self, db_session, tmp_path, monkeypatch):
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await _make_printer(db_session)
        archive = await _make_archive(db_session, printer, tmp_path)
        spool_a = await _make_spool(db_session, weight_used=800)  # runout slot origin
        spool_b = await _make_spool(db_session)  # second colour, untouched by the runout
        spool_c = await _make_spool(db_session)  # backup tray spool
        await _journal(
            db_session,
            printer,
            archive,
            [
                (EVENT_RUNOUT, KIND_AUTOSWITCH, 0, 100, spool_a.id),
                (EVENT_TRAY_CHANGE, None, 2, 100, spool_c.id),
            ],
        )
        _active_sessions[printer.id] = _session(printer.id, ams_mapping=[0, 1], spool_assignments={(0, 1): spool_b.id})

        p1, p2 = _patched_3mf(
            [
                {"slot_id": 1, "used_g": 200.0, "type": "PLA", "color": ""},
                {"slot_id": 2, "used_g": 100.0, "type": "PLA", "color": ""},
            ]
        )
        with p1, p2:
            await on_print_complete(
                printer.id, {"status": "completed"}, _pm(total_layers=200), db_session, archive_id=archive.id
            )

        await db_session.refresh(spool_a)
        await db_session.refresh(spool_b)
        await db_session.refresh(spool_c)
        # Slot 1 (200 g over 200 layers): origin A gets 100/200 → 100 g, then
        # zero-corrected 800+100 → 1000; backup C gets the other 100 g.
        assert spool_a.weight_used == pytest.approx(1000.0)
        assert spool_c.weight_used == pytest.approx(100.0)
        # Slot 2 charged whole to its own spool, untouched by the split.
        assert spool_b.weight_used == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_path2_skips_every_journal_touched_tray(self, db_session, tmp_path, monkeypatch):
        """Audit finding #1: the substitute tray's remain-delta must not be
        charged on top of the 3MF estimate — journal trays are handled."""
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await _make_printer(db_session)
        archive = await _make_archive(db_session, printer, tmp_path)
        spool_c = await _make_spool(db_session)  # substitute on tray 2, remain dropped
        from backend.app.models.spool_assignment import SpoolAssignment

        db_session.add(SpoolAssignment(printer_id=printer.id, ams_id=0, tray_id=2, spool_id=spool_c.id))
        await db_session.commit()
        await _journal(db_session, printer, archive, [(EVENT_TRAY_CHANGE, None, 2, 100, spool_c.id)])
        _active_sessions[printer.id] = _session(printer.id, tray_remain_start={(0, 2): 80})

        ams = [{"id": 0, "tray": [{"id": 2, "remain": 70}]}]
        p1, p2 = _patched_3mf([])
        with p1, p2:
            await on_print_complete(
                printer.id, {"status": "completed"}, _pm(ams=ams), db_session, archive_id=archive.id
            )

        await db_session.refresh(spool_c)
        assert spool_c.weight_used == pytest.approx(0.0)  # delta NOT charged

    @pytest.mark.asyncio
    async def test_zero_point_setting_off_disables_corrections(self, db_session, tmp_path, monkeypatch):
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        db_session.add(Settings(key="runout_zero_point_enabled", value="false"))
        await db_session.commit()
        printer = await _make_printer(db_session)
        archive = await _make_archive(db_session, printer, tmp_path)
        spool_a = await _make_spool(db_session, weight_used=700)
        await _journal(db_session, printer, archive, [(EVENT_RUNOUT, KIND_PAUSE, 0, 140, spool_a.id)])
        _active_sessions[printer.id] = _session(printer.id)

        p1, p2 = _patched_3mf([{"slot_id": 1, "used_g": 100.0, "type": "PLA", "color": ""}])
        with p1, p2:
            await on_print_complete(
                printer.id, {"status": "completed"}, _pm(total_layers=200), db_session, archive_id=archive.id
            )

        await db_session.refresh(spool_a)
        assert spool_a.weight_used == pytest.approx(800.0)  # print grams only
        assert [(s, w, st) for s, w, st in await _history(db_session) if st == RUNOUT_STATUS] == []

    @pytest.mark.asyncio
    async def test_boundary_events_suspend_live_reassignment(self, db_session, tmp_path, monkeypatch):
        """The journal owns mid-print changes ONLY via boundary events
        (runout / spool_loaded) — a start or pause row must not suspend the
        legitimate wrong-link correction ('live wins')."""
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await _make_printer(db_session)
        archive = await _make_archive(db_session, printer, tmp_path)
        spool_a = await _make_spool(db_session)
        spool_b = await _make_spool(db_session)
        from backend.app.models.spool_assignment import SpoolAssignment

        # Live assignment (created during the print) points at B…
        db_session.add(SpoolAssignment(printer_id=printer.id, ams_id=0, tray_id=0, spool_id=spool_b.id))
        await db_session.commit()
        # …but the snapshot froze A, and the print HAS a boundary event.
        await _journal(
            db_session,
            printer,
            archive,
            [
                (EVENT_START, None, 0, 0, spool_a.id),
                (EVENT_RUNOUT, KIND_AMBIGUOUS, 3, 10, None),  # boundary elsewhere still suspends live-wins
                (EVENT_SPOOL_LOADED, None, 3, 10, None),
            ],
        )
        _active_sessions[printer.id] = _session(printer.id, spool_assignments={(0, 0): spool_a.id})

        p1, p2 = _patched_3mf([{"slot_id": 1, "used_g": 50.0, "type": "PLA", "color": ""}])
        with p1, p2:
            await on_print_complete(printer.id, {"status": "completed"}, _pm(), db_session, archive_id=archive.id)

        await db_session.refresh(spool_a)
        await db_session.refresh(spool_b)
        assert spool_a.weight_used == pytest.approx(50.0)
        assert spool_b.weight_used == pytest.approx(0.0)


class TestAutoswitchPurgeGrams:
    @pytest.mark.asyncio
    async def test_purge_lands_on_the_backup_segment(self, db_session, tmp_path, monkeypatch):
        """runout_purge_grams=25 + autoswitch → the backup spool's journal
        segment carries +25 g; the origin's segment is untouched."""
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        db_session.add(Settings(key="runout_purge_grams", value="25"))
        await db_session.commit()
        printer = await _make_printer(db_session)
        archive = await _make_archive(db_session, printer, tmp_path)
        spool_a = await _make_spool(db_session, weight_used=900)
        spool_c = await _make_spool(db_session)
        await _journal(
            db_session,
            printer,
            archive,
            [
                (EVENT_RUNOUT, KIND_AUTOSWITCH, 0, 100, spool_a.id),
                (EVENT_TRAY_CHANGE, None, 2, 100, spool_c.id),
            ],
        )
        _active_sessions[printer.id] = _session(printer.id)

        p1, p2 = _patched_3mf([{"slot_id": 1, "used_g": 200.0, "type": "PLA", "color": ""}])
        with p1, p2:
            await on_print_complete(
                printer.id, {"status": "completed"}, _pm(total_layers=200), db_session, archive_id=archive.id
            )

        await db_session.refresh(spool_a)
        await db_session.refresh(spool_c)
        # Origin: 900 + 100 (its segment) → zero-corrected to 1000.
        assert spool_a.weight_used == pytest.approx(1000.0)
        # Backup: 100 (segment) + 25 (purge) = 125, inside the segment row.
        assert spool_c.weight_used == pytest.approx(125.0)
        assert (spool_c.id, 125.0, "completed") in await _history(db_session)


class TestPath2UuidGate:
    @pytest.mark.asyncio
    async def test_remain_delta_skips_a_swapped_spool(self, db_session, tmp_path, monkeypatch):
        """Parity with the Spoolman remain-delta: a tray whose RFID uuid
        changed mid-print is a physical spool swap — attributing the delta to
        the snapshot's spool would bill the wrong reel."""
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await _make_printer(db_session)
        archive = await _make_archive(db_session, printer, tmp_path)
        spool = await _make_spool(db_session)
        from backend.app.models.spool_assignment import SpoolAssignment

        db_session.add(SpoolAssignment(printer_id=printer.id, ams_id=0, tray_id=0, spool_id=spool.id))
        await db_session.commit()

        session = _session(printer.id, tray_remain_start={(0, 0): 80})
        session.tray_uuid_start = {(0, 0): "AAAA0000AAAA0000AAAA0000AAAA0000"}
        _active_sessions[printer.id] = session

        ams = [{"id": 0, "tray": [{"id": 0, "remain": 70, "tray_uuid": "BBBB0000BBBB0000BBBB0000BBBB0000"}]}]
        p1, p2 = _patched_3mf([])
        with p1, p2:
            await on_print_complete(
                printer.id, {"status": "completed"}, _pm(ams=ams), db_session, archive_id=archive.id
            )

        await db_session.refresh(spool)
        assert spool.weight_used == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_uuid_survives_the_session_round_trip(self, db_session):
        from backend.app.services.usage_tracker import persist_session, restore_session

        printer = await _make_printer(db_session, name="RT1")
        session = _session(printer.id, tray_remain_start={(0, 1): 55})
        session.tray_uuid_start = {(0, 1): "CAFE0000CAFE0000CAFE0000CAFE0000"}
        await persist_session(db_session, session)

        _active_sessions.clear()
        await restore_session(db_session, printer.id)
        restored = _active_sessions[printer.id]
        assert restored.tray_remain_start == {(0, 1): 55}
        assert restored.tray_uuid_start == {(0, 1): "CAFE0000CAFE0000CAFE0000CAFE0000"}

    @pytest.mark.asyncio
    async def test_start_and_pause_rows_keep_live_wins(self, db_session, tmp_path, monkeypatch):
        """A jam-time (or any mid-print) re-assignment on a print with no
        boundary events keeps the old semantics: the corrected live link is
        charged, not the stale snapshot."""
        from backend.app.models.print_usage_event import EVENT_PAUSE

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await _make_printer(db_session)
        archive = await _make_archive(db_session, printer, tmp_path)
        spool_a = await _make_spool(db_session)
        spool_b = await _make_spool(db_session)
        from backend.app.models.spool_assignment import SpoolAssignment

        db_session.add(SpoolAssignment(printer_id=printer.id, ams_id=0, tray_id=0, spool_id=spool_b.id))
        await db_session.commit()
        await _journal(
            db_session,
            printer,
            archive,
            [(EVENT_START, None, 0, 0, spool_a.id), (EVENT_PAUSE, None, 0, 50, spool_a.id)],
        )
        # The print started an hour ago; the live re-assignment (created just
        # now, above) therefore postdates it — the correction case.
        from datetime import timedelta

        _active_sessions[printer.id] = _session(
            printer.id,
            spool_assignments={(0, 0): spool_a.id},
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        p1, p2 = _patched_3mf([{"slot_id": 1, "used_g": 50.0, "type": "PLA", "color": ""}])
        with p1, p2:
            await on_print_complete(printer.id, {"status": "completed"}, _pm(), db_session, archive_id=archive.id)

        await db_session.refresh(spool_a)
        await db_session.refresh(spool_b)
        assert spool_b.weight_used == pytest.approx(50.0)  # live correction won
        assert spool_a.weight_used == pytest.approx(0.0)


class TestMultiEpisodeBoundaries:
    def test_two_reels_back_to_back_give_three_segments(self):
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        events = [
            ev(1, "runout", "external", 254, 80, 7),
            ev(2, "spool_loaded", None, 254, 80, 9),
            ev(3, "runout", "external", 254, 250, 9),
            ev(4, "spool_loaded", None, 254, 250, 11),
        ]
        assert journal_boundaries_for_tray(events, 254) == [(0, 7, None), (80, 9, None), (250, 11, None)]

    def test_a_jam_splits_only_when_a_replacement_was_demonstrably_loaded(self):
        # Printer 3, 2026-08-25: the holder jam (12FF8000 — a reel's taped
        # tail) journals an AMBIGUOUS runout. With the mid-pause replacement
        # assigned it is a boundary; untangle-and-resume splits nothing.
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        replaced = [
            ev(1, "runout", "ambiguous", 254, 12, 273),
            ev(2, "spool_loaded", None, 254, 12, 278),
        ]
        assert journal_boundaries_for_tray(replaced, 254) == [(0, 273, None), (12, 278, None)]

    def test_a_runout_onto_the_same_spool_is_not_a_boundary(self):
        # Printer 5, archive 822 (2026-08-31): reel swapped at layer 227, then a
        # JAM at 405 that the firmware escalated from `ambiguous` to a definite
        # `external` runout — so the ambiguous guard above no longer applied and
        # the "resumed without a replacement" branch opened a segment feeding
        # the SAME spool 267. One continuous stretch became two history rows,
        # which reads as a double charge. A boundary that does not change the
        # spool is not a boundary.
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        events = [
            ev(1, "runout", "manual", 254, 227, 279),
            ev(2, "spool_loaded", None, 254, 227, 267),
            ev(3, "runout", "external", 254, 405, 267),  # the jam; nothing reloaded
        ]
        assert journal_boundaries_for_tray(events, 254) == [(0, 279, None), (227, 267, None)]

    def test_two_reels_of_the_same_spool_id_still_need_different_spools_to_split(self):
        # Guard the merge against over-reach: consecutive segments whose spools
        # DIFFER must survive untouched (this is the back-to-back reel shape).
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        events = [
            ev(1, "runout", "external", 254, 80, 7),
            ev(2, "spool_loaded", None, 254, 80, 9),
            ev(3, "runout", "external", 254, 250, 9),
            ev(4, "spool_loaded", None, 254, 250, 11),
        ]
        assert journal_boundaries_for_tray(events, 254) == [(0, 7, None), (80, 9, None), (250, 11, None)]

    def test_the_backup_that_took_over_before_the_first_layer_is_named_by_the_start(self):
        # Printer 1, archives 810-837 (2026-08-30/31): the plates are sliced for
        # AMS slot 2, that slot is empty, and the AMS backs up from slot 3
        # BEFORE layer 1 — so ``tray_now`` is already 3 when the print starts and
        # no ``tray_change`` is ever emitted. The autoswitch branch could only
        # name a backup through that event, so six prints in a row charged
        # nobody (939 g). The journal already holds the answer in its own start
        # event: the print began feeding from tray 3, spool 291.
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        events = [
            ev(1, "start", None, 3, 0, 291),
            ev(2, "runout", "autoswitch", 2, 1, None),
        ]
        # One segment, the whole print on the spool that actually fed it.
        assert journal_boundaries_for_tray(events, 2) == [(0, 291, None)]

    def test_a_printer_that_unloads_between_prints_keeps_using_its_tray_change(self):
        # Printer 10 (X2D), archives 813-838: the SAME empty-mapped-slot backup
        # as printer 1, but this machine unloads after a print — so ``tray_now``
        # is nothing at start (the journal's start carries no tray at all) and
        # the AMS picking slot 2 genuinely CHANGES it, emitting the tray_change
        # that names the backup. That evidence is stronger than the start event
        # and must keep winning; these prints were charging correctly all along
        # (156.4 g to spool 292 each) and must not move.
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        events = [
            ev(1, "start", None, None, 0, None),
            ev(2, "runout", "autoswitch", 3, 0, None),
            ev(3, "tray_change", None, 2, 0, 292),
        ]
        assert journal_boundaries_for_tray(events, 3) == [(0, None, None), (0, 292, None)]

    def test_a_start_on_the_very_tray_that_ran_out_teaches_nothing(self):
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        # Same tray: the start says nothing the runout didn't. Unchanged —
        # charged to nothing rather than guessed.
        events = [
            ev(1, "start", None, 2, 0, 290),
            ev(2, "runout", "autoswitch", 2, 40, 290),
        ]
        assert journal_boundaries_for_tray(events, 2) == [(0, 290, None), (40, None, None)]

    def test_a_tray_that_had_its_own_spool_is_not_overridden_by_the_start(self):
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        # The ran-out tray DID hold a spool — it really fed part of the print,
        # so the start event must not swallow its share.
        events = [
            ev(1, "start", None, 3, 0, 291),
            ev(2, "runout", "autoswitch", 2, 40, 290),
        ]
        assert journal_boundaries_for_tray(events, 2) == [(0, 290, None), (40, None, None)]

    def test_a_mid_print_backup_still_prefers_its_tray_change(self):
        # Archive 804 (2026-08-29): slot 2 ran out at layer 33 and the AMS
        # switched — the tray_change names the backup, and that evidence must
        # keep winning over the start event.
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        events = [
            ev(1, "start", None, 2, 0, 290),
            ev(2, "runout", "autoswitch", 2, 33, 290),
            ev(3, "tray_change", None, 3, 33, 291),
        ]
        assert journal_boundaries_for_tray(events, 2) == [(0, 290, None), (33, 291, None)]

    def test_a_late_tray_change_does_not_disarm_the_start_rescue(self):
        # The guard that turns the start-rescue off must ask the same question
        # the backup lookup asks: "did this tray's share move elsewhere AT the
        # runout?" It used to scan the whole print for any tray change at all —
        # so an ordinary switch 60 layers later disarmed the rescue and put
        # nothing in its place (the backup lookup only reaches layer+1), and the
        # print was charged to nobody. The start still names the feeder.
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        events = [
            ev(1, "start", None, 3, 0, 291),
            ev(2, "runout", "autoswitch", 2, 1, None),
            ev(3, "tray_change", None, 1, 60, 257),
        ]
        assert journal_boundaries_for_tray(events, 2) == [(0, 291, None)]

    def test_a_reel_loaded_into_the_empty_slot_never_takes_the_backups_share(self):
        # Printer 1, archive 837 (2026-09-01): the mapped slot 2 was empty, the
        # AMS backed up from slot 3 before layer 1, and mid-print the operator
        # put a fresh reel into slot 2 — which closes slot 2's open episode and
        # journals spool_loaded, correctly (if the AMS ever switches back, that
        # row names the reel). It must not thereby inherit what slot 3 fed:
        # with the guard too broad, a late tray change handed the whole share
        # to a reel that had never printed a gram.
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        events = [
            ev(1, "start", None, 3, 0, 291),
            ev(2, "runout", "autoswitch", 2, 1, None),
            ev(3, "spool_loaded", None, 2, 25, 294),
            ev(4, "tray_change", None, 1, 60, 257),
        ]
        assert journal_boundaries_for_tray(events, 2) == [(0, 291, None)]

    def test_the_backup_at_the_runout_layer_outranks_a_reel_loaded_later(self):
        # Printer 10, archive 838, caught live (2026-09-01). The mapped slot 3
        # was empty at layer 0, the AMS backed up to slot 2 (spool 292) in the
        # same breath, and 28 layers later the operator put a fresh reel into
        # slot 3 — which closes slot 3's open episode with a spool_loaded row.
        # That row used to win over the backup unconditionally, so the segment
        # starting at layer 0 was charged to a reel that had not existed in the
        # machine for the first 28 layers, while the five sibling prints of the
        # same plate had each charged 156.4 g to spool 292.
        #
        # A tray_change is bounded to the runout's own layer; a spool_loaded can
        # be anywhere later. The tighter evidence names the feeder.
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        events = [
            ev(1, "start", None, None, 0, None),
            ev(2, "runout", "autoswitch", 3, 0, None),
            ev(3, "tray_change", None, 2, 0, 292),
            ev(4, "spool_loaded", None, 3, 28, 293),
        ]
        assert journal_boundaries_for_tray(events, 3) == [(0, None, None), (0, 292, None)]

    def test_without_a_backup_the_loaded_reel_still_names_the_feeder(self):
        # The other real autoswitch shape: nothing backed the tray up (no
        # tray_change at all), the human refilled the very tray that ran out and
        # resumed. There is no tighter evidence to prefer, so the loaded reel
        # must keep winning — the fallback this reordering must not eat.
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        events = [
            ev(1, "runout", "autoswitch", 2, 40, 290),
            ev(2, "spool_loaded", None, 2, 45, 294),
        ]
        assert journal_boundaries_for_tray(events, 2) == [(0, 290, None), (40, 294, None)]

    def test_a_refilled_tray_that_runs_out_again_is_still_a_boundary(self):
        # The cover must be reset by a refill: once the human puts a reel into
        # the tray and it feeds again, the tray is no longer being covered by
        # anyone — so its NEXT runout is a real boundary, and an unnameable
        # feeder there is still charged to nobody rather than quietly folded
        # into the previous segment.
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        events = [
            ev(1, "runout", "autoswitch", 2, 40, 290),
            ev(2, "spool_loaded", None, 2, 45, 294),
            ev(3, "runout", "autoswitch", 2, 80, 294),
        ]
        assert journal_boundaries_for_tray(events, 2) == [(0, 290, None), (40, 294, None), (80, None, None)]

    def test_without_a_start_event_nothing_is_invented(self):
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        events = [ev(1, "runout", "autoswitch", 2, 1, None)]
        assert journal_boundaries_for_tray(events, 2) == [(0, None, None), (1, None, None)]

    def test_a_lone_same_spool_runout_keeps_its_pair(self):
        # ⚠️ The merge must never collapse below two segments. With no swap at
        # all, this pair is the ONLY thing naming the spool — a single-segment
        # result reads as "no split" and the print is charged to nobody (the
        # shape TestRunoutZeroPoint::test_an_open_episode_never_closes_the_books
        # pins end to end). On a cancelled print the tail comes out at 0 g and
        # the caller skips the row anyway.
        from types import SimpleNamespace

        from backend.app.services.usage_tracker import journal_boundaries_for_tray

        def ev(eid, event, kind, tray, layer, spool):
            return SimpleNamespace(
                id=eid,
                event=event,
                kind=kind,
                global_tray_id=tray,
                layer_num=layer,
                spool_id=spool,
                spoolman_spool_id=None,
            )

        assert journal_boundaries_for_tray([ev(1, "runout", "external", 254, 405, 267)], 254) == [
            (0, 267, None),
            (405, 267, None),
        ]

        untangled = [ev(1, "runout", "ambiguous", 254, 12, 273)]
        assert journal_boundaries_for_tray(untangled, 254) == []
