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
    async def test_journaled_print_ignores_live_reassignment(self, db_session, tmp_path, monkeypatch):
        """The journal owns mid-print changes: with events present, the
        assignment snapshot wins unconditionally — 'live wins' stays only for
        journal-less prints (its wrong-assignment-correction case)."""
        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        printer = await _make_printer(db_session)
        archive = await _make_archive(db_session, printer, tmp_path)
        spool_a = await _make_spool(db_session)
        spool_b = await _make_spool(db_session)
        from backend.app.models.spool_assignment import SpoolAssignment

        # Live assignment (created during the print) points at B…
        db_session.add(SpoolAssignment(printer_id=printer.id, ams_id=0, tray_id=0, spool_id=spool_b.id))
        await db_session.commit()
        # …but the snapshot froze A, and the print HAS journal events.
        await _journal(db_session, printer, archive, [(EVENT_START, None, 0, 0, spool_a.id)])
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
