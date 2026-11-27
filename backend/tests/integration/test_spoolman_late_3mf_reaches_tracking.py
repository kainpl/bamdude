"""Spoolman: a 3MF attached after print start reaches the tracking row (upstream 005b34af6).

An external print whose start download failed gets its Spoolman tracking row with no 3MF figures -- or no
row at all when no slot reported a remain%. The file arrives ten minutes later through the download retry,
and nothing told the row: the print was charged by the AMS remain% change alone.

An 87 g print, a 1 kg reel:

- RFID reel: 90 g (whole percents) instead of 87;
- a reel without RFID, in a slot with no remain%: 0 g, with no notice;
- no slot had a remain% at start: no row, 0 g.

The built-in tracker reads the archive at completion and gives 87.0 g in all three. Now the retry hands the
attached file to ``spoolman_tracking.attach_recovered_3mf``: an existing row with no 3MF figures gets them
(its start snapshot is never touched), and a missing row is made only while the print still runs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.active_print_session import ActivePrintSession
from backend.app.models.active_print_spoolman import ActivePrintSpoolman
from backend.app.models.archive import PrintArchive
from backend.app.models.settings import Settings
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.services import spoolman_tracking, usage_tracker
from backend.app.services.print_file_analysis import PrintFileAnalysis
from backend.app.utils.tray_split import CompletionSnapshot, terminal_snapshot_fields

# C047 part 2: the accounting tail runs inside an admitted charge (services/spoolman_charge).
pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.usefixtures("spoolman_charge_admitted")]

UUID = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
ANALYSIS = PrintFileAnalysis(
    filament_usage=[{"slot_id": 1, "used_g": 87.0, "type": "PLA", "color": "#FF0000"}],
    layer_usage={10: {0: 1200.0}},
    filament_properties={1: {"density": 1.24, "diameter": 1.75, "type": "PLA"}},
)
COMPLETION = CompletionSnapshot.from_event(
    {
        "status": "completed",
        "last_layer_num": 200,
        "last_progress": 100.0,
        **terminal_snapshot_fields(layer_num=200, total_layers=200, progress=100.0),
    }
)
SPOOL = {"id": 42, "initial_weight": 1000, "remaining_weight": 1000.0, "filament": {"weight": 1000}}


@pytest.fixture(autouse=True)
def _clean_memory():
    usage_tracker._completing.clear()
    yield
    usage_tracker._completing.clear()


@pytest.fixture
def sessions(monkeypatch, test_engine):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.services.spoolman_tracking.async_session", factory)
    return factory


def _tray(tray_id: int, *, remain: int, uuid: str = "") -> dict:
    return {"id": tray_id, "tray_type": "PLA", "tray_color": "FF0000FF", "remain": remain, "tray_uuid": uuid}


def _printer(trays: list[dict]):
    state = SimpleNamespace(
        state="RUNNING", raw_data={"ams": [{"id": 0, "tray": trays}]}, tray_change_log=[(0, 0)], tray_now=0
    )
    return SimpleNamespace(get_status=lambda _pid: state)


@pytest.fixture
async def stage(db_session, printer_factory):
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    printer = await printer_factory()
    archive = PrintArchive(
        printer_id=printer.id,
        filename="bracket.gcode.3mf",
        print_name="bracket",
        file_path="",  # the start download failed
        file_size=0,
        status="printing",
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(archive)
    db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=0, spoolman_spool_id=42))
    await db_session.commit()
    await db_session.refresh(archive)
    return SimpleNamespace(printer=printer, archive=archive)


async def _start_without_the_file(db, stage, trays):
    """What print start stores when the 3MF could not be fetched."""
    await spoolman_tracking.store_print_data(
        stage.printer.id, stage.archive.id, "", db, _printer(trays), ams_mapping=[0]
    )


async def _session_row(db, stage, *, identity=None):
    db.add(
        ActivePrintSession(
            printer_id=stage.printer.id,
            print_name="bracket",
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
            tray_now_at_start=0,
            ams_mapping=[0],
            archive_identity=identity or stage.archive.recovery_identity,
            owner_tracked=True,
        )
    )
    await db.commit()


async def _late_attach(db, stage, trays, *, analysis=ANALYSIS):
    """The download retry got the file: the tracking is told."""
    with patch("backend.app.services.print_file_analysis.get_print_file_analysis", AsyncMock(return_value=analysis)):
        return await spoolman_tracking.attach_recovered_3mf(
            stage.printer.id,
            stage.archive.id,
            "x/bracket.gcode.3mf",
            db,
            _printer(trays),
            plate_id=None,
            archive_identity=stage.archive.recovery_identity,
        )


async def _row(db, stage) -> ActivePrintSpoolman | None:
    result = await db.execute(
        select(ActivePrintSpoolman)
        .where(ActivePrintSpoolman.archive_id == stage.archive.id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _charged(stage, trays_at_the_end) -> dict[int, float]:
    """The real completion report: what Spoolman is told each spool used."""
    client = AsyncMock()
    client.find_spool_by_tag = AsyncMock(return_value=None)
    client.get_spool = AsyncMock(return_value=SPOOL)
    client.use_spool = AsyncMock()
    with (
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback", AsyncMock(return_value=client)
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._resolve_spool_tag", lambda *_a: ""),
        patch("backend.app.services.spoolman_tracking._apply_spool_colors_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._apply_spool_types_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch("backend.app.services.printer_manager.printer_manager", _printer(trays_at_the_end)),
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", AsyncMock()),
    ):
        await spoolman_tracking.report_usage(stage.printer.id, stage.archive.id, completion=COMPLETION)
    return {call.args[0]: call.args[1] for call in client.use_spool.await_args_list}


# ── the three figures ───────────────────────────────────────────────────────


class TestThePrintIsChargedFromItsFile:
    async def test_an_rfid_reel(self, db_session, stage, sessions):
        """Was {42: 90.0}: 100 % -> 91 % of a kilogram."""
        await _start_without_the_file(db_session, stage, [_tray(0, remain=100, uuid=UUID)])

        assert await _late_attach(db_session, stage, [_tray(0, remain=95, uuid=UUID)]) == "filled"

        assert await _charged(stage, [_tray(0, remain=91, uuid=UUID)]) == {42: 87.0}

    async def test_a_reel_without_rfid(self, db_session, stage, sessions):
        """Was {}: the row existed for the neighbour's remain%, and this slot had none to compare."""
        start = [_tray(0, remain=-1), _tray(1, remain=40, uuid=UUID)]
        await _start_without_the_file(db_session, stage, start)

        assert await _late_attach(db_session, stage, start) == "filled"

        assert await _charged(stage, start) == {42: 87.0}

    async def test_no_slot_had_a_remain_at_start(self, db_session, stage, sessions):
        """Was {}: print start stored no row at all."""
        trays = [_tray(0, remain=-1)]
        await _start_without_the_file(db_session, stage, trays)
        assert await _row(db_session, stage) is None
        await _session_row(db_session, stage)  # the print's start session: it is still running

        assert await _late_attach(db_session, stage, trays) == "created"

        assert await _charged(stage, trays) == {42: 87.0}


# ── what the fill may touch ─────────────────────────────────────────────────


class TestTheStartSnapshotIsNeverTouched:
    async def test_only_the_three_file_fields_are_written(self, db_session, stage, sessions):
        await _start_without_the_file(db_session, stage, [_tray(0, remain=100, uuid=UUID)])
        before = await _row(db_session, stage)
        start = (dict(before.ams_trays), list(before.slot_to_tray), dict(before.tray_remain_start))

        # Mid-print the slot reads 95 % and a different reel id: none of that is the start's.
        await _late_attach(db_session, stage, [_tray(0, remain=95, uuid="F" * 32)])

        row = await _row(db_session, stage)
        assert (dict(row.ams_trays), list(row.slot_to_tray), dict(row.tray_remain_start)) == start
        assert row.filament_usage == ANALYSIS.filament_usage
        assert row.layer_usage == {"10": {"0": 1200.0}}
        assert row.filament_properties == {"1": {"density": 1.24, "diameter": 1.75, "type": "PLA"}}

    async def test_a_row_that_has_its_file_figures_is_not_rewritten(self, db_session, stage, sessions):
        """GUARD 1: a print whose 3MF arrived at start is charged exactly what it was."""
        db_session.add(
            ActivePrintSpoolman(
                printer_id=stage.printer.id,
                archive_id=stage.archive.id,
                filament_usage=[{"slot_id": 1, "used_g": 50.0}],
                ams_trays={"0": {"tray_type": "PLA"}},
                slot_to_tray=[0],
            )
        )
        await db_session.commit()

        assert await _late_attach(db_session, stage, [_tray(0, remain=-1)]) == "unchanged"

        assert (await _row(db_session, stage)).filament_usage == [{"slot_id": 1, "used_g": 50.0}]
        assert await _charged(stage, [_tray(0, remain=-1)]) == {42: 50.0}

    async def test_a_file_with_no_figures_changes_nothing(self, db_session, stage, sessions):
        await _start_without_the_file(db_session, stage, [_tray(0, remain=100, uuid=UUID)])
        empty = PrintFileAnalysis(filament_usage=[], layer_usage=None, filament_properties={})

        assert await _late_attach(db_session, stage, [_tray(0, remain=95, uuid=UUID)], analysis=empty) == "unchanged"

        assert (await _row(db_session, stage)).filament_usage == []

    async def test_the_builtin_inventory_is_not_concerned(self, db_session, stage, sessions):
        (
            await db_session.execute(select(Settings).where(Settings.key == "spoolman_enabled"))
        ).scalar_one().value = "false"
        await db_session.commit()
        await _session_row(db_session, stage)

        assert await _late_attach(db_session, stage, [_tray(0, remain=-1)]) == "unchanged"
        assert await _row(db_session, stage) is None


# ── a row is made only while the print runs ─────────────────────────────────


class TestAMissingRowIsMadeOnlyMidPrint:
    async def test_the_last_chance_attach_still_fills_a_row_that_exists(self, db_session, stage, sessions):
        """The completion's own download: the row is there until the report takes it, so it is filled."""
        await _start_without_the_file(db_session, stage, [_tray(0, remain=100, uuid=UUID)])

        with usage_tracker.completion_in_progress(stage.printer.id):
            assert await _late_attach(db_session, stage, [_tray(0, remain=91, uuid=UUID)]) == "filled"

        assert await _charged(stage, [_tray(0, remain=91, uuid=UUID)]) == {42: 87.0}

    async def test_once_the_completion_has_begun_no_row_is_made(self, db_session, stage, sessions):
        await _session_row(db_session, stage)

        with usage_tracker.completion_in_progress(stage.printer.id):
            assert await _late_attach(db_session, stage, [_tray(0, remain=-1)]) == "unchanged"

        assert await _row(db_session, stage) is None

    async def test_a_completion_that_begins_while_the_row_is_being_written_leaves_none_behind(
        self, db_session, stage, sessions
    ):
        """The print ends between the last check and the row's commit: the report has already looked
        for the row (or holds its own copy), and nothing would ever remove this one."""
        await _session_row(db_session, stage)

        # Not running at either check before the write; running once the row is committed.
        with patch.object(usage_tracker, "completion_is_running", side_effect=[False, False, True]):
            assert await _late_attach(db_session, stage, [_tray(0, remain=-1)]) == "unchanged"

        assert await _row(db_session, stage) is None

    async def test_after_the_print_was_charged_no_row_is_left_behind(self, db_session, stage, sessions):
        """A manual retry on a finished archive: its session is gone, and so is its row."""
        assert await _late_attach(db_session, stage, [_tray(0, remain=-1)]) == "unchanged"

        assert await _row(db_session, stage) is None

    async def test_another_prints_session_proves_nothing(self, db_session, stage, sessions):
        await _session_row(db_session, stage, identity="another-execution")

        assert await _late_attach(db_session, stage, [_tray(0, remain=-1)]) == "unchanged"

        assert await _row(db_session, stage) is None

    async def test_the_made_row_carries_the_dispatched_mapping_and_no_remain_snapshot(
        self, db_session, stage, sessions
    ):
        await _session_row(db_session, stage)

        await _late_attach(db_session, stage, [_tray(0, remain=60, uuid=UUID)])

        row = await _row(db_session, stage)
        assert row.slot_to_tray == [0]
        # 60 % is where the print stood when the file arrived, not where it began.
        assert row.tray_remain_start is None
        assert set(row.ams_trays) == {"0"}


# ── the retry tells the tracking ────────────────────────────────────────────


class TestTheDownloadRetryTellsTheTracking:
    async def test_a_recovered_file_is_handed_over(self, db_session, stage, sessions, monkeypatch, tmp_path):
        from backend.app.services import archive_download_retry as retry_module

        monkeypatch.setattr(retry_module, "async_session", sessions)
        downloaded = tmp_path / "bracket.gcode.3mf"
        downloaded.write_bytes(b"x")

        async def _attach(self, archive_id, *_a, **_kw):
            row = await self.db.get(PrintArchive, archive_id)
            row.file_path = "archive/1/bracket/bracket.gcode.3mf"
            await self.db.commit()
            return True

        told = AsyncMock(return_value="filled")
        with (
            patch.object(retry_module, "try_download_3mf", AsyncMock(return_value=(downloaded, "bracket.gcode.3mf"))),
            patch.object(retry_module.ArchiveService, "attach_3mf_to_archive", _attach),
            patch("backend.app.services.archive.load_objects_from_archive_into_state"),
            patch("backend.app.services.layer_projection.notify_current_print_source_ready"),
            patch.object(retry_module.ws_manager, "send_archive_updated", AsyncMock()),
            patch("backend.app.services.spoolman_tracking.attach_recovered_3mf", told),
        ):
            assert await retry_module.archive_download_retry.retry_archive(stage.archive.id) == "recovered"

        told.assert_awaited_once()
        args, kwargs = told.await_args
        assert args[:3] == (stage.printer.id, stage.archive.id, "archive/1/bracket/bracket.gcode.3mf")
        assert kwargs["archive_identity"] == stage.archive.recovery_identity

    async def test_a_tracking_failure_never_fails_the_recovery(
        self, db_session, stage, sessions, monkeypatch, tmp_path
    ):
        from backend.app.services import archive_download_retry as retry_module

        monkeypatch.setattr(retry_module, "async_session", sessions)
        downloaded = tmp_path / "bracket.gcode.3mf"
        downloaded.write_bytes(b"x")

        async def _attach(self, archive_id, *_a, **_kw):
            row = await self.db.get(PrintArchive, archive_id)
            row.file_path = "archive/1/bracket/bracket.gcode.3mf"
            await self.db.commit()
            return True

        with (
            patch.object(retry_module, "try_download_3mf", AsyncMock(return_value=(downloaded, "bracket.gcode.3mf"))),
            patch.object(retry_module.ArchiveService, "attach_3mf_to_archive", _attach),
            patch("backend.app.services.archive.load_objects_from_archive_into_state"),
            patch("backend.app.services.layer_projection.notify_current_print_source_ready"),
            patch.object(retry_module.ws_manager, "send_archive_updated", AsyncMock()),
            patch(
                "backend.app.services.spoolman_tracking.attach_recovered_3mf",
                AsyncMock(side_effect=RuntimeError("spoolman tracking broke")),
            ),
        ):
            assert await retry_module.archive_download_retry.retry_archive(stage.archive.id) == "recovered"
