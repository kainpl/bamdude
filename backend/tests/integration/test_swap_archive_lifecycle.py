"""The execution archive records the job's own declaration -- swap-source-lifecycle spec r4 §3, §7; plan T3.

``archive_print`` guesses nothing: both runners hand it the fact the job captured, and the archive writes it as
its record (``extra_data["swap_source"]``) with the bool column as its projection. The library row the job came
from may be renamed or re-declared afterwards; the print that ran keeps what it ran with, and a reprint of that
archive carries it on -- an unknown included, through any number of reprints.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.services import queue_sources, swap_compatibility as sc
from backend.tests.integration.test_adopt_print_started_while_down import _clean_module_state, main_db  # noqa: F401
from backend.tests.integration.test_dispatch_without_original import (
    PLATE,
    a_sliced_file,
    an_archive_source,
    dispatch_mocks,
    drain,
    scheduler_with_captured_dispatch,
)
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def clean_state():
    from backend.app.services.printer_manager import printer_manager

    def clear():
        queue_sources._reset_state()
        for attribute in (
            "_print_run_bindings",
            "_print_run_finishing_bindings",
            "_print_start_resolutions",
            "_pending_print_terminals",
        ):
            registry = getattr(printer_manager, attribute, None)
            if isinstance(registry, dict):
                registry.clear()
        # A plate-clear gate another module armed on the process-wide manager would park these printers.
        printer_manager._awaiting_plate_clear.clear()

    clear()
    yield clear
    deadline = time.monotonic() + 10
    while queue_sources.active_captures() and time.monotonic() < deadline:  # pragma: no cover - drain
        time.sleep(0.01)
    clear()


@pytest.fixture
def sessions(test_engine, monkeypatch):
    from backend.app.core import database

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    return factory


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def queue_and_dispatch(db, monkeypatch, test_engine, queue, **source) -> PrintArchive:
    """Queue through the real writer (it captures), dispatch through the real scheduler, return the execution."""
    from backend.app.services.queue_add import add_items_to_printer_queue

    items, _queue = await add_items_to_printer_queue(
        db, PrintQueueItemCreate(queue_id=queue.id, plate_id=PLATE, **source), None
    )
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory)
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)
    assert await scheduler.check_queue() is True
    assert await drain(spawned) == 1
    item = await db.get(PrintQueueItem, items[0].id, populate_existing=True)
    assert item.status == "printing"
    return await db.get(PrintArchive, item.archive_id, populate_existing=True)


async def finish(db, archive: PrintArchive, queue: PrinterQueue, clear) -> None:
    """The print ended: its archive is a completed source, the printer is free again."""
    archive.status = "completed"
    # Re-read: the dispatch wrote these rows from its own sessions, so this session's copies are stale.
    for item in (await db.execute(select(PrintQueueItem).execution_options(populate_existing=True))).scalars():
        item.status = "completed"
    claim = await db.get(PrinterQueue, queue.id, populate_existing=True)
    claim.status, claim.current_item_id = "idle", None
    await db.commit()
    clear()


@pytest.mark.parametrize("declared", [True, False])
async def test_execution_archive_uses_fact_not_linked_library(
    declared, db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    path = a_sliced_file(tmp_path / ("lamp.swap.gcode.3mf" if declared else "lamp.gcode.3mf"))
    library = LibraryFile(
        filename=path.name,
        file_path=str(path),
        file_size=path.stat().st_size,
        file_type="gcode",
        file_hash=sha(path),
        swap_compatible=declared,
    )
    db_session.add(library)
    await db_session.commit()
    from backend.app.services.queue_add import add_items_to_printer_queue

    items, _queue = await add_items_to_printer_queue(
        db_session, PrintQueueItemCreate(queue_id=queue.id, library_file_id=library.id, plate_id=PLATE), None
    )
    # After the job was queued the library row changes its mind and its name -- the job captured the old answer.
    library.swap_compatible = not declared
    library.filename = "renamed.swaps.gcode.3mf" if declared is False else "renamed.gcode.3mf"
    await db_session.commit()

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory)
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)
    assert await scheduler.check_queue() is True
    assert await drain(spawned) == 1

    item = await db_session.get(PrintQueueItem, items[0].id, populate_existing=True)
    execution = await db_session.get(PrintArchive, item.archive_id, populate_existing=True)
    assert execution.library_file_id == library.id  # the link is kept, it is just not the authority
    fact = sc.archive_fact(execution)
    assert fact.recorded is True
    assert fact.value is declared and fact.pending_external is False
    assert execution.swap_compatible is declared


@pytest.mark.parametrize("start", ["legacy False", "recorded True", "recorded False"])
async def test_unknown_survives_reprint_of_reprint(
    start, db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, clean_state
):
    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    first = await an_archive_source(db_session, tmp_path, printer.id, name="shelf.swap.gcode.3mf")
    expected = {"legacy False": None, "recorded True": True, "recorded False": False}[start]
    if start != "legacy False":
        sc.write_archive_fact(first, expected)
        await db_session.commit()

    chain = [first]
    for _hop in range(2):
        execution = await queue_and_dispatch(db_session, monkeypatch, test_engine, queue, archive_id=chain[-1].id)
        fact = sc.archive_fact(execution)
        # Recorded, and the same answer -- never upgraded by the ".swap." in its name, never a legacy False.
        assert fact.recorded is True
        assert fact.value is expected, (start, len(chain))
        chain.append(execution)
        await finish(db_session, execution, queue, clean_state)


async def test_parsed_metadata_cannot_write_the_record(db_session, printer_factory, tmp_path, monkeypatch):
    """A 3MF whose parsed metadata carries the reserved key declares nothing through it."""
    import backend.app.services.archive as archive_module
    from backend.app.services.archive import ArchiveService

    printer = await printer_factory()
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    path = a_sliced_file(tmp_path / "forged.gcode.3mf")
    real_parse = archive_module.ThreeMFParser.parse

    def forged(self, *args, **kwargs):
        parsed = real_parse(self, *args, **kwargs)
        parsed[sc.CARRIER_KEY] = {"version": 1, "value": True, "pending_external": False}
        return parsed

    monkeypatch.setattr(archive_module.ThreeMFParser, "parse", forged)
    archive = await ArchiveService(db_session).archive_print(
        printer_id=printer.id,
        source_file=path,
        source_content_hash=sha(path),
        print_data={"status": "printing"},
        source_swap_compatible=False,
    )

    assert sc.archive_fact(archive) == sc.ArchiveFact(False, recorded=True)


# --- the name a download really succeeded under (§7: FTP multi-path) -----------------------------------------------


class FakeFTPClient:
    """``BambuFTPClient`` over a set of remote paths that exist; counts the sessions it opened."""

    present: set[str] = set()
    sessions = 0

    def __init__(self, *args, **kwargs):
        pass

    def connect(self):
        FakeFTPClient.sessions += 1
        return True

    def download_to_file(self, remote_path, local_path):
        from backend.app.services.bambu_ftp import FileNotOnPrinterError

        if remote_path not in FakeFTPClient.present:
            raise FileNotOnPrinterError(remote_path)
        Path(local_path).write_bytes(b"PK")
        return True

    def disconnect(self):
        pass


@pytest.fixture
def fake_ftp(monkeypatch):
    import backend.app.services.archive_download  # noqa: F401 - bound to the real client before the patch
    import backend.app.services.bambu_ftp as bambu_ftp

    FakeFTPClient.present, FakeFTPClient.sessions = set(), 0
    monkeypatch.setattr(bambu_ftp, "BambuFTPClient", FakeFTPClient)
    return FakeFTPClient


async def test_ftp_bool_wrapper_keeps_old_contract_and_one_session(fake_ftp, tmp_path):
    import asyncio

    from backend.app.services.bambu_ftp import download_file_first_path_async, download_file_try_paths_async

    paths = ["/a.3mf", "/cache/b.3mf", "/model/c.3mf"]
    fake_ftp.present = {"/cache/b.3mf", "/model/c.3mf"}
    # The new answer is the path that worked -- the first that exists, not the first that was asked.
    found = await asyncio.wait_for(download_file_first_path_async("10.0.0.9", "x", paths, tmp_path / "t"), 5)
    assert found == "/cache/b.3mf"
    assert fake_ftp.sessions == 1
    # The old bool contract, on ONE session and ONE serialization: a second lock on the same printer would hang.
    assert await asyncio.wait_for(download_file_try_paths_async("10.0.0.9", "x", paths, tmp_path / "t"), 5) is True
    assert fake_ftp.sessions == 2
    fake_ftp.present = set()
    assert await asyncio.wait_for(download_file_try_paths_async("10.0.0.9", "x", paths, tmp_path / "t"), 5) is False
    assert await download_file_first_path_async("10.0.0.9", "x", paths, tmp_path / "t") is None


def _printer_for_download():
    from unittest.mock import MagicMock

    return MagicMock(id=7, ip_address="10.0.0.9", access_code="12345678", model="X1C", name="X1C")


@pytest.mark.parametrize(
    ("subtask", "filename", "present", "listing", "expected"),
    [
        # The marked candidate is asked first and is not there; the plain one is -- the file is the plain one.
        ("lamp.swap", "/data/Metadata/plate_1.gcode", {"/plate_1.gcode.3mf"}, [], "plate_1.gcode.3mf"),
        # The reverse: a plain first candidate is missing, the marked one downloaded.
        ("lamp", "/sdcard/lamp.swap.gcode.3mf", {"/cache/lamp.swap.gcode.3mf"}, [], "lamp.swap.gcode.3mf"),
        # Fuzzy: the first listed match does not download, the second does.
        (
            "lamp",
            None,
            {"/cache/lamp_v2.swap.3mf"},
            [{"name": "lamp_v1.3mf"}, {"name": "lamp_v2.swap.3mf"}],
            "lamp_v2.swap.3mf",
        ),
    ],
    ids=["direct-marked-missing", "direct-plain-missing", "fuzzy-second"],
)
async def test_external_pending_uses_successful_download_basename(
    subtask, filename, present, listing, expected, fake_ftp, tmp_path, monkeypatch
):
    from unittest.mock import AsyncMock

    from backend.app.services import archive_download

    fake_ftp.present = present

    async def listed(ip, code, directory, **kwargs):
        return [entry for entry in listing if directory == "/cache"] if listing else []

    monkeypatch.setattr(archive_download, "list_files_async", listed)
    monkeypatch.setattr(archive_download, "_try_internal_storage", AsyncMock(return_value=None))
    monkeypatch.setattr(archive_download, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30)))

    result = await archive_download.try_download_3mf(_printer_for_download(), subtask, filename, tmp_path)

    assert result is not None
    _temp_path, received = result
    assert received == expected
    assert sc.classify_external_name(received) is (".swap." in expected)


async def test_internal_storage_names_the_entry_it_read(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from backend.app.services import archive_download

    entries = [SimpleNamespace(name="lamp.swap.gcode.3mf", path="/lamp.swap.gcode.3mf")]
    transport = SimpleNamespace(list_files=AsyncMock(return_value=entries), read_bytes=AsyncMock(return_value=b"PK"))
    monkeypatch.setattr(
        "backend.app.utils.printer_storage.storage_capability_for", lambda *a: {"can_browse_internal": True}
    )
    monkeypatch.setattr("backend.app.services.printer_files.factory.transport_for", lambda *a: transport)

    result = await archive_download._try_internal_storage(
        _printer_for_download(), ["lamp.gcode.3mf", "lamp.swap.gcode.3mf"], tmp_path
    )

    assert result is not None and result[1] == "lamp.swap.gcode.3mf"


# --- external start, attach, adopt (§7) -----------------------------------------------------------------------------


async def an_external_row(db, printer_id, *, fact, pending=False, legacy_column=None, filename="lamp.gcode.3mf"):
    archive = PrintArchive(
        printer_id=printer_id,
        filename=filename,
        file_path="",
        file_size=0,
        print_name="lamp",
        status="printing",
        extra_data={"original_subtask": "lamp"},
    )
    if legacy_column is None:
        sc.write_archive_fact(archive, fact, pending_external=pending)
    else:
        archive.swap_compatible = legacy_column
    db.add(archive)
    await db.commit()
    return archive


async def attach(db, archive, received_name, tmp_path):
    from backend.app.services.archive import ArchiveService

    (tmp_path / "download").mkdir(parents=True, exist_ok=True)
    downloaded = a_sliced_file(tmp_path / "download" / (received_name or "x.gcode.3mf"))
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    assert await ArchiveService(db).attach_3mf_to_archive(
        archive.id, downloaded, received_name, received_name=received_name
    )
    return await db.get(PrintArchive, archive.id, populate_existing=True)


@pytest.mark.parametrize(("received", "expected"), [("lamp.swap.gcode.3mf", True), ("lamp.gcode.3mf", False)])
async def test_attach_finishes_only_a_pending_external_record(
    received, expected, db_session, printer_factory, tmp_path
):
    printer = await printer_factory()
    pending = await an_external_row(db_session, printer.id, fact=None, pending=True)

    attached = await attach(db_session, pending, received, tmp_path)

    assert sc.archive_fact(attached) == sc.ArchiveFact(expected, recorded=True)


async def test_attach_without_a_proven_name_leaves_pending_unknown(db_session, printer_factory, tmp_path):
    from backend.app.services.archive import ArchiveService

    printer = await printer_factory()
    pending = await an_external_row(db_session, printer.id, fact=None, pending=True)
    downloaded = a_sliced_file(tmp_path / "x.swap.gcode.3mf")
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    # A display name is no proof of what the transport read.
    assert await ArchiveService(db_session).attach_3mf_to_archive(pending.id, downloaded, "x.swap.gcode.3mf")

    attached = await db_session.get(PrintArchive, pending.id, populate_existing=True)
    assert sc.archive_fact(attached).value is None


@pytest.mark.parametrize("legacy_column", [False, True])
async def test_legacy_unattached_false_is_not_reclassified(legacy_column, db_session, printer_factory, tmp_path):
    printer = await printer_factory()
    legacy = await an_external_row(db_session, printer.id, fact=None, legacy_column=legacy_column)
    received = "lamp.gcode.3mf" if legacy_column else "lamp.swap.gcode.3mf"

    attached = await attach(db_session, legacy, received, tmp_path)

    # No record before, none after: a legacy False stays unknown (D1) and a legacy True stays True.
    assert sc.archive_fact(attached) == sc.ArchiveFact(True if legacy_column else None)
    assert sc.CARRIER_KEY not in (attached.extra_data or {})


@pytest.mark.parametrize(
    ("fact", "received"),
    [(False, "lamp.swap.gcode.3mf"), (None, "lamp.swap.gcode.3mf"), (True, "lamp.gcode.3mf")],
    ids=["captured False", "adopted unknown", "completed external True"],
)
async def test_capture_fact_and_completed_external_fact_survive_retry(
    fact, received, db_session, printer_factory, tmp_path
):
    printer = await printer_factory()
    recorded = await an_external_row(db_session, printer.id, fact=fact)

    attached = await attach(db_session, recorded, received, tmp_path)

    assert sc.archive_fact(attached) == sc.ArchiveFact(fact, recorded=True)


async def test_parsed_metadata_cannot_rewrite_the_record_on_attach(db_session, printer_factory, tmp_path, monkeypatch):
    import backend.app.services.archive as archive_module

    printer = await printer_factory()
    recorded = await an_external_row(db_session, printer.id, fact=False)
    real_parse = archive_module.ThreeMFParser.parse

    def forged(self, *args, **kwargs):
        parsed = real_parse(self, *args, **kwargs)
        parsed[sc.CARRIER_KEY] = {"version": 1, "value": True, "pending_external": False}
        return parsed

    monkeypatch.setattr(archive_module.ThreeMFParser, "parse", forged)
    attached = await attach(db_session, recorded, "lamp.gcode.3mf", tmp_path)

    assert sc.archive_fact(attached) == sc.ArchiveFact(False, recorded=True)


START_DATA = {"subtask_id": "sub-7", "raw_data": {}}


async def drive_external_start(db_session, test_engine, tmp_path, monkeypatch, printer, data, download_result=None):
    from unittest.mock import AsyncMock, MagicMock, patch

    from backend.app.core.config import settings as app_settings
    from backend.app.main import _active_prints, on_print_start

    _active_prints.clear()
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.main.async_session", factory)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    ws = MagicMock()
    for attribute in ("send_print_start", "send_archive_created", "send_archive_updated", "broadcast"):
        setattr(ws, attribute, AsyncMock())
    with (
        patch("backend.app.main.ws_manager", ws),
        patch("backend.app.main.printer_manager", MagicMock()),
        patch("backend.app.main.mqtt_relay", MagicMock(on_print_start=AsyncMock(), on_archive_created=AsyncMock())),
        patch("backend.app.main.smart_plug_manager", MagicMock(on_print_start=AsyncMock())),
        patch("backend.app.main.notify_missing_spool_assignments_on_print_start", new_callable=AsyncMock),
        patch("backend.app.services.macro_trigger.fire_event_macros", new_callable=AsyncMock),
        patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock),
        patch("backend.app.main._record_energy_start", new_callable=AsyncMock),
        patch("backend.app.main._list_timelapse_videos", new_callable=AsyncMock, return_value=([], None)),
        patch("backend.app.services.archive_download.try_download_3mf", AsyncMock(return_value=download_result)),
        patch("backend.app.services.bambu_ftp.get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30))),
    ):
        await on_print_start(printer.id, {**START_DATA, **data})
    _active_prints.clear()
    db_session.expire_all()
    return (await db_session.execute(select(PrintArchive).order_by(PrintArchive.id))).scalars().all()


async def an_archiving_printer(db_session, printer_factory):
    printer = await printer_factory()
    printer.auto_archive = True
    printer.plate_detection_enabled = False
    printer.external_camera_enabled = False
    await db_session.commit()
    return printer


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"filename": "/sdcard/lamp.swap.gcode.3mf", "subtask_name": "lamp"}, sc.ArchiveFact(True, recorded=True)),
        # A plate path names no file; the subtask helps find one but declares nothing by itself.
        (
            {"filename": "/data/Metadata/plate_1.gcode", "subtask_name": "lamp.swap"},
            sc.ArchiveFact(None, pending_external=True, recorded=True),
        ),
        ({"filename": "lamp.swap", "subtask_name": ""}, sc.ArchiveFact(None, pending_external=True, recorded=True)),
        ({"filename": "", "subtask_name": "lamp"}, sc.ArchiveFact(None, pending_external=True, recorded=True)),
    ],
    ids=["marked filename", "plate path + marked subtask", "stem without a boundary", "no filename"],
)
async def test_external_start_distinguishes_filename_subtask_and_stem(
    data, expected, db_session, test_engine, tmp_path, monkeypatch, printer_factory
):
    printer = await an_archiving_printer(db_session, printer_factory)

    rows = await drive_external_start(db_session, test_engine, tmp_path, monkeypatch, printer, data)

    assert len(rows) == 1
    assert sc.archive_fact(rows[0]) == expected


async def test_an_external_start_attaches_by_the_name_that_downloaded(
    db_session, test_engine, tmp_path, monkeypatch, printer_factory
):
    printer = await an_archiving_printer(db_session, printer_factory)
    (tmp_path / "dl").mkdir()
    downloaded = a_sliced_file(tmp_path / "dl" / "lamp.swaps.gcode.3mf")

    rows = await drive_external_start(
        db_session,
        test_engine,
        tmp_path,
        monkeypatch,
        printer,
        # The plate the synthetic file was sliced for, so the plate check accepts the download.
        {"filename": "/data/Metadata/plate_15.gcode", "subtask_name": "lamp"},
        download_result=(downloaded, "lamp.swaps.gcode.3mf"),
    )

    assert len(rows) == 1
    assert sc.archive_fact(rows[0]) == sc.ArchiveFact(True, recorded=True)


async def test_adopted_renamed_own_print_is_unknown_and_stays_so(db_session, printer_factory, main_db, tmp_path):
    from backend.tests.integration.test_adopt_print_started_while_down import _drive_hook

    _printer_id, rows, _spies, _spawned = await _drive_hook(
        db_session=db_session,
        printer_factory=printer_factory,
        data={"filename": "/sdcard/lamp.swap.gcode.3mf", "subtask_name": "lamp", "remaining_time": 60, "raw_data": {}},
    )

    assert len(rows) == 1
    # An adopted run's origin is not proven: a marked name is not an authority, and no attach may finish it.
    assert sc.archive_fact(rows[0]) == sc.ArchiveFact(None, recorded=True)
    attached = await attach(db_session, rows[0], "lamp.swap.gcode.3mf", tmp_path)
    assert sc.archive_fact(attached) == sc.ArchiveFact(None, recorded=True)


# --- the binding's proof of origin (§7, §10; for T7's swap-clear) ---------------------------------------------------


async def drive_start_with_manager(db_session, test_engine, tmp_path, monkeypatch, printer, data, download_result=None):
    """``drive_external_start`` that also returns the manager double, whose registries hold the run binding."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from backend.app.core.config import settings as app_settings
    from backend.app.main import on_print_start

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.main.async_session", factory)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    manager = MagicMock()
    ws = MagicMock()
    for attribute in ("send_print_start", "send_archive_created", "send_archive_updated", "broadcast"):
        setattr(ws, attribute, AsyncMock())
    with (
        patch("backend.app.main.ws_manager", ws),
        patch("backend.app.main.printer_manager", manager),
        patch("backend.app.main.mqtt_relay", MagicMock(on_print_start=AsyncMock(), on_archive_created=AsyncMock())),
        patch("backend.app.main.smart_plug_manager", MagicMock(on_print_start=AsyncMock())),
        patch("backend.app.main.notify_missing_spool_assignments_on_print_start", new_callable=AsyncMock),
        patch("backend.app.services.macro_trigger.fire_event_macros", new_callable=AsyncMock),
        patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock),
        patch("backend.app.main._record_energy_start", new_callable=AsyncMock),
        patch("backend.app.main._list_timelapse_videos", new_callable=AsyncMock, return_value=([], None)),
        patch("backend.app.services.archive_download.try_download_3mf", AsyncMock(return_value=download_result)),
        patch("backend.app.services.bambu_ftp.get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30))),
    ):
        await on_print_start(printer.id, {**START_DATA, **data})
    db_session.expire_all()
    rows = (await db_session.execute(select(PrintArchive).order_by(PrintArchive.id))).scalars().all()
    return rows, manager


def binding_of(manager, printer_id):
    from backend.app.services.print_run_binding import current_print_run

    return current_print_run(manager, printer_id)


async def a_running_archive(db, printer_id, path: Path, *, fact, **columns):
    from datetime import datetime, timezone

    archive = PrintArchive(
        printer_id=printer_id,
        filename=path.name,
        file_path=str(path.relative_to(settings.base_dir)),
        file_size=path.stat().st_size,
        print_name=columns.pop("print_name", "lamp"),
        status="printing",
        started_at=columns.pop("started_at", datetime.now(timezone.utc)),
        print_time_seconds=columns.pop("print_time_seconds", 3600),
        content_hash=sha(path),
        source_content_hash=sha(path),
        plate_index=15,
        **columns,
    )
    sc.write_archive_fact(archive, fact)
    db.add(archive)
    await db.commit()
    return archive


async def test_exact_rebind_keeps_execution_carrier(db_session, test_engine, tmp_path, monkeypatch, printer_factory):
    from backend.app.main import _active_prints
    from backend.app.services.print_run_binding import origin_is_proven

    printer = await an_archiving_printer(db_session, printer_factory)
    printer_id = printer.id  # read before the drive expires every ORM object
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    (tmp_path / "archive" / "1").mkdir(parents=True, exist_ok=True)
    ours = await a_running_archive(
        db_session,
        printer.id,
        a_sliced_file(tmp_path / "archive" / "1" / "lamp.gcode.3mf"),
        fact=False,
        subtask_id="777",
    )
    _active_prints.clear()  # a restart: no aliases, no binding -- only the device's own id

    rows, manager = await drive_start_with_manager(
        db_session,
        test_engine,
        tmp_path,
        monkeypatch,
        printer,
        {"filename": "/data/Metadata/plate_15.gcode", "subtask_name": "lamp.swap", "subtask_id": "777"},
    )

    assert [row.id for row in rows] == [ours.id]  # recovered, not a second row
    bound = binding_of(manager, printer_id)
    assert bound is not None and bound.archive_id == ours.id
    assert origin_is_proven(bound)
    assert sc.archive_fact(rows[0]) == sc.ArchiveFact(False, recorded=True)  # the execution record, untouched


async def test_name_only_rebind_has_no_swap_authority(db_session, test_engine, tmp_path, monkeypatch, printer_factory):
    from backend.app.main import _active_prints
    from backend.app.services.print_run_binding import origin_is_proven

    printer = await an_archiving_printer(db_session, printer_factory)
    printer_id = printer.id  # read before the drive expires every ORM object
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    (tmp_path / "archive" / "1").mkdir(parents=True, exist_ok=True)
    named = await a_running_archive(
        db_session,
        printer.id,
        a_sliced_file(tmp_path / "archive" / "1" / "lamp.swap.gcode.3mf"),
        fact=True,
        print_name="lamp.swap",
    )
    _active_prints.clear()

    rows, manager = await drive_start_with_manager(
        db_session,
        test_engine,
        tmp_path,
        monkeypatch,
        printer,
        {"filename": "/data/Metadata/plate_15.gcode", "subtask_name": "lamp.swap", "subtask_id": ""},
    )

    bound = binding_of(manager, printer_id)
    assert [row.id for row in rows] == [named.id]  # adopted by its name, as archiving always did
    assert bound is not None and bound.archive_id == named.id
    assert not origin_is_proven(bound)  # ...and that is all a name buys: no swap authority
    assert sc.archive_fact(rows[0]) == sc.ArchiveFact(True, recorded=True)


async def test_provisional_carrier_does_not_overwrite_retained_twin(
    db_session, test_engine, tmp_path, monkeypatch, printer_factory
):
    from backend.app.main import _active_prints
    from backend.app.services.print_run_binding import origin_is_proven

    printer = await an_archiving_printer(db_session, printer_factory)
    printer_id = printer.id  # read before the drive expires every ORM object
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    (tmp_path / "archive" / "1").mkdir(parents=True, exist_ok=True)
    twin_file = a_sliced_file(tmp_path / "archive" / "1" / "shelf.gcode.3mf")
    # No start and no estimate (an earlier adoption whose file never parsed), so the stale-row cleanup leaves it
    # for the hash to find.
    twin = await a_running_archive(
        db_session, printer.id, twin_file, fact=True, print_name="shelf", started_at=None, print_time_seconds=None
    )
    _active_prints.clear()
    (tmp_path / "dl").mkdir()
    downloaded = tmp_path / "dl" / "other.swap.gcode.3mf"
    downloaded.write_bytes(twin_file.read_bytes())  # the same bytes under another name: a hash twin

    rows, manager = await drive_start_with_manager(
        db_session,
        test_engine,
        tmp_path,
        monkeypatch,
        printer,
        {"filename": "/data/Metadata/plate_15.gcode", "subtask_name": "other.swap", "subtask_id": ""},
        download_result=(downloaded, "other.swap.gcode.3mf"),
    )

    assert [row.id for row in rows] == [twin.id]  # the provisional row was withdrawn
    assert sc.archive_fact(rows[0]) == sc.ArchiveFact(True, recorded=True)  # its own record, not the provisional's
    bound = binding_of(manager, printer_id)
    assert bound is not None and bound.archive_id == twin.id
    assert not origin_is_proven(bound)  # a hash twin is no proof of the run


async def test_a_new_external_start_is_a_proven_origin(db_session, test_engine, tmp_path, monkeypatch, printer_factory):
    from backend.app.main import _active_prints
    from backend.app.services.print_run_binding import origin_is_proven

    printer = await an_archiving_printer(db_session, printer_factory)
    printer_id = printer.id  # read before the drive expires every ORM object
    _active_prints.clear()

    rows, manager = await drive_start_with_manager(
        db_session,
        test_engine,
        tmp_path,
        monkeypatch,
        printer,
        {"filename": "/sdcard/lamp.swap.gcode.3mf", "subtask_name": "lamp", "subtask_id": ""},
    )

    bound = binding_of(manager, printer_id)
    assert bound is not None and bound.archive_id == rows[0].id
    assert origin_is_proven(bound)


def test_only_the_named_origins_are_proven():
    from backend.app.services.print_run_binding import PrintRunBinding, origin_is_proven

    assert [
        origin
        for origin in ("dispatch", "external", "exact_subtask")
        if origin_is_proven(PrintRunBinding(printer_id=1, archive_id=1, origin=origin))
    ] == ["dispatch", "external", "exact_subtask"]
    for origin in ("observed", "adopted", "", "DISPATCH"):
        assert not origin_is_proven(PrintRunBinding(printer_id=1, archive_id=1, origin=origin))
    assert not origin_is_proven(None)


async def test_our_expected_dispatch_start_is_a_proven_origin(
    db_session, test_engine, tmp_path, monkeypatch, printer_factory
):
    """The dispatcher registered the print; the start binds to it as ours even when no prepared binding survived."""
    from backend.app.main import _active_prints, _expected_prints
    from backend.app.services.print_run_binding import origin_is_proven

    printer = await an_archiving_printer(db_session, printer_factory)
    printer_id = printer.id
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    (tmp_path / "archive" / "1").mkdir(parents=True, exist_ok=True)
    ours = await a_running_archive(
        db_session, printer_id, a_sliced_file(tmp_path / "archive" / "1" / "lamp.gcode.3mf"), fact=False
    )
    _active_prints.clear()
    _expected_prints.clear()
    _expected_prints[(printer_id, "lamp.gcode.3mf")] = ours.id
    try:
        rows, manager = await drive_start_with_manager(
            db_session,
            test_engine,
            tmp_path,
            monkeypatch,
            printer,
            {"filename": "/sdcard/lamp.gcode.3mf", "subtask_name": "lamp", "subtask_id": ""},
        )
    finally:
        _expected_prints.clear()

    bound = binding_of(manager, printer_id)
    assert [row.id for row in rows] == [ours.id]
    assert bound is not None and bound.archive_id == ours.id
    assert origin_is_proven(bound)


# --- metadata merges after an await (§7: energy, reconciliation) -----------------------------------------------------


async def a_pending_finished_archive(db, printer_id, **columns):
    from datetime import datetime, timezone

    archive = PrintArchive(
        printer_id=printer_id,
        filename="lamp.gcode.3mf",
        file_path="",
        file_size=0,
        print_name="lamp",
        status=columns.pop("status", "completed"),
        started_at=datetime.now(timezone.utc),
        extra_data={"original_subtask": "lamp"},
        **columns,
    )
    sc.write_archive_fact(archive, None, pending_external=True)
    db.add(archive)
    await db.commit()
    return archive


async def attach_elsewhere(factory, archive_id):
    """What the download retry does in its own session: it finishes the pending record -- here, to True."""
    async with factory() as other:
        row = await other.get(PrintArchive, archive_id, populate_existing=True)
        sc.write_archive_fact(row, True)
        await other.commit()


async def test_energy_merge_cannot_erase_concurrent_attach_fact(db_session, test_engine, printer_factory, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import backend.app.main as main_module

    printer = await printer_factory()
    archive = await a_pending_finished_archive(db_session, printer.id, energy_start_kwh=10.0)
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(main_module, "async_session", factory)
    # A plug from before m211: no measurement source -- but a real row, which the write checks it against.
    from backend.app.models.smart_plug import SmartPlug

    plug = SmartPlug(name="meter", plug_type="tasmota", ip_address="10.0.0.9", enabled=True)
    db_session.add(plug)
    await db_session.commit()
    monkeypatch.setattr(
        main_module,
        "_energy_plug_for_printer",
        AsyncMock(return_value=SimpleNamespace(id=plug.id, measurement_source_id=None)),
    )
    monkeypatch.setattr(main_module.smart_plug_manager, "record_energy_snapshot", AsyncMock())
    phases: list[str] = []

    async def reading_while_the_file_lands(plug, db, force_read=False):
        phases.append("read")
        await attach_elsewhere(factory, archive.id)  # inside the await: after the energy read, before its write
        phases.append("attached")
        return {"total": 11.5}

    monkeypatch.setattr(main_module, "_get_plug_energy", reading_while_the_file_lands)

    await main_module._record_print_energy(archive.id, printer.id, approximate=True)

    assert phases == ["read", "attached"]
    row = await db_session.get(PrintArchive, archive.id, populate_existing=True)
    assert row.energy_kwh == 1.5
    assert row.extra_data["energy_is_approximate"] is True
    assert sc.archive_fact(row) == sc.ArchiveFact(True, recorded=True)  # the attach's answer survives


async def test_reconcile_merge_cannot_erase_concurrent_attach_fact(
    db_session, test_engine, printer_factory, monkeypatch
):
    from unittest.mock import AsyncMock

    from backend.app.services import print_reconciliation, usage_tracker
    from backend.app.services.printer_manager import printer_manager

    printer = await printer_factory()
    archive = await a_pending_finished_archive(db_session, printer.id, status="printing")
    loaded = await db_session.get(PrintArchive, archive.id)  # the sweep's snapshot, taken before the awaits
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(printer_manager, "arm_awaiting_plate_clear", AsyncMock())
    monkeypatch.setattr(usage_tracker, "get_persisted_print_name", AsyncMock(return_value=None))

    async def booking_while_the_file_lands(*args, **kwargs):
        await attach_elsewhere(factory, archive.id)

    monkeypatch.setattr(usage_tracker, "on_print_complete", booking_while_the_file_lands)

    await print_reconciliation._reconcile_complete_archive(db_session, loaded, status="completed", uncertain=False)
    await db_session.commit()

    row = await db_session.get(PrintArchive, archive.id, populate_existing=True)
    assert row.status == "completed"
    assert row.extra_data["recovered_by_startup_sweep"] is True
    assert sc.archive_fact(row) == sc.ArchiveFact(True, recorded=True)


async def test_structural_recovery_cannot_erase_an_attach_fact_from_a_stale_snapshot(
    db_session, test_engine, printer_factory
):
    """The sweep hands in rows it read earlier; an accepted terminal's structural close must not write that copy."""
    from datetime import datetime, timezone

    from backend.app.main import _TERMINAL_ACCEPTANCE_KEY
    from backend.app.services import print_reconciliation

    printer = await printer_factory()
    queue = await db_session.get(PrinterQueue, printer.id)
    if queue is None:
        queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing")
        db_session.add(queue)
        await db_session.commit()
    archive = await a_pending_finished_archive(db_session, printer.id, status="printing")
    started = datetime.now(timezone.utc).replace(microsecond=0)
    item = PrintQueueItem(queue_id=queue.id, archive_id=archive.id, status="printing", position=1, started_at=started)
    db_session.add(item)
    await db_session.commit()
    marker = {
        "version": 2,
        "stage": "accepted",
        "outcome": "completed",
        "queue_item_id": item.id,
        "queue_started_at": started.isoformat(),
    }
    archive.extra_data = {**archive.extra_data, _TERMINAL_ACCEPTANCE_KEY: marker}
    await db_session.commit()
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sweep:
        stale = await sweep.get(PrintArchive, archive.id)
        await attach_elsewhere(factory, archive.id)  # lands after the sweep read its rows

        assert await print_reconciliation._recover_accepted_terminal_checkpoint(sweep, stale) == archive.id
        await sweep.commit()

    row = await db_session.get(PrintArchive, archive.id, populate_existing=True)
    assert row.extra_data["terminal_effects_uncertain"] is True
    assert sc.archive_fact(row) == sc.ArchiveFact(True, recorded=True)


# --- the matrix: both runners x library/archive x original present/absent --------------------------------------------


@pytest.mark.parametrize("original", ["present", "gone"])
@pytest.mark.parametrize("kind", ["library", "archive"])
async def test_execution_fact_follows_the_job_with_or_without_its_original(
    kind, original, db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_items_to_printer_queue
    from backend.tests.integration.test_dispatch_without_original import lose_the_original

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    if kind == "library":
        path = a_sliced_file(tmp_path / "lamp.gcode.3mf")
        source = LibraryFile(
            filename=path.name,
            file_path=str(path),
            file_size=path.stat().st_size,
            file_type="gcode",
            file_hash=sha(path),
            swap_compatible=True,
        )
        db_session.add(source)
        await db_session.commit()
        declared, link = True, {"library_file_id": source.id}
    else:
        source = await an_archive_source(db_session, tmp_path, printer.id)
        sc.write_archive_fact(source, False)
        await db_session.commit()
        path = settings.base_dir / source.file_path
        declared, link = False, {"archive_id": source.id}

    items, _queue = await add_items_to_printer_queue(
        db_session, PrintQueueItemCreate(queue_id=queue.id, plate_id=PLATE, **link), None
    )
    item = items[0]
    if original == "gone":
        await lose_the_original(db_session, item, path, row=source)

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory)
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)
    assert await scheduler.check_queue() is True
    assert await drain(spawned) == 1

    item = await db_session.get(PrintQueueItem, item.id, populate_existing=True)
    execution = await db_session.get(PrintArchive, item.archive_id, populate_existing=True)
    assert sc.archive_fact(execution) == sc.ArchiveFact(declared, recorded=True)
    mqtt._client.publish.assert_called_once()


# --- the plate-corrected re-download names the file it fetched (§7) --------------------------------------------------


@pytest.mark.parametrize(
    ("stale_subtask", "first_name", "corrected_name", "expected"),
    [
        ("lamp.swap.x_plate_14", "lamp.swap.x_plate_14.gcode.3mf", "lamp.swap.x_plate_15.gcode.3mf", True),
        # The stale first file carried the marker; the file that is really printing does not.
        ("lamp_plate_14", "lamp.swap_plate_14.gcode.3mf", "lamp_plate_15.gcode.3mf", False),
    ],
    ids=["corrected marked", "stale marked, corrected plain"],
)
async def test_the_plate_retry_attaches_by_the_corrected_file(
    stale_subtask, first_name, corrected_name, expected, db_session, test_engine, tmp_path, monkeypatch, printer_factory
):
    import backend.app.services.bambu_ftp as bambu_ftp
    from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
    from backend.tests.integration.test_dispatch_without_original import PLATE_FILAMENTS

    printer = await an_archiving_printer(db_session, printer_factory)
    (tmp_path / "dl").mkdir()
    stale = write_routing_3mf(tmp_path / "dl" / first_name, {14: PLATE_FILAMENTS})  # the previous plate's upload

    async def the_corrected_upload(ip, code, remote_path, local_path, **kwargs):
        if remote_path == f"/cache/{corrected_name}":
            write_routing_3mf(Path(local_path), {15: PLATE_FILAMENTS})
            return True
        raise bambu_ftp.FileNotOnPrinterError(remote_path)

    monkeypatch.setattr(bambu_ftp, "download_file_async", the_corrected_upload)
    rows = await drive_external_start(
        db_session,
        test_engine,
        tmp_path,
        monkeypatch,
        printer,
        {"filename": "/data/Metadata/plate_15.gcode", "subtask_name": stale_subtask},
        download_result=(stale, first_name),
    )

    assert len(rows) == 1
    assert rows[0].filename == corrected_name
    assert sc.archive_fact(rows[0]) == sc.ArchiveFact(expected, recorded=True)
