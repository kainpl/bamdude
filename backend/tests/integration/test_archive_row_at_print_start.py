"""The archive row for an external print exists before its 3MF is fetched.

``on_print_start`` used to create the row *after* pulling the 3MF back off
the printer over FTP. Measured on a live P1S on 2026-08-16, that fetch took
**8m40s** for a 22 MB file — 43 KB/s, because the file comes back over the
same SD card the print is reading from. (The identical fetch on an idle
printer took 96 s.) For those eight minutes the print existed nowhere in
BamDude: no card in Archives, no start notification, no busy queue, and no
energy baseline.

⚠️ **The baseline is the one waiting cannot repair.** Every other symptom is
a delay and nothing more; a smart-plug counter read eight minutes late
silently drops eight minutes of consumption from the print, always in the
same direction. That is why the ordering assertions here are on the *events*
and not merely on the final DB shape — "the row eventually exists" was true
before this change too.

⚠️ **Exactly one row per physical print.** Two branches downstream of the
download can still conclude the print already had an archive (the
plate-corrected re-download, and content-hash adoption). Each is a place
where the speculative row has to be withdrawn rather than left behind, so
the tests below count rows, not just inspect them.
"""

from __future__ import annotations

import hashlib
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive

PRINT_DATA = {
    "filename": "/data/Metadata/plate_3.gcode",
    "subtask_name": "Plate_3",
    "subtask_id": "sub-42",
}


def _write_3mf(path: Path, *, plate_index: int = 3, bed_type: str = "textured_plate") -> bytes:
    """A 3MF whose slice_info carries the plate index and bed type."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "Metadata/slice_info.config",
            "<config><plate>"
            f'<metadata key="index" value="{plate_index}" />'
            f'<metadata key="curr_bed_type" value="{bed_type}" />'
            '<metadata key="prediction" value="3600" />'
            '<metadata key="weight" value="12.5" />'
            "</plate></config>",
        )
    return path.read_bytes()


class _Recorder:
    """Ordered log of the events whose *sequence* is the thing under test."""

    def __init__(self) -> None:
        self.seq: list[str] = []
        self.created_ids: list[int] = []

    def mark(self, name: str):
        async def _record(*args, **kwargs):
            self.seq.append(name)
            return None

        return AsyncMock(side_effect=_record)

    def archive_created(self):
        async def _record(payload, *args, **kwargs):
            self.seq.append("archive_created")
            self.created_ids.append(payload["id"])

        return AsyncMock(side_effect=_record)


async def _prepare_printer(printer_factory):
    printer = await printer_factory()
    printer.auto_archive = True
    printer.plate_detection_enabled = False
    printer.external_camera_enabled = False
    return printer


async def _drive_print_start(
    *,
    db_session,
    test_engine,
    tmp_path,
    monkeypatch,
    printer,
    download_result,
    recorder: _Recorder,
):
    """Run ``on_print_start`` against the in-memory DB with the network stubbed."""
    from backend.app.core.config import settings as app_settings
    from backend.app.main import on_print_start

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)

    test_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.main.async_session", test_factory)
    # Helpers reached from here open their own sessions off the module-level
    # factory; without this they would write to the developer's real database.
    monkeypatch.setattr("backend.app.core.database.async_session", test_factory)

    async def _download(*args, **kwargs):
        recorder.seq.append("download")
        return download_result

    ws = MagicMock()
    ws.send_print_start = AsyncMock()
    ws.send_archive_created = recorder.archive_created()
    ws.send_archive_updated = recorder.mark("archive_updated")
    ws.broadcast = AsyncMock()

    with (
        patch("backend.app.main.ws_manager", ws),
        patch("backend.app.main.printer_manager", MagicMock()),
        patch("backend.app.main.mqtt_relay", MagicMock(on_print_start=AsyncMock(), on_archive_created=AsyncMock())),
        patch("backend.app.main.smart_plug_manager", MagicMock(on_print_start=AsyncMock())),
        patch("backend.app.main.notify_missing_spool_assignments_on_print_start", new_callable=AsyncMock),
        patch("backend.app.services.macro_trigger.fire_event_macros", new_callable=AsyncMock),
        patch("backend.app.main._send_print_start_notification", recorder.mark("notification")),
        patch("backend.app.main._record_energy_start", recorder.mark("energy_start")),
        patch("backend.app.main._list_timelapse_videos", new_callable=AsyncMock, return_value=([], None)),
        patch("backend.app.services.archive_download.try_download_3mf", side_effect=_download),
        patch(
            "backend.app.services.bambu_ftp.get_ftp_retry_settings",
            new_callable=AsyncMock,
            return_value=(False, 0, 0, 30),
        ),
    ):
        await on_print_start(printer.id, dict(PRINT_DATA))

    db_session.expire_all()
    return (await db_session.execute(select(PrintArchive).order_by(PrintArchive.id))).scalars().all()


@pytest.fixture(autouse=True)
def _clear_active_prints():
    """``_active_prints`` is module state — a leftover key from one test makes
    the next one return at the duplicate guard and assert against nothing."""
    from backend.app.main import _active_prints

    _active_prints.clear()
    yield
    _active_prints.clear()


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheRowComesFirst:
    async def test_the_archive_exists_before_the_download_is_attempted(
        self, db_session, test_engine, tmp_path, monkeypatch, printer_factory
    ):
        printer = await _prepare_printer(printer_factory)
        rec = _Recorder()
        src = tmp_path / "Plate_3.gcode.3mf"
        _write_3mf(src)

        rows = await _drive_print_start(
            db_session=db_session,
            test_engine=test_engine,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            printer=printer,
            download_result=(src, "Plate_3.gcode.3mf"),
            recorder=rec,
        )

        assert "download" in rec.seq, "the download never ran — the test proved nothing"
        download_at = rec.seq.index("download")
        for event in ("archive_created", "notification", "energy_start"):
            assert event in rec.seq, f"{event} never happened"
            assert rec.seq.index(event) < download_at, (
                f"{event} still waits for the 3MF: {rec.seq}. The energy baseline in "
                f"particular cannot be read late without under-counting the print."
            )
        assert len(rows) == 1

    async def test_the_row_created_up_front_is_the_one_that_gets_the_file(
        self, db_session, test_engine, tmp_path, monkeypatch, printer_factory
    ):
        """No second row on the success path — the first one is filled in place."""
        printer = await _prepare_printer(printer_factory)
        rec = _Recorder()
        src = tmp_path / "Plate_3.gcode.3mf"
        _write_3mf(src)

        rows = await _drive_print_start(
            db_session=db_session,
            test_engine=test_engine,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            printer=printer,
            download_result=(src, "Plate_3.gcode.3mf"),
            recorder=rec,
        )

        assert len(rows) == 1, f"one physical print, {len(rows)} archive rows"
        row = rows[0]
        assert row.id == rec.created_ids[0], "the row that got the file is not the row announced at print start"
        assert row.file_path, "the 3MF was downloaded but never attached"
        assert (row.extra_data or {}).get("no_3mf_available") is None
        assert row.status == "printing"
        assert row.subtask_id == "sub-42"

    async def test_the_plate_comes_from_the_live_state_not_the_file(
        self, db_session, test_engine, tmp_path, monkeypatch, printer_factory
    ):
        """``plate_index`` is what the attach parser reads back to decide which
        plate to describe, so it has to be right *before* the file arrives."""
        printer = await _prepare_printer(printer_factory)
        rec = _Recorder()

        rows = await _drive_print_start(
            db_session=db_session,
            test_engine=test_engine,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            printer=printer,
            download_result=None,  # never mind the file — the plate is already known
            recorder=rec,
        )

        assert len(rows) == 1
        assert rows[0].plate_index == 3, "plate 3 was running; the row says otherwise"

    async def test_a_failed_download_marks_the_row_it_did_not_create(
        self, db_session, test_engine, tmp_path, monkeypatch, printer_factory
    ):
        """⚠️ ``no_3mf_available`` means "we tried and could not" — it drives a
        warning banner in Archives, so it must NOT be set optimistically at
        creation, only once an attempt has actually failed."""
        printer = await _prepare_printer(printer_factory)
        rec = _Recorder()

        rows = await _drive_print_start(
            db_session=db_session,
            test_engine=test_engine,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            printer=printer,
            download_result=None,
            recorder=rec,
        )

        assert len(rows) == 1
        assert rows[0].file_path == "", "empty file_path is what the retry triggers select on"
        assert (rows[0].extra_data or {}).get("no_3mf_available") is True
        assert (rows[0].extra_data or {}).get("_print_data", {}).get("subtask_name") == "Plate_3"


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheSpeculativeRowIsWithdrawn:
    async def test_hash_adoption_leaves_exactly_one_row(
        self, db_session, test_engine, tmp_path, monkeypatch, printer_factory
    ):
        """Restart-mid-print: an in-flight row already exists but is named in a
        way the pre-download name lookup misses. The downloaded bytes identify
        it by hash — and the row this handler created on the way there has to
        go, or the print ends up with two archives.
        """
        printer = await _prepare_printer(printer_factory)
        src = tmp_path / "Plate_3.gcode.3mf"
        payload = _write_3mf(src)
        content_hash = hashlib.sha256(payload).hexdigest()

        # Named so the lenient cleanup rule keeps it (basename matches) but the
        # stricter name-match adoption misses it (full path, different
        # print_name) — the exact gap content-hash adoption exists to cover.
        existing = PrintArchive(
            printer_id=printer.id,
            filename="/data/Metadata/Plate_3.gcode.3mf",
            file_path="archive/old/Plate_3.gcode.3mf",
            file_size=len(payload),
            print_name="named before the restart",
            content_hash=content_hash,
            source_content_hash=content_hash,
            plate_index=3,
            status="printing",
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(existing)
        await db_session.commit()
        await db_session.refresh(existing)
        existing_id = existing.id

        rec = _Recorder()
        rows = await _drive_print_start(
            db_session=db_session,
            test_engine=test_engine,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            printer=printer,
            download_result=(src, "Plate_3.gcode.3mf"),
            recorder=rec,
        )

        # Without this the test would also pass if the *name-match* block had
        # adopted the row — it returns before the download, so it never creates
        # a second row and never needs one withdrawn. That is a different code
        # path and would leave this one untested.
        assert "download" in rec.seq, "name-match adopted first — content-hash adoption was never exercised"
        assert [r.id for r in rows] == [existing_id], (
            "the print-start row survived alongside the adopted one — one physical print, two archives"
        )

    async def test_no_active_print_key_survives_pointing_at_the_deleted_row(
        self, db_session, test_engine, tmp_path, monkeypatch, printer_factory
    ):
        """A key left behind on a deleted id reads to the duplicate guard as
        "this print is already tracked", and the next event returns early."""
        from backend.app.main import _active_prints

        printer = await _prepare_printer(printer_factory)
        src = tmp_path / "Plate_3.gcode.3mf"
        payload = _write_3mf(src)
        content_hash = hashlib.sha256(payload).hexdigest()

        existing = PrintArchive(
            printer_id=printer.id,
            filename="/data/Metadata/Plate_3.gcode.3mf",
            file_path="archive/old/Plate_3.gcode.3mf",
            file_size=len(payload),
            print_name="named before the restart",
            content_hash=content_hash,
            source_content_hash=content_hash,
            plate_index=3,
            status="printing",
            started_at=datetime.now(timezone.utc),
        )
        db_session.add(existing)
        await db_session.commit()
        await db_session.refresh(existing)
        existing_id = existing.id

        rec = _Recorder()
        await _drive_print_start(
            db_session=db_session,
            test_engine=test_engine,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            printer=printer,
            download_result=(src, "Plate_3.gcode.3mf"),
            recorder=rec,
        )

        assert "download" in rec.seq, "name-match adopted first — content-hash adoption was never exercised"
        live_ids = set(_active_prints.values())
        assert live_ids == {existing_id}, f"stale ids left in _active_prints: {live_ids - {existing_id}}"
