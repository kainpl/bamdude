"""``attach_3mf_to_archive`` has to fill what ``archive_print`` fills.

The two are the only ways a 3MF's metadata reaches an archive row, and since
``on_print_start`` began creating the row up front and filling it in later,
the attach is the path **every external print** takes — not just the rare
recovered one. Anything ``archive_print`` writes and the attach forgets is a
column that is simply NULL for that whole class of print.

Three were forgotten, and each is silent in a different way:

* ``bed_type`` — just missing.
* ``plate_index`` — the attach *reads* it to choose which plate to describe,
  so a row that arrives without one keeps NULL for ever even though the
  parser worked the answer out on its way past.
* ``extra_data["plate_id"]`` — the mirror ``queue_virtual.py`` still reads.

⚠️ ``plate_index`` is backfilled, never overwritten. The column records what
the printer was actually running, taken from live MQTT state; the 3MF only
knows what it contains. Where they disagree the live state is right.
"""

from __future__ import annotations

import asyncio
import threading
import zipfile
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.services.archive import ArchiveService

PLATES = {
    2: {"prediction": 3600, "weight": 12.5, "bed": "textured_plate"},
    5: {"prediction": 7200, "weight": 99.0, "bed": "cool_plate"},
}


def _multi_plate_3mf(path: Path) -> Path:
    body = "".join(
        f"<plate>"
        f'<metadata key="index" value="{idx}" />'
        f'<metadata key="prediction" value="{spec["prediction"]}" />'
        f'<metadata key="weight" value="{spec["weight"]}" />'
        f'<metadata key="curr_bed_type" value="{spec["bed"]}" />'
        f"</plate>"
        for idx, spec in PLATES.items()
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", f"<config>{body}</config>")
    return path


async def _attach(db_session, tmp_path, monkeypatch, printer, *, plate_index: int | None):
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)

    archive = PrintArchive(
        printer_id=printer.id,
        filename="Plate.gcode.3mf",
        file_path="",
        file_size=0,
        print_name="Plate",
        status="printing",
        plate_index=plate_index,
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)

    src = _multi_plate_3mf(tmp_path / "src.gcode.3mf")
    ok = await ArchiveService(db_session).attach_3mf_to_archive(archive.id, src, "Plate.gcode.3mf")
    assert ok, "attach failed — the assertions below would be about nothing"
    await db_session.refresh(archive)
    return archive


@pytest.mark.asyncio
@pytest.mark.integration
class TestFieldParity:
    async def test_cancelled_prepare_eventually_removes_its_private_staging_tree(
        self, db_session, tmp_path, monkeypatch, printer_factory
    ):
        """Cancelling the request must not leave a large recovered 3MF behind."""
        from backend.app.core.config import settings as app_settings
        from backend.app.services.archive import _prepare_archive_attach_file

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
        (tmp_path / "archive").mkdir(parents=True, exist_ok=True)
        printer = await printer_factory()
        archive = PrintArchive(
            printer_id=printer.id,
            filename="cancel.gcode.3mf",
            file_path="",
            file_size=0,
            print_name="Cancel",
            status="printing",
        )
        db_session.add(archive)
        await db_session.commit()
        source = _multi_plate_3mf(tmp_path / "cancel.gcode.3mf")

        loop = asyncio.get_running_loop()
        prepare_started = asyncio.Event()
        release_prepare = threading.Event()

        def slow_prepare(*args, **kwargs):
            loop.call_soon_threadsafe(prepare_started.set)
            release_prepare.wait(timeout=5)
            return _prepare_archive_attach_file(*args, **kwargs)

        monkeypatch.setattr("backend.app.services.archive._prepare_archive_attach_file", slow_prepare)
        task = asyncio.create_task(ArchiveService(db_session).attach_3mf_to_archive(archive.id, source))
        await asyncio.wait_for(prepare_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        release_prepare.set()
        staging = tmp_path / "archive" / str(printer.id) / ".attach-staging"
        for _ in range(20):
            if not staging.exists() or not any(staging.iterdir()):
                break
            await asyncio.sleep(0.05)
        assert not staging.exists() or not any(staging.iterdir())

    async def test_slow_prepare_does_not_hold_sqlite_archive_writer(
        self, db_session, test_engine, tmp_path, monkeypatch, printer_factory
    ):
        """Hash/copy/parse runs before the short publication transaction.

        This is deliberately a barrier test, not a timing assertion: while a
        large recovered 3MF is being prepared, an unrelated archive writer
        must acquire SQLite's sole writer. The old method held BEGIN IMMEDIATE
        around all three operations and deadlocked this exact shape.
        """
        from backend.app.core.config import settings as app_settings
        from backend.app.services.archive import _prepare_archive_attach_file
        from backend.app.services.archive_write_scope import archive_write_scope

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
        (tmp_path / "archive").mkdir(parents=True, exist_ok=True)
        printer = await printer_factory()
        first = PrintArchive(
            printer_id=printer.id,
            filename="slow.gcode.3mf",
            file_path="",
            file_size=0,
            print_name="Slow",
            status="printing",
        )
        second = PrintArchive(
            printer_id=printer.id,
            filename="other.gcode.3mf",
            file_path="",
            file_size=0,
            print_name="Other",
            status="printing",
        )
        db_session.add_all([first, second])
        await db_session.commit()
        source = _multi_plate_3mf(tmp_path / "slow.gcode.3mf")

        loop = asyncio.get_running_loop()
        prepare_started = asyncio.Event()
        release_prepare = threading.Event()

        def slow_prepare(*args, **kwargs):
            loop.call_soon_threadsafe(prepare_started.set)
            release_prepare.wait(timeout=5)
            return _prepare_archive_attach_file(*args, **kwargs)

        monkeypatch.setattr("backend.app.services.archive._prepare_archive_attach_file", slow_prepare)
        attach_task = asyncio.create_task(ArchiveService(db_session).attach_3mf_to_archive(first.id, source))
        await asyncio.wait_for(prepare_started.wait(), timeout=1)

        maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        other_entered = asyncio.Event()

        async def write_other_archive():
            async with maker() as other_db, archive_write_scope(other_db, second.id):
                other_entered.set()
                await other_db.commit()

        writer = asyncio.create_task(write_other_archive())
        await asyncio.wait_for(other_entered.wait(), timeout=0.5)
        release_prepare.set()
        await writer
        assert await attach_task

    async def test_bed_type_is_written(self, db_session, tmp_path, monkeypatch, printer_factory):
        printer = await printer_factory()
        archive = await _attach(db_session, tmp_path, monkeypatch, printer, plate_index=2)

        assert archive.bed_type == "textured_plate"

    async def test_a_missing_plate_index_is_backfilled_from_the_file(
        self, db_session, tmp_path, monkeypatch, printer_factory
    ):
        """Single-plate exports carry nothing in the gcode filename to parse,
        so the row legitimately arrives without one."""
        printer = await printer_factory()
        archive = await _attach(db_session, tmp_path, monkeypatch, printer, plate_index=None)

        assert archive.plate_index == 2, "the parser knew the plate and the column stayed NULL"

    async def test_a_known_plate_index_is_not_overwritten(self, db_session, tmp_path, monkeypatch, printer_factory):
        """⚠️ The column is what the printer is running; the container holds
        several plates and cannot arbitrate between them."""
        printer = await printer_factory()
        archive = await _attach(db_session, tmp_path, monkeypatch, printer, plate_index=5)

        assert archive.plate_index == 5
        # ...and the metadata that landed is plate 5's, not the first plate's.
        assert archive.print_time_seconds == 7200
        assert archive.filament_used_grams == pytest.approx(99.0)
        assert archive.bed_type == "cool_plate"

    async def test_extra_data_mirrors_the_plate_column(self, db_session, tmp_path, monkeypatch, printer_factory):
        printer = await printer_factory()
        archive = await _attach(db_session, tmp_path, monkeypatch, printer, plate_index=5)

        assert (archive.extra_data or {}).get("plate_id") == 5

    async def test_plate_correction_with_the_same_file_is_not_a_duplicate_retry(
        self, db_session, tmp_path, monkeypatch, printer_factory
    ):
        """A later live plate correction must re-read the same 3MF's plate."""
        printer = await printer_factory()
        archive = await _attach(db_session, tmp_path, monkeypatch, printer, plate_index=2)
        archive_id = archive.id
        archive.plate_index = 5
        await db_session.commit()

        source = tmp_path / "src.gcode.3mf"
        assert await ArchiveService(db_session).attach_3mf_to_archive(archive_id, source, "Plate.gcode.3mf")
        corrected = await db_session.get(PrintArchive, archive_id)

        assert corrected is not None
        assert corrected.print_time_seconds == 7200
        assert corrected.filament_used_grams == pytest.approx(99.0)
        assert (corrected.extra_data or {}).get("_attached_plate_index") == 5

    async def test_failed_path_attach_can_be_marked_and_retried(
        self, db_session, tmp_path, monkeypatch, printer_factory
    ):
        """A rollback expires ORM rows; caller recovery uses scalar ID + reload."""
        from backend.app.core.config import settings as app_settings

        monkeypatch.setattr(app_settings, "base_dir", tmp_path)
        monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
        (tmp_path / "archive").mkdir(parents=True, exist_ok=True)
        printer = await printer_factory()
        archive = PrintArchive(
            printer_id=printer.id,
            filename="recovered.gcode.3mf",
            file_path="",
            file_size=0,
            print_name="Recovered",
            status="printing",
        )
        db_session.add(archive)
        await db_session.commit()
        archive_id = archive.id
        src = _multi_plate_3mf(tmp_path / "recovered.gcode.3mf")
        service = ArchiveService(db_session)

        assert not await service.attach_3mf_to_archive(archive_id, src, "../escape.gcode.3mf")
        # ``get`` is awaited before any ORM field is accessed: this is the
        # same boundary on_print_start crosses after a rollback.
        assert await service.mark_3mf_unavailable(archive_id)
        recovered = await db_session.get(PrintArchive, archive_id)
        assert recovered is not None
        assert recovered.file_path == ""
        assert (recovered.extra_data or {}).get("no_3mf_available") is True

        assert await service.attach_3mf_to_archive(archive_id, src, "recovered.gcode.3mf")
        recovered = await db_session.get(PrintArchive, archive_id)
        assert recovered is not None
        first_file_path = recovered.file_path
        assert first_file_path
        assert (recovered.extra_data or {}).get("no_3mf_available") is None
        assert await service.attach_3mf_to_archive(archive_id, src, "recovered.gcode.3mf")
        recovered = await db_session.get(PrintArchive, archive_id)
        assert recovered is not None and recovered.file_path == first_file_path
        assert not await service.mark_3mf_unavailable(archive_id)
        recovered = await db_session.get(PrintArchive, archive_id)
        assert recovered is not None
        assert (recovered.extra_data or {}).get("no_3mf_available") is None
