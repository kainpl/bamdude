"""A queued job's PICTURE comes out of the bytes it owns — spec §4 / A09.

Task 7 delivered the other half of A09 (the estimate, the weight, the build plate
and the name) and measured why the picture could not follow from the backend
alone: the frontend builds a row's image only from ``archive_id`` /
``library_file_id``, one site *filters out* a row with neither, and the
``*_thumbnail`` response fields are server disk paths rather than URLs. §4 is
explicit that the thumbnail is recoverable from the stored 3MF and that **the UI
must not require the original's**, so the three pieces have to arrive together: a
route that serves the snapshot's plate render, a flag that says a row HAS one,
and the two frontend sites preferring it.

Everything here goes through the real API. The original file is deleted **and
trapped** (Task 6's seven-door ``forbid_reads``), so a reader that answered from
the original instead of the snapshot names itself rather than passing quietly —
and a job whose ``library_file_id`` and ``archive_id`` are both NULL is the case
every assertion is about.
"""

from __future__ import annotations

import hashlib
import time
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import database
from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.queue_source import STATE_BROKEN, QueueSource
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.services import monitor_snapshot, queue_sources, queue_times
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.integration.test_dispatch_without_original import forbid_reads
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration

PLATE = 15
OTHER_PLATE = 4
PLATE_PNG = b"\x89PNG\r\n\x1a\nthe plate fifteen render"
OTHER_PNG = b"\x89PNG\r\n\x1a\nthe plate four render"
WRONG_PLATE_PNG = b"\x89PNG\r\n\x1a\nplate one, a DIFFERENT plate"


@pytest.fixture(autouse=True)
def clean_spool_state():
    """Process-global capture state must not travel between tests."""
    queue_sources._reset_state()
    yield
    deadline = time.monotonic() + 10
    while queue_sources.active_captures() and time.monotonic() < deadline:  # pragma: no cover - drain
        time.sleep(0.01)
    queue_sources._reset_state()


@pytest.fixture(autouse=True)
def clean_picture_cache():
    """Both caches are module-level; a hit from another test hides a read."""
    queue_times._PLATE_PICTURE_CACHE.clear()
    queue_times._PLATE_META_CACHE.clear()
    yield
    queue_times._PLATE_PICTURE_CACHE.clear()
    queue_times._PLATE_META_CACHE.clear()


@pytest.fixture
def sessions(test_engine, monkeypatch):
    """``queue_sources.publish`` owns its transaction, so it needs this engine."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    return factory


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def a_sliced_file(path: Path, *, pictures: dict[int, bytes] | None = None) -> Path:
    """A two-plate sliced 3MF; ``pictures`` decides which plates are rendered."""
    return write_routing_3mf(
        path,
        {
            PLATE: [{"id": 3, "type": "PLA", "color": "#FF0000", "used_g": "12.5"}],
            OTHER_PLATE: [{"id": 3, "type": "PLA", "color": "#FF0000", "used_g": "3.0"}],
        },
        model="P1P",
        plate_pngs=pictures if pictures is not None else {PLATE: PLATE_PNG, OTHER_PLATE: OTHER_PNG},
    )


async def a_library_source(db, tmp_path, *, name="lamp.gcode.3mf", pictures=None) -> LibraryFile:
    path = a_sliced_file(tmp_path / name, pictures=pictures)
    row = LibraryFile(
        filename=path.name,
        file_path=str(path),
        file_size=path.stat().st_size,
        file_type="gcode",
        file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        # A thumbnail of its OWN on disk, so "the snapshot's picture outranks the
        # original's" is a question with two different answers rather than one.
        thumbnail_path=str(_a_thumbnail_on_disk(tmp_path / f"{name}.png")),
    )
    db.add(row)
    await db.commit()
    return row


async def an_archive_source(db, tmp_path, printer_id: int, *, name="shelf.gcode.3mf") -> PrintArchive:
    folder = settings.archive_dir / "picture-source"
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
        thumbnail_path=str(_a_thumbnail_on_disk(folder / f"{name}.png").relative_to(settings.base_dir)),
    )
    db.add(row)
    await db.commit()
    return row


def _a_thumbnail_on_disk(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\nthe ORIGINAL row's own thumbnail")
    return path


async def one_blob(db) -> QueueSource:
    return (await db.execute(select(QueueSource).order_by(QueueSource.id))).scalars().one()


async def lose_the_original(db, item, original: Path, *, row) -> None:
    item.archive_id = None
    item.library_file_id = None
    await db.delete(row)
    await db.commit()
    original.unlink()


async def a_queued_job(db, queue, source, *, plate=PLATE, user=None):
    items, _queue = await add_one(db, queue, source, plate=plate, user=user)
    return items[0]


async def add_one(db, queue, source, *, plate=PLATE, user=None):
    from backend.app.services.queue_add import add_items_to_printer_queue

    kwargs = {"archive_id": source.id} if isinstance(source, PrintArchive) else {"library_file_id": source.id}
    return await add_items_to_printer_queue(db, PrintQueueItemCreate(queue_id=queue.id, plate_id=plate, **kwargs), user)


async def queue_rows(client) -> list[dict]:
    response = await client.get("/api/v1/queue/")
    assert response.status_code == 200, response.text
    return response.json()


async def row_for(client, item_id: int) -> dict:
    rows = [row for row in await queue_rows(client) if row["id"] == item_id]
    assert rows, f"queue item {item_id} is not in the list"
    return rows[0]


async def picture(client, item_id: int):
    return await client.get(f"/api/v1/queue/{item_id}/source-thumbnail")


# --------------------------------------------------------------------------- #
# The decisive case: no original rows at all
# --------------------------------------------------------------------------- #


async def test_a_job_with_neither_original_row_still_has_its_picture(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """A09, and the whole point of the task: both ids NULL, the picture is served."""
    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    original = Path(source.file_path)
    item = await a_queued_job(db_session, queue, source)
    await lose_the_original(db_session, item, original, row=source)
    touched = forbid_reads(monkeypatch, original)

    row = await row_for(async_client, item.id)
    assert row["archive_id"] is None and row["library_file_id"] is None
    assert row["source_thumbnail"] is True
    assert row["archive_thumbnail"] is None and row["library_file_thumbnail"] is None

    response = await picture(async_client, item.id)
    assert response.status_code == 200, response.text
    assert response.content == PLATE_PNG
    assert response.headers["content-type"] == "image/png"
    assert touched == []


async def test_the_snapshot_picture_outranks_the_originals_own_thumbnail(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """A03: the job keeps the bytes it accepted, so it keeps THEIR picture.

    The library row is still here and still has a thumbnail of its own, and the
    file behind that row has been re-sliced since the job accepted its bytes — same
    name, same plate, a different render. The route must serve the frozen copy's.

    No read trap here, deliberately: with the library row present, ``_enrich_response``
    legitimately stats the original for its own per-plate branch (the snapshot is
    applied on top of that, not instead of it), so a trap would fire on
    pre-existing, correct behaviour. What proves the ranking is that the two files
    hold *different* bytes under the same plate.
    """
    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    item = await a_queued_job(db_session, queue, source)
    resliced = b"\x89PNG\r\n\x1a\nRE-SLICED SINCE THE JOB WAS QUEUED"
    a_sliced_file(Path(source.file_path), pictures={PLATE: resliced})

    row = await row_for(async_client, item.id)
    assert row["library_file_id"] == source.id
    assert row["library_file_thumbnail"] == source.thumbnail_path
    assert row["source_thumbnail"] is True

    response = await picture(async_client, item.id)
    assert response.status_code == 200, response.text
    assert response.content == PLATE_PNG
    assert response.content != resliced


async def test_a_reprint_whose_archive_is_gone_still_has_its_picture(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    archive = await an_archive_source(db_session, tmp_path, printer.id)
    original = settings.base_dir / archive.file_path
    item = await a_queued_job(db_session, queue, archive)
    await lose_the_original(db_session, item, original, row=archive)
    touched = forbid_reads(monkeypatch, original)

    assert (await row_for(async_client, item.id))["source_thumbnail"] is True
    response = await picture(async_client, item.id)
    assert response.status_code == 200 and response.content == PLATE_PNG
    assert touched == []


# --------------------------------------------------------------------------- #
# The plate is the question
# --------------------------------------------------------------------------- #


async def test_the_jobs_own_plate_decides_which_render_is_served(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """Two jobs, one blob, two plates — and two different pictures."""
    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    fifteen = await a_queued_job(db_session, queue, source, plate=PLATE)
    four = await a_queued_job(db_session, queue, source, plate=OTHER_PLATE)
    await db_session.commit()

    assert (await picture(async_client, fifteen.id)).content == PLATE_PNG
    assert (await picture(async_client, four.id)).content == OTHER_PNG


async def test_the_archive_plate_fallback_answers_a_job_that_names_no_plate(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """``plate_id`` NULL on an archive-sourced job: the archive's plate is the
    fallback, exactly as every other snapshot reader resolves it."""
    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    archive = await an_archive_source(db_session, tmp_path, printer.id)
    item = await a_queued_job(db_session, queue, archive, plate=None)
    original = settings.base_dir / archive.file_path
    await lose_the_original(db_session, item, original, row=archive)
    touched = forbid_reads(monkeypatch, original)

    assert (await row_for(async_client, item.id))["source_thumbnail"] is True
    assert (await picture(async_client, item.id)).content == PLATE_PNG
    assert touched == []


async def test_a_plate_the_file_does_not_render_is_the_honest_empty_state(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """Never another plate's picture: plate 15 is rendered, plate 4 is not.

    The file also carries a ``plate_1.png``, which is what the legacy
    ``archives.get_plate_preview`` chain would have fallen back to — so a reader
    that kept that fallback would answer 200 with a picture of the wrong parts.
    """
    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path, pictures={1: WRONG_PLATE_PNG, PLATE: PLATE_PNG})
    item = await a_queued_job(db_session, queue, source, plate=OTHER_PLATE)
    await db_session.commit()

    assert (await row_for(async_client, item.id))["source_thumbnail"] is False
    assert (await picture(async_client, item.id)).status_code == 404


# --------------------------------------------------------------------------- #
# A missing picture is not an error (decision 2)
# --------------------------------------------------------------------------- #


async def test_a_snapshot_with_no_render_at_all_says_so(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path, pictures={})
    item = await a_queued_job(db_session, queue, source)
    await db_session.commit()

    assert (await row_for(async_client, item.id))["source_thumbnail"] is False
    assert (await picture(async_client, item.id)).status_code == 404


async def test_a_broken_blob_shows_nothing_rather_than_a_broken_image(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """§7: a job that names a blob is answered from that blob whatever state it is
    in — and for a picture "no bytes" is simply no picture."""
    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    item = await a_queued_job(db_session, queue, source)
    blob = await one_blob(db_session)
    blob.state = STATE_BROKEN
    await db_session.commit()
    (settings.base_dir / blob.relative_path).unlink()

    row = await row_for(async_client, item.id)
    assert row["source_storage"] == "broken"
    assert row["source_thumbnail"] is False
    assert (await picture(async_client, item.id)).status_code == 404


async def test_a_legacy_row_is_described_exactly_as_before(
    async_client, db_session, tmp_path, printer_factory, monkeypatch
):
    """No snapshot: the flag is False and the picture still comes from the rows."""
    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    item = PrintQueueItem(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE, position=1, status="pending")
    db_session.add(item)
    await db_session.commit()

    row = await row_for(async_client, item.id)
    assert row["source_storage"] == "legacy"
    assert row["source_thumbnail"] is False
    assert row["library_file_thumbnail"] == source.thumbnail_path
    assert (await picture(async_client, item.id)).status_code == 404


async def test_an_unknown_item_is_a_plain_not_found(async_client):
    assert (await picture(async_client, 987654)).status_code == 404


# --------------------------------------------------------------------------- #
# Cost: the list must not open a ZIP per row per poll (decision 1)
# --------------------------------------------------------------------------- #


async def test_the_flag_costs_one_namelist_read_per_object_and_plate(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """Three rows on one blob, two polls: four opens in all, none on the second.

    Three of them are the metadata parsers Task 7 already counted; the fourth is
    this flag. The picture itself is never read by the list — it is served by a
    route the list does not call, once per rendered row, and cached by the browser
    under the object's own ETag.
    """
    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    items, _queue = await add_items_to_printer_queue_thrice(db_session, queue, source)
    await lose_the_original(db_session, items[0], Path(source.file_path), row=source)
    for extra in items[1:]:
        extra.archive_id, extra.library_file_id = None, None
    await db_session.commit()

    opened: list[str] = []
    real = zipfile.ZipFile

    def counting(file, *args, **kwargs):
        opened.append(str(file))
        return real(file, *args, **kwargs)

    monkeypatch.setattr(queue_times.zipfile, "ZipFile", counting)
    monkeypatch.setattr("backend.app.utils.threemf_tools.zipfile.ZipFile", counting)

    first = await queue_rows(async_client)
    second = await queue_rows(async_client)
    ids = {item.id for item in items}
    assert [row["source_thumbnail"] for row in first if row["id"] in ids] == [True] * 3
    assert [row["source_thumbnail"] for row in second if row["id"] in ids] == [True] * 3
    assert len(opened) == 4, opened


async def add_items_to_printer_queue_thrice(db, queue, source):
    from backend.app.services.queue_add import add_items_to_printer_queue

    return await add_items_to_printer_queue(
        db,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE, quantity=3),
        None,
    )


async def test_the_route_pins_the_blob_while_it_reads_the_bytes(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """§9: the collector must not be able to unlink the object mid-read."""
    from backend.app.api.routes import print_queue as print_queue_routes

    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    item = await a_queued_job(db_session, queue, source)
    blob = await one_blob(db_session)
    await db_session.commit()

    seen: list[frozenset[int]] = []
    real = print_queue_routes._read_plate_picture

    def watching(path, plate):
        seen.append(queue_sources.pinned_source_ids())
        return real(path, plate)

    monkeypatch.setattr(print_queue_routes, "_read_plate_picture", watching)
    assert (await picture(async_client, item.id)).status_code == 200
    assert seen == [frozenset({blob.id})]
    # And released afterwards, or the GC could never collect the blob again.
    assert queue_sources.pinned_source_ids() == frozenset()


# --------------------------------------------------------------------------- #
# A picture is content: whoever cannot see the row cannot see its picture
# --------------------------------------------------------------------------- #


async def test_a_reader_scoped_to_their_own_rows_cannot_see_another_persons_picture(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """The same ``queue:read_all`` / ``queue:read_own`` split ``GET /queue/`` uses,
    answered 404 rather than 403 so an id is not confirmed by its refusal."""
    mine_token, mine_id = await a_user(async_client, "pic_mine", ["queue:read_own"])
    _other_token, other_id = await a_user(async_client, "pic_other", ["queue:read_own"])
    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    mine = await a_queued_job(db_session, queue, source)
    theirs = await a_queued_job(db_session, queue, source)
    mine.created_by_id, theirs.created_by_id = mine_id, other_id
    await db_session.commit()

    headers = {"Authorization": f"Bearer {mine_token}"}
    assert (await async_client.get(f"/api/v1/queue/{mine.id}/source-thumbnail", headers=headers)).status_code == 200
    assert (await async_client.get(f"/api/v1/queue/{theirs.id}/source-thumbnail", headers=headers)).status_code == 404


async def test_an_ownerless_row_needs_read_all(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """Fail-closed, as every other ownership-scoped read in this codebase."""
    token, _uid = await a_user(async_client, "pic_own_only", ["queue:read_own"])
    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    item = await a_queued_job(db_session, queue, source)
    assert item.created_by_id is None
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}
    assert (await async_client.get(f"/api/v1/queue/{item.id}/source-thumbnail", headers=headers)).status_code == 404
    # The admin client holds read_all and sees it.
    assert (await picture(async_client, item.id)).status_code == 200


async def test_no_token_no_picture(async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions):
    """Unlike the legacy archive/library thumbnail routes, this one is not public."""
    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    item = await a_queued_job(db_session, queue, source)
    await db_session.commit()

    response = await async_client.get(f"/api/v1/queue/{item.id}/source-thumbnail", headers={"Authorization": ""})
    assert response.status_code == 401, response.text


async def a_user(client, username: str, permissions: list[str]) -> tuple[str, int]:
    password = "Aa1!queue-picture-test"  # pragma: allowlist secret
    group = await client.post("/api/v1/groups/", json={"name": f"qp_{username}", "permissions": permissions})
    assert group.status_code == 201, group.text
    user = await client.post(
        "/api/v1/users/",
        json={"username": username, "password": password, "role": "user", "group_ids": [group.json()["id"]]},
    )
    assert user.status_code == 201, user.text
    login = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return login.json()["access_token"], user.json()["id"]


# --------------------------------------------------------------------------- #
# The monitor wall names the same job
# --------------------------------------------------------------------------- #


async def test_the_monitor_names_a_queue_head_whose_original_rows_are_gone(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """``monitor_snapshot`` built the head's name from the original rows only, so
    an independent job showed as a nameless tile on the wall."""
    _unused, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    item = await a_queued_job(db_session, queue, source)
    await lose_the_original(db_session, item, Path(source.file_path), row=source)
    touched = forbid_reads(monkeypatch, Path(source.file_path))

    snapshot = await monitor_snapshot.build_snapshot(
        db_session, "queues", monitor_snapshot.MonitorAccess(queue_read=True, read_all=True)
    )
    tile = next(entry for entry in snapshot.printers if entry.printer_id == printer.id)
    assert tile.queue is not None and tile.queue.next_job is not None
    assert tile.queue.next_job.name == "lamp.gcode.3mf"
    assert touched == []
