"""A sliced 3MF named ``Foo.3mf`` prints like one named ``Foo.gcode.3mf`` (upstream #2993).

Upstream 2e405afc. The name is not evidence: a plate exported from the slicer or
a print that went through the cloud reaches the printer — and so the archive,
and so the library — as ``Foo.3mf`` with its G-code intact. Our library has
judged the container by its contents since m137 (``has_sliced_gcode`` in
``file_metadata``, and the ``gcode`` tag the file manager's Print button reads),
but every backend gate between that button and the printer still asked the
NAME: the library print route, both queue writers, the Copy Queue path and the
dispatcher. So the button was offered and the click was refused with "Not a
sliced file".

Every gate now takes the same content arm beside its own filename rule: a
``.3mf`` whose stored flag says so, or — when no flag was ever written — whose
ZIP central directory holds ``Metadata/*.gcode``. The gates only WIDEN: a name
that was accepted before is accepted still.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import database
from backend.app.models.library import LibraryFile
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.services import queue_sources
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration

PLATE = 15


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
    """A 3MF that holds plate G-code, or the same container without it."""
    return write_routing_3mf(
        path,
        {PLATE: [{"id": 3, "type": "PLA", "color": "#FF0000", "used_g": "1.5"}]},
        model="P1P",
        gcode_plates=None if sliced else [],
    )


async def a_library_row(db, path: Path, *, flag: bool | None) -> LibraryFile:
    """A library row named like a source project, as ``detect_file_type`` files it."""
    row = LibraryFile(
        filename=path.name,
        file_path=str(path),
        file_size=path.stat().st_size,
        file_type="3mf",
        file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        file_metadata=None if flag is None else {"has_sliced_gcode": flag},
    )
    db.add(row)
    await db.commit()
    return row


# --------------------------------------------------------------------------- #
# The per-printer queue writer (``add_items_to_printer_queue``)
# --------------------------------------------------------------------------- #


async def test_the_queue_takes_a_sliced_3mf_named_as_source(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_items_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    row = await a_library_row(db_session, a_3mf(tmp_path / "lamp.3mf", sliced=True), flag=True)

    items, _q = await add_items_to_printer_queue(
        db_session, PrintQueueItemCreate(queue_id=queue.id, library_file_id=row.id, plate_id=PLATE), None
    )

    assert len(items) == 1 and items[0].queue_source_id is not None


async def test_a_row_that_never_had_the_flag_is_judged_by_its_zip(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """An external-folder row written before the scan recorded the flag."""
    from backend.app.services.queue_add import add_items_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    row = await a_library_row(db_session, a_3mf(tmp_path / "lamp.3mf", sliced=True), flag=None)

    items, _q = await add_items_to_printer_queue(
        db_session, PrintQueueItemCreate(queue_id=queue.id, library_file_id=row.id, plate_id=PLATE), None
    )

    assert len(items) == 1


async def test_a_3mf_without_gcode_is_still_refused(db_session, tmp_path, printer_factory, monkeypatch, sessions):
    from backend.app.services.queue_add import add_items_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    row = await a_library_row(db_session, a_3mf(tmp_path / "model.3mf", sliced=False), flag=None)

    with pytest.raises(HTTPException) as refused:
        await add_items_to_printer_queue(
            db_session, PrintQueueItemCreate(queue_id=queue.id, library_file_id=row.id, plate_id=PLATE), None
        )

    assert refused.value.status_code == 400
    assert "Not a sliced file" in str(refused.value.detail)


async def test_a_stored_false_is_believed_without_opening_the_file(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """The flag is the answer when there is one — the zip is opened only when
    nothing was ever recorded. Here the file on disk would say yes; the row says
    no, and the row was written from these bytes."""
    from backend.app.services.queue_add import add_items_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    row = await a_library_row(db_session, a_3mf(tmp_path / "lamp.3mf", sliced=True), flag=False)

    with pytest.raises(HTTPException) as refused:
        await add_items_to_printer_queue(
            db_session, PrintQueueItemCreate(queue_id=queue.id, library_file_id=row.id, plate_id=PLATE), None
        )

    assert refused.value.status_code == 400


async def test_run_next_takes_a_sliced_3mf_named_as_source(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_next_block_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    row = await a_library_row(db_session, a_3mf(tmp_path / "lamp.3mf", sliced=True), flag=True)

    items, _q = await add_next_block_to_printer_queue(
        db_session,
        [PrintQueueItemCreate(queue_id=queue.id, library_file_id=row.id, plate_id=PLATE, enqueue_position="next")],
        None,
    )

    assert len(items) == 1


# --------------------------------------------------------------------------- #
# Copy Queue: the job's own snapshot, named ``lamp.3mf``
# --------------------------------------------------------------------------- #


async def _a_queued_sliced_3mf(db, tmp_path, printer_factory, monkeypatch):
    from backend.app.services.queue_add import add_items_to_printer_queue

    _src, _printer, queue, _mqtt = await setup_source(db, tmp_path, printer_factory, monkeypatch)
    row = await a_library_row(db, a_3mf(tmp_path / "lamp.3mf", sliced=True), flag=True)
    items, _q = await add_items_to_printer_queue(
        db, PrintQueueItemCreate(queue_id=queue.id, library_file_id=row.id, plate_id=PLATE), None
    )
    return items[0], queue


async def test_copy_queue_takes_the_snapshot_of_a_sliced_3mf_named_as_source(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_items_to_printer_queue

    item, queue = await _a_queued_sliced_3mf(db_session, tmp_path, printer_factory, monkeypatch)

    copies, _q = await add_items_to_printer_queue(
        db_session, PrintQueueItemCreate(queue_id=queue.id, source_queue_item_id=item.id, plate_id=PLATE), None
    )

    assert len(copies) == 1 and copies[0].queue_source_id == item.queue_source_id


async def test_copy_queue_run_next_takes_it_too(db_session, tmp_path, printer_factory, monkeypatch, sessions):
    from backend.app.services.queue_add import add_next_block_to_printer_queue

    item, queue = await _a_queued_sliced_3mf(db_session, tmp_path, printer_factory, monkeypatch)

    copies, _q = await add_next_block_to_printer_queue(
        db_session,
        [
            PrintQueueItemCreate(
                queue_id=queue.id, source_queue_item_id=item.id, plate_id=PLATE, enqueue_position="next"
            )
        ],
        None,
    )

    assert len(copies) == 1


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


async def test_the_print_route_takes_a_sliced_3mf_named_as_source(async_client, db_session, tmp_path, printer_factory):
    printer = await printer_factory()
    row = await a_library_row(db_session, a_3mf(tmp_path / "lamp.3mf", sliced=True), flag=True)

    response = await _print(async_client, row, printer, tmp_path)

    assert response.status_code == 200, response.text


async def test_the_print_route_still_refuses_a_source_project(async_client, db_session, tmp_path, printer_factory):
    printer = await printer_factory()
    row = await a_library_row(db_session, a_3mf(tmp_path / "model.3mf", sliced=False), flag=False)

    response = await _print(async_client, row, printer, tmp_path)

    assert response.status_code == 400
    assert "Not a sliced file" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# The dispatcher: the last gate before FTP
# --------------------------------------------------------------------------- #


async def test_the_dispatcher_takes_a_sliced_3mf_named_as_source(tmp_path):
    from backend.app.services.background_dispatch import BackgroundDispatchService

    await BackgroundDispatchService._require_sliced_source("lamp.3mf", a_3mf(tmp_path / "lamp.3mf", sliced=True))


async def test_the_dispatcher_refuses_a_source_project(tmp_path):
    from backend.app.services.background_dispatch import BackgroundDispatchService

    with pytest.raises(RuntimeError, match="Not a sliced file"):
        await BackgroundDispatchService._require_sliced_source("model.3mf", a_3mf(tmp_path / "model.3mf", sliced=False))


async def test_the_dispatcher_does_not_open_a_file_its_name_already_settles(tmp_path):
    """``.gcode.3mf`` and ``.gcode`` pass on the name, as they always did."""
    from backend.app.services.background_dispatch import BackgroundDispatchService

    await BackgroundDispatchService._require_sliced_source("lamp.gcode.3mf", tmp_path / "not-there.gcode.3mf")
    await BackgroundDispatchService._require_sliced_source("lamp.gcode", tmp_path / "not-there.gcode")


def test_the_library_runner_asks_the_gate():
    """The runner cannot be driven without a printer and an FTP server; the
    gate's behaviour is pinned above, this pins that the runner asks it."""
    import inspect

    from backend.app.services import background_dispatch as module

    source = inspect.getsource(module.BackgroundDispatchService._run_print_library_file)
    assert "_require_sliced_source(" in source
    assert "_is_sliced_file(library_filename)" not in source
