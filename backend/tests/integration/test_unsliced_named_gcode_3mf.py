"""A project saved as ``Foo.gcode.3mf`` is not a print source (upstream #3435, G11 U1).

The slicer saves an unsliced project under any name the user types. Named
``Foo.gcode.3mf`` it has no ``Metadata/plate_N.gcode`` inside, and the file
manager — whose ``gcode`` tag has followed the content since m137 — offers Slice
for it, not Print. The server doors behind that button judged the NAME first, so
an API key, Telegram or a stale page could still queue it or send it to a printer.

Every door now asks ``library_helpers.sliced_source_refusal``: content that is
KNOWN (the stored ``has_sliced_gcode`` fact, or a container that opened) decides;
content nobody could read keeps the name rule, exactly as before.
"""

from __future__ import annotations

import hashlib
import time
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import database
from backend.app.models.library import LibraryFile
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.services import queue_sources
from backend.app.services.library_helpers import SLICED_GCODE_META_KEY, sliced_source_refusal
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration

PLATE = 15
NAME = "lamp.gcode.3mf"


@pytest.fixture(autouse=True)
def clean_spool_state():
    queue_sources._reset_state()
    yield
    deadline = time.monotonic() + 10
    while queue_sources.active_captures() and time.monotonic() < deadline:  # pragma: no cover - drain
        time.sleep(0.01)
    queue_sources._reset_state()


@pytest.fixture
def sessions(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    return factory


def a_3mf(path: Path, *, sliced: bool) -> Path:
    return write_routing_3mf(
        path,
        {PLATE: [{"id": 3, "type": "PLA", "color": "#FF0000", "used_g": "1.5"}]},
        model="P1P",
        gcode_plates=None if sliced else [],
    )


async def a_library_row(db, path: Path, *, flag: bool | None) -> LibraryFile:
    """A row named like a sliced file, as ``detect_file_type`` files it."""
    row = LibraryFile(
        filename=path.name,
        file_path=str(path),
        file_size=path.stat().st_size,
        file_type="gcode",
        file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        file_metadata=None if flag is None else {SLICED_GCODE_META_KEY: flag},
    )
    db.add(row)
    await db.commit()
    return row


def _item(queue, row, **extra) -> PrintQueueItemCreate:
    return PrintQueueItemCreate(queue_id=queue.id, library_file_id=row.id, plate_id=PLATE, **extra)


# --------------------------------------------------------------------------- #
# The one function
# --------------------------------------------------------------------------- #


class TestTheOneQuestion:
    async def test_a_recorded_false_refuses_the_sliced_name(self, tmp_path):
        refusal = await sliced_source_refusal(NAME, tmp_path / "gone", file_metadata={SLICED_GCODE_META_KEY: False})
        assert refusal is not None and "Slice it first" in refusal

    async def test_a_readable_project_refuses_the_sliced_name(self, tmp_path):
        assert await sliced_source_refusal(NAME, a_3mf(tmp_path / NAME, sliced=False)) is not None

    async def test_a_really_sliced_file_passes(self, tmp_path):
        assert await sliced_source_refusal(NAME, a_3mf(tmp_path / NAME, sliced=True)) is None
        assert await sliced_source_refusal(NAME, None, file_metadata={SLICED_GCODE_META_KEY: True}) is None

    async def test_raw_gcode_is_never_opened_or_refused(self, tmp_path):
        """A ``.gcode`` is text, not a container: nothing to look inside."""
        raw = tmp_path / "lamp.gcode"
        raw.write_text("G1 X0\n")
        assert await sliced_source_refusal("lamp.gcode", raw) is None
        assert await sliced_source_refusal("lamp.gcode", tmp_path / "gone.gcode") is None
        # A stray fact on a raw G-code row says nothing about it either.
        assert await sliced_source_refusal("lamp.gcode", raw, file_metadata={SLICED_GCODE_META_KEY: False}) is None

    async def test_unknown_content_keeps_the_name_rule(self, tmp_path):
        """Unreadable is not unsliced: the door could not look, so the name decides."""
        junk = tmp_path / NAME
        junk.write_bytes(b"not a zip at all")
        assert await sliced_source_refusal(NAME, junk) is None
        assert await sliced_source_refusal(NAME, tmp_path / "gone.gcode.3mf") is None
        assert await sliced_source_refusal(NAME, None) is None
        # …and a name that does not say sliced is refused when nothing proves it is.
        assert await sliced_source_refusal("model.3mf", junk) is not None

    async def test_a_narrower_name_rule_is_the_callers(self, tmp_path):
        """The dispatcher's own rule: ``x.gcode.zip`` is not its sliced name."""
        assert await sliced_source_refusal("x.gcode.zip", None) is None
        assert await sliced_source_refusal("x.gcode.zip", None, named=False) is not None


# --------------------------------------------------------------------------- #
# queue_add: the two library doors
# --------------------------------------------------------------------------- #


async def test_the_queue_refuses_a_project_named_gcode_3mf(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_items_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    row = await a_library_row(db_session, a_3mf(tmp_path / NAME, sliced=False), flag=False)

    with pytest.raises(HTTPException) as refused:
        await add_items_to_printer_queue(db_session, _item(queue, row), None)

    assert refused.value.status_code == 400
    assert "Slice it first" in str(refused.value.detail)


async def test_the_queue_refuses_it_with_no_recorded_fact_by_looking_inside(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_items_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    row = await a_library_row(db_session, a_3mf(tmp_path / NAME, sliced=False), flag=None)

    with pytest.raises(HTTPException) as refused:
        await add_items_to_printer_queue(db_session, _item(queue, row), None)

    assert refused.value.status_code == 400


async def test_the_queue_still_takes_a_sliced_gcode_3mf(db_session, tmp_path, printer_factory, monkeypatch, sessions):
    from backend.app.services.queue_add import add_items_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    row = await a_library_row(db_session, a_3mf(tmp_path / NAME, sliced=True), flag=None)

    items, _q = await add_items_to_printer_queue(db_session, _item(queue, row), None)

    assert len(items) == 1 and items[0].queue_source_id is not None


async def test_run_next_refuses_a_project_named_gcode_3mf(db_session, tmp_path, printer_factory, monkeypatch, sessions):
    from backend.app.services.queue_add import add_next_block_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    row = await a_library_row(db_session, a_3mf(tmp_path / NAME, sliced=False), flag=False)

    with pytest.raises(HTTPException) as refused:
        await add_next_block_to_printer_queue(db_session, [_item(queue, row, enqueue_position="next")], None)

    assert refused.value.status_code == 400
    assert "Slice it first" in str(refused.value.detail)


async def test_run_next_still_takes_a_sliced_gcode_3mf(db_session, tmp_path, printer_factory, monkeypatch, sessions):
    from backend.app.services.queue_add import add_next_block_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    row = await a_library_row(db_session, a_3mf(tmp_path / NAME, sliced=True), flag=True)

    items, _q = await add_next_block_to_printer_queue(db_session, [_item(queue, row, enqueue_position="next")], None)

    assert len(items) == 1


# --------------------------------------------------------------------------- #
# queue_add: the two Copy Queue doors judge the job's own captured bytes
# --------------------------------------------------------------------------- #


async def _a_queued_job_whose_bytes_are_a_project(db, tmp_path, printer_factory, monkeypatch):
    """A job from before the gate: queued as sliced, its snapshot holds no G-code.

    The gate refuses such a file today, so the job is queued from a sliced one
    and the captured object is then replaced by the project's bytes — what a job
    queued by an older version carries.
    """
    from backend.app.services.filament_intake import item_descriptor
    from backend.app.services.queue_add import add_items_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db, tmp_path, printer_factory, monkeypatch)
    row = await a_library_row(db, a_3mf(tmp_path / NAME, sliced=True), flag=True)
    items, _q = await add_items_to_printer_queue(db, _item(queue, row), None)
    descriptor = await item_descriptor(db, items[0])
    blob = Path(descriptor.path)
    with zipfile.ZipFile(blob, "r") as zf:
        assert any(n.endswith(".gcode") for n in zf.namelist())
    blob.chmod(0o644)
    a_3mf(blob, sliced=False)
    return items[0], queue


async def test_copy_queue_refuses_a_snapshot_that_is_a_project(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_items_to_printer_queue

    item, queue = await _a_queued_job_whose_bytes_are_a_project(db_session, tmp_path, printer_factory, monkeypatch)

    with pytest.raises(HTTPException) as refused:
        await add_items_to_printer_queue(
            db_session, PrintQueueItemCreate(queue_id=queue.id, source_queue_item_id=item.id, plate_id=PLATE), None
        )

    assert refused.value.status_code == 422


async def test_copy_queue_run_next_refuses_it_too(db_session, tmp_path, printer_factory, monkeypatch, sessions):
    from backend.app.services.queue_add import add_next_block_to_printer_queue

    item, queue = await _a_queued_job_whose_bytes_are_a_project(db_session, tmp_path, printer_factory, monkeypatch)

    with pytest.raises(HTTPException) as refused:
        await add_next_block_to_printer_queue(
            db_session,
            [
                PrintQueueItemCreate(
                    queue_id=queue.id, source_queue_item_id=item.id, plate_id=PLATE, enqueue_position="next"
                )
            ],
            None,
        )

    assert refused.value.status_code == 422


async def test_copy_queue_still_takes_a_sliced_gcode_3mf(db_session, tmp_path, printer_factory, monkeypatch, sessions):
    from backend.app.services.queue_add import add_items_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    row = await a_library_row(db_session, a_3mf(tmp_path / NAME, sliced=True), flag=True)
    items, _q = await add_items_to_printer_queue(db_session, _item(queue, row), None)

    copies, _q = await add_items_to_printer_queue(
        db_session, PrintQueueItemCreate(queue_id=queue.id, source_queue_item_id=items[0].id, plate_id=PLATE), None
    )

    assert len(copies) == 1 and copies[0].queue_source_id == items[0].queue_source_id


# --------------------------------------------------------------------------- #
# The library's own Print route
# --------------------------------------------------------------------------- #


async def _print(async_client, row, printer, tmp_path):
    with (
        patch("backend.app.api.routes.library.app_settings.base_dir", tmp_path),
        patch("backend.app.services.printer_manager.printer_manager.is_connected", return_value=True),
        patch(
            "backend.app.services.background_dispatch.background_dispatch.dispatch_print_library_file",
            new=AsyncMock(return_value={"dispatch_job_id": 21, "dispatch_position": 1}),
        ),
    ):
        return await async_client.post(f"/api/v1/library/files/{row.id}/print?printer_id={printer.id}", json={})


async def test_the_print_route_refuses_a_project_named_gcode_3mf(async_client, db_session, tmp_path, printer_factory):
    printer = await printer_factory()
    row = await a_library_row(db_session, a_3mf(tmp_path / NAME, sliced=False), flag=False)

    response = await _print(async_client, row, printer, tmp_path)

    assert response.status_code == 400, response.text
    assert "Slice it first" in response.json()["detail"]


async def test_the_print_route_still_takes_a_sliced_gcode_3mf(async_client, db_session, tmp_path, printer_factory):
    printer = await printer_factory()
    row = await a_library_row(db_session, a_3mf(tmp_path / NAME, sliced=True), flag=True)

    response = await _print(async_client, row, printer, tmp_path)

    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# The dispatcher: the last gate before FTP
# --------------------------------------------------------------------------- #


async def test_the_dispatcher_refuses_a_project_named_gcode_3mf(tmp_path):
    from backend.app.services.background_dispatch import BackgroundDispatchService

    with pytest.raises(RuntimeError, match="Slice it first"):
        await BackgroundDispatchService._require_sliced_source(NAME, a_3mf(tmp_path / NAME, sliced=False))


async def test_the_dispatcher_still_takes_sliced_names(tmp_path):
    from backend.app.services.background_dispatch import BackgroundDispatchService

    raw = tmp_path / "lamp.gcode"
    raw.write_text("G1 X0\n")
    await BackgroundDispatchService._require_sliced_source(NAME, a_3mf(tmp_path / NAME, sliced=True))
    await BackgroundDispatchService._require_sliced_source("lamp.gcode", raw)


# --------------------------------------------------------------------------- #
# An ARCHIVE as the source: Reprint, add to queue, run next (review B-I2)
# --------------------------------------------------------------------------- #


async def an_archive_row(db, printer, path: Path, base: Path):
    """An archive holding ``path`` as its file — a run from before the gate keeps a project."""
    from backend.app.models.archive import PrintArchive

    row = PrintArchive(
        printer_id=printer.id,
        filename=path.name,
        print_name="Lamp",
        file_path=str(path.relative_to(base)),
        file_size=path.stat().st_size,
        status="failed",
    )
    db.add(row)
    await db.commit()
    return row


def _archive_item(queue, archive, **extra) -> PrintQueueItemCreate:
    return PrintQueueItemCreate(queue_id=queue.id, archive_id=archive.id, plate_id=PLATE, **extra)


class TestTheArchiveQuestion:
    async def test_a_known_project_is_refused_whatever_it_is_called(self, tmp_path):
        from backend.app.services.library_helpers import archive_source_refusal

        named = await archive_source_refusal(NAME, a_3mf(tmp_path / NAME, sliced=False))
        plain = await archive_source_refusal("lamp.3mf", a_3mf(tmp_path / "lamp.3mf", sliced=False))
        assert named is not None and "Slice it first" in named
        assert plain is not None and "Not a sliced file" in plain

    async def test_a_sliced_archive_passes_under_any_name(self, tmp_path):
        from backend.app.services.library_helpers import archive_source_refusal

        assert await archive_source_refusal(NAME, a_3mf(tmp_path / NAME, sliced=True)) is None
        # An external print is archived as ``Foo.3mf`` with its G-code inside.
        assert await archive_source_refusal("lamp.3mf", a_3mf(tmp_path / "lamp.3mf", sliced=True)) is None

    async def test_content_nobody_could_read_refuses_nothing(self, tmp_path):
        """An archive never had a name rule: unknown content is sent as it always was."""
        from backend.app.services.library_helpers import archive_source_refusal

        junk = tmp_path / "lamp.3mf"
        junk.write_bytes(b"not a zip at all")
        raw = tmp_path / "lamp.gcode"
        raw.write_text("G1 X0\n")
        assert await archive_source_refusal("lamp.3mf", junk) is None
        assert await archive_source_refusal("lamp.3mf", tmp_path / "gone.3mf") is None
        assert await archive_source_refusal("lamp.3mf", None) is None
        assert await archive_source_refusal("lamp.gcode", raw) is None


async def test_the_queue_refuses_an_archive_that_holds_a_project(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_items_to_printer_queue

    _src, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    monkeypatch.setattr("backend.app.services.filament_intake.settings.base_dir", tmp_path)
    archive = await an_archive_row(db_session, printer, a_3mf(tmp_path / NAME, sliced=False), tmp_path)

    # The capture's own plate check closes this door: the queue reads the plate it is asked for.
    with pytest.raises(HTTPException) as refused:
        await add_items_to_printer_queue(db_session, _archive_item(queue, archive), None)

    assert refused.value.status_code == 422
    assert refused.value.detail["code"] == "plate_gcode_missing"


async def test_the_queue_still_takes_a_sliced_archive(db_session, tmp_path, printer_factory, monkeypatch, sessions):
    from backend.app.services.queue_add import add_items_to_printer_queue

    _src, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    monkeypatch.setattr("backend.app.services.filament_intake.settings.base_dir", tmp_path)
    archive = await an_archive_row(db_session, printer, a_3mf(tmp_path / NAME, sliced=True), tmp_path)

    items, _q = await add_items_to_printer_queue(db_session, _archive_item(queue, archive), None)

    assert len(items) == 1 and items[0].queue_source_id is not None


async def test_run_next_refuses_an_archive_that_holds_a_project(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_next_block_to_printer_queue

    _src, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    monkeypatch.setattr("backend.app.services.filament_intake.settings.base_dir", tmp_path)
    archive = await an_archive_row(db_session, printer, a_3mf(tmp_path / NAME, sliced=False), tmp_path)

    with pytest.raises(HTTPException) as refused:
        await add_next_block_to_printer_queue(
            db_session, [_archive_item(queue, archive, enqueue_position="next")], None
        )

    assert refused.value.status_code == 422
    assert refused.value.detail["code"] == "plate_gcode_missing"


async def test_run_next_still_takes_a_sliced_archive(db_session, tmp_path, printer_factory, monkeypatch, sessions):
    from backend.app.services.queue_add import add_next_block_to_printer_queue

    _src, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    monkeypatch.setattr("backend.app.services.filament_intake.settings.base_dir", tmp_path)
    archive = await an_archive_row(db_session, printer, a_3mf(tmp_path / NAME, sliced=True), tmp_path)

    items, _q = await add_next_block_to_printer_queue(
        db_session, [_archive_item(queue, archive, enqueue_position="next")], None
    )

    assert len(items) == 1


async def _reprint(async_client, archive, printer, tmp_path):
    with (
        patch("backend.app.api.routes.archives.settings.base_dir", tmp_path),
        patch("backend.app.services.filament_intake.settings.base_dir", tmp_path),
        patch("backend.app.services.printer_manager.printer_manager.is_connected", return_value=True),
        patch(
            "backend.app.services.background_dispatch.background_dispatch.dispatch_reprint_archive",
            new=AsyncMock(return_value={"dispatch_job_id": 22, "dispatch_position": 1}),
        ),
    ):
        return await async_client.post(f"/api/v1/archives/{archive.id}/reprint?printer_id={printer.id}", json={})


async def test_reprint_refuses_an_archive_that_holds_a_project(async_client, db_session, tmp_path, printer_factory):
    printer = await printer_factory()
    archive = await an_archive_row(db_session, printer, a_3mf(tmp_path / NAME, sliced=False), tmp_path)

    response = await _reprint(async_client, archive, printer, tmp_path)

    assert response.status_code == 400, response.text
    assert "Slice it first" in response.json()["detail"]


async def test_reprint_still_sends_a_sliced_archive(async_client, db_session, tmp_path, printer_factory):
    printer = await printer_factory()
    archive = await an_archive_row(db_session, printer, a_3mf(tmp_path / NAME, sliced=True), tmp_path)

    response = await _reprint(async_client, archive, printer, tmp_path)

    assert response.status_code == 200, response.text


async def test_the_archive_runner_refuses_a_project_as_its_last_gate(tmp_path):
    from backend.app.services.background_dispatch import BackgroundDispatchService

    with pytest.raises(RuntimeError, match="Slice it first"):
        await BackgroundDispatchService._require_sliced_archive_source(NAME, a_3mf(tmp_path / NAME, sliced=False))


async def test_the_archive_runner_sends_sliced_and_unreadable_bytes_as_before(tmp_path):
    from backend.app.services.background_dispatch import BackgroundDispatchService

    await BackgroundDispatchService._require_sliced_archive_source(
        "lamp.3mf", a_3mf(tmp_path / "lamp.3mf", sliced=True)
    )
    await BackgroundDispatchService._require_sliced_archive_source("lamp.3mf", tmp_path / "gone.3mf")


def test_the_archive_runner_asks_its_gate():
    """The runner is too heavy to drive here: pin that its body reaches the gate before it patches or uploads."""
    import inspect

    from backend.app.services.background_dispatch import BackgroundDispatchService

    body = inspect.getsource(BackgroundDispatchService._run_reprint_archive)
    assert "_require_sliced_archive_source(" in body
    assert body.index("_require_sliced_archive_source(") < body.index("apply_3mf_transforms")


# --------------------------------------------------------------------------- #
# A recorded "no" is as old as the last scan of a share (review B-M1)
# --------------------------------------------------------------------------- #


class TestAStaleNoOnAShare:
    async def test_a_file_sliced_in_place_since_the_scan_passes(self, tmp_path):
        path = a_3mf(tmp_path / NAME, sliced=True)
        stale = {SLICED_GCODE_META_KEY: False}
        assert await sliced_source_refusal(NAME, path, file_metadata=stale, may_have_changed=True) is None

    async def test_a_file_that_is_still_a_project_is_refused_on_the_fresh_read(self, tmp_path):
        path = a_3mf(tmp_path / NAME, sliced=False)
        stale = {SLICED_GCODE_META_KEY: False}
        assert await sliced_source_refusal(NAME, path, file_metadata=stale, may_have_changed=True) is not None

    async def test_a_file_nobody_can_read_now_keeps_the_recorded_no(self, tmp_path):
        stale = {SLICED_GCODE_META_KEY: False}
        gone = tmp_path / "gone.gcode.3mf"
        assert await sliced_source_refusal(NAME, gone, file_metadata=stale, may_have_changed=True) is not None

    async def test_a_managed_file_s_recorded_no_is_believed_without_a_read(self, tmp_path, monkeypatch):
        from backend.app.services import library_helpers

        def opened(_path):
            raise AssertionError("the container was opened")

        monkeypatch.setattr(library_helpers, "sliced_gcode_in_3mf", opened)
        stale = {SLICED_GCODE_META_KEY: False}
        assert await sliced_source_refusal(NAME, tmp_path / NAME, file_metadata=stale) is not None

    async def test_a_recorded_yes_is_not_read_again(self, tmp_path, monkeypatch):
        from backend.app.services import library_helpers

        def opened(_path):
            raise AssertionError("the container was opened")

        monkeypatch.setattr(library_helpers, "sliced_gcode_in_3mf", opened)
        known = {SLICED_GCODE_META_KEY: True}
        assert await sliced_source_refusal(NAME, tmp_path / NAME, file_metadata=known, may_have_changed=True) is None


async def _an_external_row_scanned_as_a_project(db, path: Path) -> LibraryFile:
    row = await a_library_row(db, path, flag=False)
    row.is_external = True
    await db.commit()
    return row


async def test_the_queue_takes_an_external_file_sliced_in_place_since_its_scan(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_items_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    row = await _an_external_row_scanned_as_a_project(db_session, a_3mf(tmp_path / NAME, sliced=True))

    items, _q = await add_items_to_printer_queue(db_session, _item(queue, row), None)

    assert len(items) == 1


async def test_run_next_takes_an_external_file_sliced_in_place_since_its_scan(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_next_block_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    row = await _an_external_row_scanned_as_a_project(db_session, a_3mf(tmp_path / NAME, sliced=True))

    items, _q = await add_next_block_to_printer_queue(db_session, [_item(queue, row, enqueue_position="next")], None)

    assert len(items) == 1


async def test_the_print_route_takes_an_external_file_sliced_in_place_since_its_scan(
    async_client, db_session, tmp_path, printer_factory
):
    printer = await printer_factory()
    row = await _an_external_row_scanned_as_a_project(db_session, a_3mf(tmp_path / NAME, sliced=True))

    response = await _print(async_client, row, printer, tmp_path)

    assert response.status_code == 200, response.text


async def test_an_external_file_that_is_still_a_project_stays_refused(
    async_client, db_session, tmp_path, printer_factory
):
    printer = await printer_factory()
    row = await _an_external_row_scanned_as_a_project(db_session, a_3mf(tmp_path / NAME, sliced=False))

    response = await _print(async_client, row, printer, tmp_path)

    assert response.status_code == 400, response.text
