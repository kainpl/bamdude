"""A queued job prints its snapshot — the original is never opened again (spec §7).

Spec: ``60-specs/queue-source-spool-spec.md`` §7, A02, A18, S2, S7, S9. Everything
here drives the REAL consumers — the auto-queue assignment, ``check_queue``,
``filament_preflight``, both ``background_dispatch`` runners and
``ArchiveService.archive_print`` — with only device I/O mocked, because the claim
under test is about which bytes those paths read.

The proof is a **trap, not a deletion**: the original file stays on disk and every
``open`` / ``stat`` of it raises. A test that merely deleted the original would
pass for a path that still tried to read it and swallowed the error; this one
names the path that did. ``test_the_trap_catches_a_legacy_job_reading_its_original``
is the harness's own control — it proves the trap fires when the code does look.
"""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import time
import zipfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import database
from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.queue_source import STATE_READY, QueueSource
from backend.app.schemas.auto_queue import AutoQueueItemCreate
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.services import queue_sources
from backend.app.services.filament_routing import RoutingDeferred
from backend.app.services.print_run_binding import discard_print_run
from backend.app.services.printer_manager import printer_manager
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration


PLATE = 15
PLATE_FILAMENTS = [{"id": 3, "type": "PLA", "color": "#FF0000", "used_g": "0.0001"}]


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def clean_spool_state():
    """Process-global capture state must not travel between tests."""
    queue_sources._reset_state()
    # These tests deliberately stop after synthetic dispatch rather than feed a
    # terminal MQTT event.  Start each independent fixture with the same empty
    # runtime binding a fresh server would have; otherwise a fake run from an
    # earlier test can correctly veto a later test's dispatch for the wrong
    # reason.
    for attribute in (
        "_print_run_bindings",
        "_print_run_finishing_bindings",
        "_print_start_resolutions",
        "_pending_print_terminals",
    ):
        registry = getattr(printer_manager, attribute, None)
        if isinstance(registry, dict):
            registry.clear()
    yield
    deadline = time.monotonic() + 10
    while queue_sources.active_captures() and time.monotonic() < deadline:  # pragma: no cover - drain
        time.sleep(0.01)
    queue_sources._reset_state()


@pytest.fixture
def sessions(test_engine, monkeypatch):
    """``queue_sources.publish`` owns its transaction, so it needs this engine."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    return factory


def _key(target) -> str | None:
    """A comparable spelling of a path, without touching the filesystem."""
    if isinstance(target, int):  # an open file descriptor, not a path
        return None
    try:
        return os.path.normcase(os.path.abspath(os.fspath(target)))
    except TypeError:
        return None


def forbid_reads(monkeypatch, *paths: Path) -> list[str]:
    """Make every ``open``/``stat`` of ``paths`` raise, and record the attempt.

    Five patches, because every one of them is a way around the other four and the
    whole value of this trap is that there is no way around it — a reader that
    walked past it would leave all fifteen ``touched == []`` assertions asserting
    nothing:

    * ``Path.stat`` — the existence guards (``require_source_file`` asks
      ``is_file()``, ``SourceIdentity.of`` asks ``stat()``);
    * ``builtins.open`` **and** ``io.open`` — the readers (``zipfile``, the hasher,
      the FTP upload, and ``Path.open``, which calls ``io.open`` directly, so
      patching ``builtins`` alone would miss it);
    * ``os.stat`` — everything that does not go through ``pathlib``, including
      ``os.path.getsize``, and none of it touches ``Path.stat``;
    * ``os.path.isfile`` and ``os.path.exists`` — ⚠️ measured, not assumed: on
      CPython 3.12 for Windows these are the C fast paths ``nt._path_isfile`` /
      ``nt._path_exists``, which never call ``os.stat``, so patching ``os.stat``
      does **not** cover them;
    * ``os.open`` — the file-descriptor route, which no reader here uses today and
      which is exactly why it is closed.
    """
    forbidden = {_key(path) for path in paths}
    touched: list[str] = []
    real_path_stat, real_builtin_open, real_io_open = Path.stat, builtins.open, io.open
    real_os_stat, real_os_open = os.stat, os.open
    real_isfile, real_exists = os.path.isfile, os.path.exists

    def guard(target, what: str) -> None:
        key = _key(target)
        if key is not None and key in forbidden:
            touched.append(f"{what}:{key}")
            raise AssertionError(f"the ORIGINAL source was {what}ed after capture: {key}")

    def path_stat(self, *args, **kwargs):
        guard(self, "stat")
        return real_path_stat(self, *args, **kwargs)

    def watched(real, what: str):
        def call(target, *args, **kwargs):
            guard(target, what)
            return real(target, *args, **kwargs)

        return call

    monkeypatch.setattr(Path, "stat", path_stat)
    monkeypatch.setattr(builtins, "open", watched(real_builtin_open, "open"))
    monkeypatch.setattr(io, "open", watched(real_io_open, "open"))
    monkeypatch.setattr(os, "stat", watched(real_os_stat, "stat"))
    monkeypatch.setattr(os, "open", watched(real_os_open, "open"))
    monkeypatch.setattr(os.path, "isfile", watched(real_isfile, "stat"))
    monkeypatch.setattr(os.path, "exists", watched(real_exists, "stat"))
    return touched


def a_sliced_file(path: Path) -> Path:
    """A synthetic sliced 3MF whose plate gcode carries an uncommented ``M970``.

    The vibration-check line is what makes ``mesh_mode_fast_check=False`` produce
    a real patched copy, so a test can tell the archive's *source* bytes from its
    *dispatched* ones instead of asserting on two equal hashes.
    """
    plain = write_routing_3mf(path.with_suffix(".plain.3mf"), {PLATE: PLATE_FILAMENTS})
    with zipfile.ZipFile(plain) as src, zipfile.ZipFile(path, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename.endswith(".gcode"):
                data = b"M970 Q1 A1\n" + data
            dst.writestr(info.filename, data)
    plain.unlink()
    return path


async def a_library_source(db, tmp_path, *, name="part.gcode.3mf") -> LibraryFile:
    path = a_sliced_file(tmp_path / name)
    row = LibraryFile(
        filename=path.name,
        file_path=str(path),
        file_size=path.stat().st_size,
        file_type="gcode",
        file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    db.add(row)
    await db.commit()
    return row


async def an_archive_source(db, tmp_path, printer_id: int, *, name="shelf.gcode.3mf") -> PrintArchive:
    """An archive row whose 3MF is where ``archive_print`` would have put it."""
    folder = settings.archive_dir / "source"
    folder.mkdir(parents=True, exist_ok=True)
    path = a_sliced_file(folder / name)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    row = PrintArchive(
        printer_id=printer_id,
        filename=path.name,
        file_path=str(path.relative_to(settings.base_dir)),
        file_size=path.stat().st_size,
        content_hash=digest,
        source_content_hash=digest,
        status="completed",
        plate_index=PLATE,
    )
    db.add(row)
    await db.commit()
    return row


async def one_blob(db) -> QueueSource:
    return (await db.execute(select(QueueSource).order_by(QueueSource.id))).scalars().one()


def object_of(blob: QueueSource) -> Path:
    return Path(settings.base_dir) / blob.relative_path


def dispatch_mocks(monkeypatch, factory, *, upload=None):
    """Everything between the runner and a real printer, and nothing else."""
    import backend.app.services.background_dispatch as bd
    import backend.app.services.print_scheduler as ps

    monkeypatch.setattr(ps, "async_session", factory)
    monkeypatch.setattr(bd, "async_session", factory)
    monkeypatch.setattr(bd, "resolve_dispatch_storage", lambda *_: ("external", None))
    monkeypatch.setattr(bd, "upload_file_async", upload or AsyncMock(return_value=True))
    monkeypatch.setattr(bd, "delete_file_async", AsyncMock(return_value=True))
    monkeypatch.setattr(bd, "list_files_async", AsyncMock(return_value=[]))
    monkeypatch.setattr(bd, "get_ftp_retry_settings", AsyncMock(return_value=(False, 0, 0, 30)))
    monkeypatch.setattr(bd.printer_manager, "ensure_fresh_connection_for_printer", AsyncMock(return_value=True))
    monkeypatch.setattr(bd, "_warn_on_filament_deficit", AsyncMock())
    monkeypatch.setattr(bd, "_apply_calibrations_for_print", AsyncMock())
    monkeypatch.setattr("backend.app.services.preheat.preheat_and_soak", AsyncMock())
    monkeypatch.setattr("backend.app.main.register_expected_print", MagicMock())
    monkeypatch.setattr("backend.app.main.withdraw_expected_print", MagicMock())
    monkeypatch.setattr("backend.app.main.register_swap_config", MagicMock())
    monkeypatch.setattr("backend.app.main.register_macro_selection", MagicMock())
    monkeypatch.setattr(bd.ws_manager, "broadcast", AsyncMock())
    service = bd.BackgroundDispatchService()
    monkeypatch.setattr(bd, "background_dispatch", service)
    monkeypatch.setattr(service, "_ensure_live_connection_before_start", AsyncMock())
    monkeypatch.setattr(service, "_run_swap_macro_if_needed", AsyncMock())
    monkeypatch.setattr(service, "_verify_print_response", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "_strict_stagger_refuses", AsyncMock(return_value=False))
    monkeypatch.setattr("backend.app.services.print_scheduler.scheduler.acquire_stagger_slot", AsyncMock())
    return service


def record_dispatch_contract(monkeypatch, service) -> list[tuple[str, int | None]]:
    """Every ``(kind, source_id)`` the scheduler hands the dispatcher.

    A wrapper around the real call, not a replacement: which runner the snapshot's
    provenance picks is a decision ``print_scheduler`` makes and nothing else can be
    observed from — both runners produce an archive with the same name and hash for
    a source-less job, so an assertion on the outcome cannot tell them apart.
    """
    seen: list[tuple[str, int | None]] = []
    real = service.run_from_queue_item

    async def spy(**kwargs):
        seen.append((kwargs["kind"], kwargs["source_id"]))
        return await real(**kwargs)

    monkeypatch.setattr(service, "run_from_queue_item", spy)
    return seen


def scheduler_with_captured_dispatch(monkeypatch) -> tuple[object, list]:
    """A real ``PrintScheduler`` whose spawned dispatch the test awaits itself."""
    import backend.app.services.print_scheduler as ps

    spawned: list = []
    monkeypatch.setattr(ps, "spawn_background_task", lambda coroutine, **_: spawned.append(coroutine))
    scheduler = ps.PrintScheduler()
    monkeypatch.setattr(scheduler, "_check_auto_drying", AsyncMock())
    monkeypatch.setattr(scheduler, "_watchdog_print_start", AsyncMock())
    return scheduler, spawned


async def drain(spawned: list) -> int:
    """Await every coroutine the scheduler spawned, in the order it spawned them.

    More than the dispatch lands here — the watchdog this harness stubs is spawned
    too — so the tests await the list rather than one index of it.
    """
    started = 0
    while spawned:
        coroutine = spawned.pop(0)
        if "_dispatch_and_finalize" in getattr(coroutine, "__qualname__", ""):
            started += 1
        await coroutine
    return started


async def lose_the_original(db, item, original: Path, *, row) -> None:
    """The case the feature exists for: the source row and its file are gone.

    The queue row keeps its ``queue_source_id`` and loses BOTH original
    references — a first-class job (§7), not a broken one.
    """
    item.archive_id = None
    item.library_file_id = None
    await db.delete(row)
    await db.commit()
    original.unlink()


# --------------------------------------------------------------------------- #
# A02 / S2 — the whole chain on a job whose original is gone
# --------------------------------------------------------------------------- #


async def test_an_assigned_auto_job_prints_after_its_library_file_is_gone(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    """auto add → assignment → check_queue → preflight → patch → md5 → upload → archive.

    The library row and its file are removed after the assignment, so every step
    of this runs on a job whose ``library_file_id`` and ``archive_id`` are both
    NULL — the state Task 5 left preflight refusing.
    """
    from backend.app.services.auto_queue_add import add_items_to_auto_queue
    from backend.app.services.auto_queue_scheduler import AutoQueueScheduler

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path, name="lamp.gcode.3mf")
    original = Path(source.file_path)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)

    await add_items_to_auto_queue(
        db_session,
        AutoQueueItemCreate(library_file_id=source.id, plate_ids=[PLATE], target_model="P1P"),
        None,
    )

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("backend.app.services.auto_queue_scheduler.async_session", session)
    await AutoQueueScheduler().tick()
    item = (await db_session.execute(select(PrintQueueItem))).scalars().one()
    blob = await one_blob(db_session)
    assert item.queue_source_id == blob.id
    item.mesh_mode_fast_check = False  # make the patcher produce a dispatched copy
    await lose_the_original(db_session, item, original, row=source)

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    service = dispatch_mocks(monkeypatch, factory)
    contract = record_dispatch_contract(monkeypatch, service)
    touched = forbid_reads(monkeypatch, original)
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)

    assert await scheduler.check_queue() is True
    assert await drain(spawned) == 1

    assert touched == []
    # A library-provenance snapshot goes to the library runner, with no original id.
    assert contract == [("print_library_file", None)]
    mqtt._client.publish.assert_called_once()
    command = json.loads(mqtt._client.publish.call_args.args[1])["print"]
    assert command["param"] == f"Metadata/plate_{PLATE}.gcode"
    # The printer is given the human name it was queued under, never the object's
    # own name, which is its hash (§4, A04). ``lamp.gcode.3mf`` → ``lamp.3mf`` is
    # ``derive_remote_filename``'s existing suffix collapse.
    assert command["file"] == "lamp.3mf"
    assert blob.sha256[:16] not in command["url"]

    await db_session.refresh(item)
    assert item.status == "printing"
    archive = await db_session.get(PrintArchive, item.archive_id)
    assert archive is not None and archive.status == "printing"
    assert archive.filename == source.filename
    assert archive.plate_index == PLATE
    # S9: the snapshot is the archive's SOURCE, the patched temp copy is what was
    # dispatched. The two hashes are what tells them apart.
    assert archive.source_content_hash == blob.sha256
    assert archive.content_hash != archive.source_content_hash
    assert json.loads(archive.applied_patches)
    # The archive owns its own bytes — a copy, not a pointer into the spool (§9:
    # the blob is released when the last job row lets go of it) — and keeps them
    # under the human name, so one archive tree does not hold two naming
    # conventions depending on how the print was queued.
    archived = Path(settings.base_dir) / archive.file_path
    assert archived.is_file() and archived != object_of(blob)
    assert archived.name == source.filename
    assert blob.sha256 not in archive.file_path
    assert archived.parent.name.endswith("_lamp")
    assert archived.read_bytes() == object_of(blob).read_bytes()


async def test_a_queue_job_reprints_after_its_archive_row_is_gone(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    """The reprint runner on a job whose source archive no longer exists."""
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    source = await an_archive_source(db_session, tmp_path, printer.id)
    original = Path(settings.base_dir) / source.file_path

    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, archive_id=source.id, plate_id=PLATE),
        None,
    )
    item = items[0]
    blob = await one_blob(db_session)
    assert item.queue_source_id == blob.id
    await lose_the_original(db_session, item, original, row=source)

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    service = dispatch_mocks(monkeypatch, factory)
    contract = record_dispatch_contract(monkeypatch, service)
    touched = forbid_reads(monkeypatch, original)
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)

    assert await scheduler.check_queue() is True
    assert await drain(spawned) == 1

    assert touched == []
    # An archive-provenance snapshot goes to the REPRINT runner — the mapping
    # ``print_scheduler`` makes from ``provenance["kind"]`` — and carries no
    # original id, because the row it names is gone.
    assert contract == [("reprint_archive", None)]
    mqtt._client.publish.assert_called_once()
    await db_session.refresh(item)
    assert item.status == "printing"
    # ⚠️ Not compared against the source archive's id: SQLite hands a deleted
    # row's id to the next INSERT, so "a new row" is proven by its content, not by
    # its number. (The dispatch is careful for the same reason — it resolves an
    # original by id only when the queue row still names one.)
    execution = await db_session.get(PrintArchive, item.archive_id)
    assert execution is not None and execution.status == "printing"
    assert execution.filename == "shelf.gcode.3mf"
    assert execution.source_content_hash == blob.sha256
    assert (Path(settings.base_dir) / execution.file_path).is_file()


async def test_a_direct_print_survives_its_library_row_being_trashed_mid_dispatch(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    """Print now, then the file is deleted while the upload runs — it still prints.

    The direct path takes its own capture before claiming the printer, so the
    runner has bytes even though the row it was started from is gone by the time
    it reads them.
    """
    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    source = await a_library_source(db_session, tmp_path, name="bracket.gcode.3mf")
    original = Path(source.file_path)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    service = dispatch_mocks(monkeypatch, factory)

    await service.dispatch_print_library_file(
        file_id=source.id,
        filename=source.filename,
        printer_id=printer.id,
        printer_name=printer.name,
        options={"plate_id": PLATE, "mesh_mode_fast_check": True},
        requested_by_user_id=None,
        requested_by_username=None,
        # Print-now-and-delete is precisely how the row goes away under a running
        # dispatch, so the cleanup has to survive finding it already gone.
        cleanup_library_after_dispatch=True,
    )
    job = service._queued_jobs[0]
    claim = await db_session.get(PrintQueueItem, job.queue_item_id)
    blob = await one_blob(db_session)
    assert claim.queue_source_id == blob.id
    await lose_the_original(db_session, claim, original, row=source)
    touched = forbid_reads(monkeypatch, original)

    await service._run_active_job(job)

    assert touched == []
    assert job.outcome["success"] is True, job.outcome
    mqtt._client.publish.assert_called_once()
    archive = await db_session.get(PrintArchive, job.outcome["archive_id"])
    assert archive is not None
    assert archive.filename == "bracket.gcode.3mf"
    assert archive.source_content_hash == blob.sha256
    assert archive.library_file_id is None


@pytest.mark.parametrize("owner", ["queue", "direct"])
async def test_a_payload_this_version_cannot_read_refuses_instead_of_printing_a_hash(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, owner
):
    """A future ``source_snapshot`` version fails closed — it never names a print.

    ``stored_descriptor`` degrades an unparsable payload to the object's own name,
    which is its sha256. That is fine for a label and unacceptable here: the same
    string decides whether the file looks sliced and becomes the file name on the
    printer (§4/A04). So the dispatch refuses with ``source_unreadable``, the
    reason a job whose source this BamDude cannot read has always had — and
    upgrading back makes the job printable again.

    Both doors are covered: the scheduler resolves the name before it flips the
    row to ``printing``, and the runner resolves it again for a direct print, which
    never passes through ``_start_print``.
    """
    from backend.app.services.filament_intake import routing_detail
    from backend.app.services.queue_add import add_items_to_printer_queue
    from backend.app.services.queue_source_descriptor import SOURCE_SNAPSHOT_VERSION

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    source = await a_library_source(db_session, tmp_path, name="downgraded.gcode.3mf")
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    service = dispatch_mocks(monkeypatch, factory)

    if owner == "direct":
        await service.dispatch_print_library_file(
            file_id=source.id,
            filename=source.filename,
            printer_id=printer.id,
            printer_name=printer.name,
            options={"plate_id": PLATE, "mesh_mode_fast_check": True},
            requested_by_user_id=None,
            requested_by_username=None,
        )
        job = service._queued_jobs[0]
        item = await db_session.get(PrintQueueItem, job.queue_item_id)
    else:
        items, _queue = await add_items_to_printer_queue(
            db_session,
            PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE),
            None,
        )
        item = items[0]
    blob = await one_blob(db_session)
    # What a row written by a LATER version looks like to this one.
    item.source_snapshot = {**(item.source_snapshot or {}), "version": SOURCE_SNAPSHOT_VERSION + 1}
    await lose_the_original(db_session, item, Path(source.file_path), row=source)
    refusal = routing_detail("source_unreadable")["message"]

    if owner == "direct":
        await service._run_active_job(job)
        assert job.outcome["deferred"] is True, job.outcome
        assert job.outcome["reason"]["code"] == "source_unreadable"
        await db_session.refresh(item)
        # A refused direct print is a failed row with the reason and a Retry —
        # not a waiting one (spec direct-print-silent-cancel §4.1).
        assert (item.status, item.error_message) == ("failed", refusal)
    else:
        scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)
        await scheduler.check_queue()
        assert await drain(spawned) == 0, "nothing may be dispatched for a job that cannot be named"
        await db_session.refresh(item)
        assert item.status == "failed"
        assert item.error_message == refusal

    mqtt._client.publish.assert_not_called()
    assert (await db_session.execute(select(func.count()).select_from(PrintArchive))).scalar() == 0
    # The refusal never quotes the object's name, and the object itself is intact:
    # this is a job BamDude cannot read, not a blob that went bad.
    assert blob.sha256 not in f"{item.error_message} {item.waiting_reason}"
    await db_session.refresh(blob)
    assert blob.state == STATE_READY and object_of(blob).is_file()


async def test_the_trap_catches_a_legacy_job_reading_its_original(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    """The control: a row with no snapshot still reads its original, and is caught.

    Without this the three tests above would also pass for a dispatch that never
    happened at all.
    """
    from backend.app.services.filament_policy_write import prepare_routing

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    source = await a_library_source(db_session, tmp_path, name="legacy.gcode.3mf")
    routing, plate = await prepare_routing(db_session, printer_id=printer.id, library_file_id=source.id)
    item = PrintQueueItem(
        queue_id=queue.id,
        library_file_id=source.id,
        filament_routing=routing,
        plate_id=plate,
        status="pending",
        position=1,
    )
    db_session.add(item)
    await db_session.commit()
    assert item.queue_source_id is None

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory)
    touched = forbid_reads(monkeypatch, Path(source.file_path))
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)

    # The trap's own ``AssertionError`` comes back out of whichever read reached
    # it first; where it surfaces is not the point, ``touched`` is.
    with suppress(AssertionError):
        await scheduler.check_queue()
        await drain(spawned)

    assert touched, "a legacy row must still read its original — otherwise the trap proves nothing"
    mqtt._client.publish.assert_not_called()


def test_the_trap_covers_the_routes_around_pathlib(tmp_path, monkeypatch):
    """Every door, not just the ones today's readers use.

    ``os.stat``, ``os.open`` and the ``os.path`` predicates reach the filesystem
    without touching ``Path.stat`` or ``builtins.open`` — and on this platform
    ``os.path.isfile`` does not even reach ``os.stat``. A future reader could have
    walked past the trap and left every ``touched == []`` assertion in this file
    asserting nothing at all.
    """
    guarded = tmp_path / "original.gcode.3mf"
    guarded.write_bytes(b"bytes")
    other = tmp_path / "somebody-else.gcode.3mf"
    other.write_bytes(b"bytes")
    touched = forbid_reads(monkeypatch, guarded)

    for read in (
        lambda: guarded.stat(),
        lambda: guarded.is_file(),
        lambda: guarded.open("rb"),
        # Deliberately unmanaged and deliberately spelled two ways: each of these
        # is a door being knocked on, and the trap raises before a handle exists.
        lambda: open(guarded, "rb"),  # noqa: SIM115
        lambda: io.open(guarded, "rb"),  # noqa: SIM115, UP020
        lambda: os.stat(guarded),
        lambda: os.path.isfile(guarded),
        lambda: os.path.exists(guarded),
        lambda: os.open(guarded, os.O_RDONLY),
    ):
        with pytest.raises(AssertionError, match="ORIGINAL source"):
            read()

    assert len(touched) == 9
    # And it is the named path that is trapped, not the filesystem: every other
    # read in the process has to keep working, or the harness would be proving
    # that nothing can read anything.
    assert other.is_file() and other.read_bytes() == b"bytes"
    assert os.path.isfile(other) and os.path.exists(other)


# --------------------------------------------------------------------------- #
# Obligation A — preflight answers from the job's own snapshot
# --------------------------------------------------------------------------- #


async def test_preflight_clears_a_job_whose_original_references_are_both_null(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.filament_preflight import final_guard, preflight_item
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path, name="knob.gcode.3mf")
    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE),
        None,
    )
    item = items[0]
    await lose_the_original(db_session, item, Path(source.file_path), row=source)

    guard = await preflight_item(db_session, item, printer.id)
    assert guard is not None
    assert (await final_guard(guard, printer.id)).plan.resolved_plate_id == PLATE


async def test_preflight_exempts_a_raw_gcode_snapshot_with_no_original_row(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """A raw ``.gcode`` job keeps its explicit per-printer workflow off the ROW's name.

    The exemption used to be read off the original path's suffix, which a job with
    no original row cannot answer — and then the 3MF reader refused the file as
    unreadable instead of letting it through.
    """
    from backend.app.services.filament_preflight import preflight_item
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    path = tmp_path / "hand.gcode"
    path.write_text("; synthetic raw G-code\n", encoding="utf-8")
    source = LibraryFile(filename=path.name, file_path=str(path), file_type="gcode", file_size=path.stat().st_size)
    db_session.add(source)
    await db_session.commit()

    items, _queue = await add_items_to_printer_queue(
        db_session, PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id), None
    )
    item = items[0]
    await lose_the_original(db_session, item, path, row=source)

    assert await preflight_item(db_session, item, printer.id) is None


async def test_a_restored_spool_mtime_does_not_defer_a_snapshot_job(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """A restore moves every spool mtime and not one byte, and the job still dispatches.

    ⚠️ It passes because the intent records the blob's **hash** (routing v2) and
    preflight compares that — NOT because a snapshot job ignores its revision. It
    did ignore it for one commit (Task 6's temporary reader-ignore) and restoring
    that ignore is now a *mutation*: it makes
    ``test_a_row_whose_blob_was_swapped_under_its_intent_is_refused`` fail. So this
    test is the portable-restore half of one pair — the comparison is live, and this
    pins that it compares something a restore cannot change.

    The intent here is written by the real ``_assign``, which is the writer whose
    stamp had to move to the hash at the same time as the reader.
    """
    from backend.app.services.auto_queue_add import add_items_to_auto_queue
    from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
    from backend.app.services.filament_preflight import preflight_item

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path, name="hinge.gcode.3mf")
    await add_items_to_auto_queue(
        db_session,
        AutoQueueItemCreate(library_file_id=source.id, plate_ids=[PLATE], target_model="P1P"),
        None,
    )

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("backend.app.services.auto_queue_scheduler.async_session", session)
    await AutoQueueScheduler().tick()
    item = (await db_session.execute(select(PrintQueueItem))).scalars().one()
    blob = await one_blob(db_session)
    await lose_the_original(db_session, item, Path(source.file_path), row=source)

    target = object_of(blob)
    os.utime(target, (time.time() + 120, time.time() + 120))

    assert await preflight_item(db_session, item, printer.id) is not None


# --------------------------------------------------------------------------- #
# §9 / obligation C — the blob is held for the dispatch, the archive copies it
# --------------------------------------------------------------------------- #


async def test_the_blob_is_pinned_for_the_whole_dispatch_and_released_after(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    """The GC must not be able to collect the bytes a print is reading (§9)."""
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    source = await a_library_source(db_session, tmp_path, name="pinned.gcode.3mf")
    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE),
        None,
    )
    item = items[0]
    blob = await one_blob(db_session)
    await lose_the_original(db_session, item, Path(source.file_path), row=source)

    pinned_during: list[frozenset[int]] = []

    async def upload(*args, **kwargs):
        pinned_during.append(queue_sources.pinned_source_ids())
        return True

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory, upload=AsyncMock(side_effect=upload))
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)

    assert await scheduler.check_queue() is True
    assert await drain(spawned) == 1

    assert pinned_during == [frozenset({blob.id})]
    assert queue_sources.pinned_source_ids() == frozenset()


async def test_two_prints_of_one_blob_share_the_archive_bytes(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    """Archive bytes keep their own lifecycle — and their own dedup (obligation C).

    Two copies of the same job dispatch from one blob; the second archive reuses
    the first one's file on disk instead of writing a second copy of the same
    bytes, exactly as two prints of one library file always did.
    """
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    source = await a_library_source(db_session, tmp_path, name="twice.gcode.3mf")
    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE, quantity=2),
        None,
    )
    blob = await one_blob(db_session)
    for item in items:
        item.archive_id = None
        item.library_file_id = None
    await db_session.delete(source)
    await db_session.commit()
    Path(source.file_path).unlink()

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory)
    # ``_start_print`` per copy rather than two ``check_queue`` ticks: the second
    # tick would see the printer this test never let go of (post-dispatch hold),
    # and the question here is about the archive's bytes, not about the gates.
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)

    archives: list[PrintArchive] = []
    for index, queued in enumerate(items):
        await scheduler._start_print(db_session, queued)
        assert await drain(spawned) == 1, index
        row = await db_session.get(PrintQueueItem, queued.id)
        await db_session.refresh(row)
        assert row.status == "printing", index
        archives.append(await db_session.get(PrintArchive, row.archive_id))
        # This fixture drives the scheduler directly to test archive-byte
        # deduplication.  Finish its synthetic first run before asking the
        # scheduler to admit the next one; a live claim is deliberately not
        # bypassed merely because this test did not start the MQTT completion
        # path.
        if index == 0:
            from backend.app.services.queue_counters import set_queue_idle

            row.status = "completed"
            archives[-1].status = "completed"
            await set_queue_idle(db_session, queue.id, expected_item_id=row.id)
            await db_session.commit()
            discard_print_run(printer_manager, printer.id, archives[-1].id)

    assert archives[0].id != archives[1].id
    assert archives[0].file_path == archives[1].file_path, "the same bytes must not be copied twice"
    assert {a.source_content_hash for a in archives} == {blob.sha256}
    assert len(list((settings.archive_dir / str(printer.id)).iterdir())) == 1


async def test_the_archive_records_the_hash_of_the_bytes_it_stored_not_the_shares_new_one(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    """``source_content_hash`` describes what went on disk, not what the row claims.

    The share is re-sliced after the job was queued and the library row is
    re-hashed with it — the very case the spool exists for. The archive stores the
    captured bytes, so recording the row's new hash would (a) lie about them and
    (b) hand this archive another row's ``file_path``, because on-disk dedup
    matches on exactly that hash.
    """
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    source = await a_library_source(db_session, tmp_path, name="resliced.gcode.3mf")
    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE),
        None,
    )
    item = items[0]
    blob = await one_blob(db_session)

    original = Path(source.file_path)
    with original.open("ab") as stream:
        stream.write(b"re-sliced on the share")
    stale_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    source.file_hash = stale_hash
    # Somebody else's archive already carries those bytes, so the dedup lookup has
    # a file to hand out if the wrong hash is recorded.
    stranger = await an_archive_source(db_session, tmp_path, printer.id, name="stranger.gcode.3mf")
    stranger.content_hash = stranger.source_content_hash = stale_hash
    await db_session.commit()

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory)
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)

    assert await scheduler.check_queue() is True
    assert await drain(spawned) == 1

    await db_session.refresh(item)
    archive = await db_session.get(PrintArchive, item.archive_id)
    assert archive.source_content_hash == blob.sha256 != stale_hash
    assert archive.file_path != stranger.file_path
    assert (Path(settings.base_dir) / archive.file_path).read_bytes() == object_of(blob).read_bytes()


async def test_a_deferred_snapshot_job_keeps_its_blob_and_invents_no_original(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    """A refusal before publish releases the claim and touches neither source ref."""
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    source = await a_library_source(db_session, tmp_path, name="deferred.gcode.3mf")
    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE),
        None,
    )
    item = items[0]
    blob = await one_blob(db_session)
    await lose_the_original(db_session, item, Path(source.file_path), row=source)

    async def upload(*args, **kwargs):
        # The feed changes under the dispatch: the external spool becomes PETG.
        mqtt._process_message({"print": {"vt_tray": {"id": 254, "tray_type": "PETG"}}})
        return True

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory, upload=AsyncMock(side_effect=upload))
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)

    assert await scheduler.check_queue() is True
    assert await drain(spawned) == 1

    mqtt._client.publish.assert_not_called()
    await db_session.refresh(item)
    assert item.status == "pending"
    assert (item.archive_id, item.library_file_id) == (None, None)
    assert item.queue_source_id == blob.id
    await db_session.refresh(blob)
    assert blob.state == STATE_READY
    assert object_of(blob).is_file()
    assert queue_sources.pinned_source_ids() == frozenset()


async def test_a_snapshot_job_is_not_refused_when_the_original_changes(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """A03: an accepted job keeps the bytes it accepted, whatever the share does."""
    from backend.app.services.filament_preflight import final_guard, preflight_item
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path, name="edited.gcode.3mf")
    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE),
        None,
    )
    item = items[0]
    with Path(source.file_path).open("ab") as stream:
        stream.write(b"somebody re-sliced it")

    guard = await preflight_item(db_session, item, printer.id)
    assert guard is not None
    assert await final_guard(guard, printer.id) is not None


async def test_a_snapshot_whose_object_is_missing_fails_its_own_job(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    """S7: a bad spool never falls back to the original — it names this job."""
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    source = await a_library_source(db_session, tmp_path, name="vanished.gcode.3mf")
    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE),
        None,
    )
    item = items[0]
    blob = await one_blob(db_session)
    object_of(blob).unlink()

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory)
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)

    await scheduler.check_queue()
    assert await drain(spawned) == 0

    mqtt._client.publish.assert_not_called()
    await db_session.refresh(item)
    assert item.status == "failed"
    assert (await db_session.execute(select(func.count()).select_from(PrintArchive))).scalar() == 0


async def test_preflight_still_refuses_a_legacy_job_whose_source_was_repointed(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """The ``{kind, id}`` scope check is not weakened — only narrowed to legacy rows."""
    from backend.app.services.filament_policy_write import prepare_routing

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path, name="scoped.gcode.3mf")
    other = await a_library_source(db_session, tmp_path, name="other.gcode.3mf")
    routing, plate = await prepare_routing(db_session, printer_id=printer.id, library_file_id=source.id)
    item = PrintQueueItem(
        queue_id=queue.id, library_file_id=other.id, filament_routing=routing, plate_id=plate, position=1
    )
    db_session.add(item)
    await db_session.commit()

    from backend.app.services.filament_preflight import preflight_item

    with pytest.raises(RoutingDeferred) as refusal:
        await preflight_item(db_session, item, printer.id)
    assert refusal.value.reason == "source_changed"


async def test_no_auto_row_is_promoted_with_a_source_its_blob_does_not_match(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """The promotion carries the blob AND the snapshot, so the per-printer row reads it."""
    from backend.app.services.auto_queue_add import add_items_to_auto_queue
    from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
    from backend.app.services.filament_intake import item_descriptor

    _unused, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path, name="promoted.gcode.3mf")
    await add_items_to_auto_queue(
        db_session,
        AutoQueueItemCreate(library_file_id=source.id, plate_ids=[PLATE], target_model="P1P"),
        None,
    )

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("backend.app.services.auto_queue_scheduler.async_session", session)
    await AutoQueueScheduler().tick()

    auto = (await db_session.execute(select(AutoQueueItem))).scalars().one()
    item = (await db_session.execute(select(PrintQueueItem))).scalars().one()
    blob = await one_blob(db_session)
    descriptor = await item_descriptor(db_session, item)
    assert (auto.queue_source_id, item.queue_source_id) == (blob.id, blob.id)
    assert descriptor is not None
    assert descriptor.path == object_of(blob)
    assert descriptor.display_filename == source.filename
