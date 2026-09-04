"""Library 3MFs that never got their object metadata get it — and nothing else.

⚠️ These pin the four ways this sweep can go wrong quietly.

* A file whose mount is down must be *skipped*, not emptied — the row still
  describes real objects, we simply cannot see them today.
* A file that has already been ASKED must not be asked again, or every boot
  re-opens the library; and the marker is the ``printable_objects`` KEY, never a
  non-empty value, because an empty map is an answer.
* The marker must not be ``has_sliced_gcode``: objects also come from
  ``slice_info.config``, which a saved project carries without any packed
  g-code — exactly the files this feature exists for.
* A file bound to a product must go back through ``sync_product_for_file``: the
  objects are only half the fix — without the sync the product still has no
  parts and the plan still counts nothing.
"""

from __future__ import annotations

import asyncio
import io
import json
import threading
import zipfile
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductPart, ProductPlate, product_files
from backend.app.services import library_objects_backfill as backfill
from backend.app.services.library_objects_backfill import (
    BackfillSummary,
    backfill_library_objects,
    files_missing_objects,
)


def sliced_3mf(
    objects_by_plate: dict[int, list[str]],
    *,
    marker: bytes = b"",
    pack_gcode: bool = True,
    settings: dict | None = None,
) -> bytes:
    """A real sliced 3MF — the same shape ``test_product_export_import`` builds:
    ``plate_N.gcode`` for plate discovery, ``slice_info`` for the object names.

    ``pack_gcode=False`` is a **saved project** rather than an exported sliced
    file: Bambu Studio wrote the slice_info but packed no g-code.
    """
    identify = 100
    plates = []
    for index, names in sorted(objects_by_plate.items()):
        objects = ""
        for name in names:
            identify += 1
            objects += f'<object identify_id="{identify}" name="{name}" skipped="false" />'
        plates.append(
            f'<plate><metadata key="index" value="{index}" /><metadata key="prediction" value="600" />{objects}</plate>'
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", '<?xml version="1.0"?><model/>')
        zf.writestr("Metadata/slice_info.config", '<?xml version="1.0"?><config>' + "".join(plates) + "</config>")
        if settings is not None:
            zf.writestr("Metadata/project_settings.config", json.dumps(settings))
        if pack_gcode:
            for index in sorted(objects_by_plate):
                zf.writestr(f"Metadata/plate_{index}.gcode", b"; sliced\n" + marker)
        else:
            zf.writestr("Metadata/note.txt", marker or b"x")
    return buf.getvalue()


MONO = sliced_3mf({1: ["hook.stl", "hook.stl", "clip.stl"]}, marker=b"mono")
MULTI = sliced_3mf({1: ["body.stl"], 2: ["lid.stl", "lid.stl"]}, marker=b"multi")
#: A saved project, not an exported sliced file — m137 stamps such a container
#: ``has_sliced_gcode = False``, and it still names its objects.
PROJECT_ONLY = sliced_3mf({1: ["knob.stl", "knob.stl"]}, marker=b"project", pack_gcode=False)
SKIPPABLE = sliced_3mf(
    {1: ["tab.stl"]},
    marker=b"skippable",
    settings={"gcode_label_objects": "1", "exclude_object": "1"},
)


def _factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _add_file(db, *, filename: str, path: str, metadata: dict | None) -> int:
    row = LibraryFile(
        filename=filename,
        file_path=path,
        file_type="gcode" if filename.lower().endswith(".gcode.3mf") else "3mf",
        file_size=1,
        file_metadata=metadata,
    )
    db.add(row)
    await db.flush()
    await db.commit()
    return row.id


@pytest.fixture
async def library(db_session, tmp_path):
    """Three rows on disk: a 3MF with no objects, one that already has them, a STL.

    Only the first is work. The other two exist to prove the worklist is a
    filter and not a full-table sweep — re-parsing a file that already knows its
    objects is how a "one cheap SELECT" boot task turns into a library-wide
    rewrite.
    """
    files = tmp_path / "lib"
    files.mkdir()
    (files / "empty.gcode.3mf").write_bytes(MONO)
    (files / "known.gcode.3mf").write_bytes(MONO)
    (files / "part.stl").write_bytes(b"solid\n")

    ids = {
        "empty": await _add_file(
            db_session,
            filename="empty.gcode.3mf",
            path=str(files / "empty.gcode.3mf"),
            metadata={"print_time_seconds": 600},
        ),
        "known": await _add_file(
            db_session,
            filename="known.gcode.3mf",
            path=str(files / "known.gcode.3mf"),
            metadata={"printable_objects": {"7": "already.stl"}},
        ),
        "stl": await _add_file(
            db_session,
            filename="part.stl",
            path=str(files / "part.stl"),
            metadata=None,
        ),
    }
    return ids


# ── The worklist: who is work, and what it costs to ask ──────────────────────


@pytest.mark.asyncio
async def test_only_a_3mf_without_objects_is_work(library, test_engine):
    worklist = await files_missing_objects(_factory(test_engine))

    assert [fid for fid, _ in worklist] == [library["empty"]]


@pytest.mark.asyncio
async def test_a_plate_that_carries_objects_is_not_work(db_session, test_engine, tmp_path):
    """The objects can live per plate and nowhere else — a multi-plate file's
    top level never carries the other plates. Reading only the top level would
    put every such file back on the worklist at every boot."""
    target = tmp_path / "plated.gcode.3mf"
    target.write_bytes(MULTI)
    await _add_file(
        db_session,
        filename="plated.gcode.3mf",
        path=str(target),
        metadata={"plates": [{"index": 1, "printable_objects": {"101": "body.stl"}}, {"index": 2}]},
    )

    assert await files_missing_objects(_factory(test_engine)) == []


@pytest.mark.asyncio
async def test_a_plate_asked_and_found_nothing_is_not_asked_again(db_session, test_engine, tmp_path):
    """``parse_per_plate_skip_metadata`` writes ``printable_objects`` for every
    plate it finds, ``{}`` included — an empty map is an ANSWER. Keying the
    worklist on emptiness instead of on the key re-opens such files at every
    single boot, for ever."""
    target = tmp_path / "asked.gcode.3mf"
    target.write_bytes(MONO)
    await _add_file(
        db_session,
        filename="asked.gcode.3mf",
        path=str(target),
        metadata={"plates": [{"index": 1, "printable_objects": {}}]},
    )

    assert await files_missing_objects(_factory(test_engine)) == []


@pytest.mark.asyncio
async def test_a_project_3mf_with_no_packed_gcode_is_work_and_gets_its_objects(db_session, test_engine, tmp_path):
    """⚠️ The file this whole feature exists for. Objects have a third source —
    ``Metadata/slice_info.config`` — which Bambu Studio writes on every save of a
    sliced plate, while ``plate_N.gcode`` is packed only on "export sliced file".
    m137 stamped ``has_sliced_gcode = False`` across every legacy project 3MF, so
    a worklist that excluded on that flag would skip precisely these files."""
    target = tmp_path / "project.3mf"
    target.write_bytes(PROJECT_ONLY)
    file_id = await _add_file(
        db_session,
        filename="project.3mf",
        path=str(target),
        metadata={"has_sliced_gcode": False, "print_time_seconds": 120},
    )

    assert [fid for fid, _ in await files_missing_objects(_factory(test_engine))] == [file_id]
    assert (await backfill_library_objects(_factory(test_engine))).filled == 1

    db_session.expire_all()
    row = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == file_id))).scalar_one()
    assert sorted(row.file_metadata["printable_objects"].values()) == ["knob.stl", "knob.stl"]
    # Still true of the file, and not ours to revise.
    assert row.file_metadata["has_sliced_gcode"] is False
    assert await files_missing_objects(_factory(test_engine)) == []


@pytest.mark.asyncio
async def test_only_3mf_rows_are_fetched_at_all(db_session, test_engine, tmp_path):
    """⚠️ One SELECT, prefiltered in SQL. Fetching every non-deleted row's
    ``file_metadata`` so Python can JSON-parse it makes a boot task proportional
    to the whole library — on the event loop, for rows that can never be work."""
    for name in ("a.stl", "b.step", "c.gcode", "d.png"):
        (tmp_path / name).write_bytes(b"x")
        await _add_file(db_session, filename=name, path=str(tmp_path / name), metadata=None)
    (tmp_path / "e.gcode.3mf").write_bytes(MONO)
    only = await _add_file(db_session, filename="e.gcode.3mf", path=str(tmp_path / "e.gcode.3mf"), metadata=None)

    seen: list[tuple[str, object]] = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        if "library_files" in statement:
            seen.append((statement, parameters))

    event.listen(test_engine.sync_engine, "before_cursor_execute", spy)
    try:
        worklist = await files_missing_objects(_factory(test_engine))
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", spy)

    assert [fid for fid, _ in worklist] == [only]
    assert len(seen) == 1, seen
    assert "%.3mf" in str(seen[0][1])


@pytest.mark.asyncio
async def test_a_trashed_file_is_not_work(db_session, test_engine, tmp_path):
    """A file in the trash is not part of the library, and restoring it re-reads
    nothing — the sweep would be parsing files the user deleted."""
    target = tmp_path / "trashed.gcode.3mf"
    target.write_bytes(MONO)
    row = LibraryFile(
        filename="trashed.gcode.3mf",
        file_path=str(target),
        file_type="gcode",
        file_size=1,
        file_metadata=None,
        deleted_at=datetime.now(timezone.utc),
    )
    db_session.add(row)
    await db_session.commit()

    assert await files_missing_objects(_factory(test_engine)) == []


# ── What gets written ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_objects_are_written_and_the_others_left_alone(library, db_session, test_engine):
    summary = await backfill_library_objects(_factory(test_engine))

    assert isinstance(summary, BackfillSummary)
    assert (summary.scanned, summary.filled) == (1, 1)
    assert (summary.skipped_unreachable, summary.skipped_unparseable) == (0, 0)
    # ⚠️ Names the rows THIS run wrote. ``m158.seed()`` scopes its parts step to
    # them — "something was filled" would let a re-run walk the plates of a
    # product it never touched and put back an ``auto`` part somebody deleted.
    assert summary.filled_ids == [library["empty"]]

    db_session.expire_all()
    filled = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == library["empty"]))).scalar_one()
    objects = filled.file_metadata["printable_objects"]
    assert sorted(objects.values()) == ["clip.stl", "hook.stl", "hook.stl"]
    # Everything that was already there survives — this writes objects, it does
    # not re-derive the row's metadata.
    assert filled.file_metadata["print_time_seconds"] == 600

    untouched = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == library["known"]))).scalar_one()
    assert untouched.file_metadata == {"printable_objects": {"7": "already.stl"}}
    stl = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == library["stl"]))).scalar_one()
    assert stl.file_metadata is None


@pytest.mark.asyncio
async def test_a_second_run_finds_nothing(library, test_engine):
    """Idempotent by construction: the worklist query IS the marker. A
    ``DEBUG=true`` re-run of m158 and every boot after it must add nothing."""
    first = await backfill_library_objects(_factory(test_engine))
    second = await backfill_library_objects(_factory(test_engine))

    assert first.filled == 1
    assert (second.scanned, second.filled) == (0, 0)


@pytest.mark.asyncio
async def test_a_relative_path_resolves_under_the_data_directory(db_session, test_engine, tmp_path):
    """A managed file stores a path relative to ``base_dir``; an external one
    stores the mount path absolute. Both must resolve, or the whole managed half
    of a library reports itself unreachable."""
    (tmp_path / "library").mkdir(exist_ok=True)
    (tmp_path / "library" / "managed.gcode.3mf").write_bytes(MONO)
    fid = await _add_file(
        db_session,
        filename="managed.gcode.3mf",
        path="library/managed.gcode.3mf",
        metadata=None,
    )

    summary = await backfill_library_objects(_factory(test_engine))

    assert summary.filled == 1
    db_session.expire_all()
    row = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == fid))).scalar_one()
    assert len(row.file_metadata["printable_objects"]) == 3


@pytest.mark.asyncio
async def test_the_plates_of_a_multi_plate_file_each_get_their_own(db_session, test_engine, tmp_path):
    """Composition reads ``plates[].printable_objects`` first and the top level
    only as the whole-file fallback — filling one and not the other leaves every
    plate but the first empty."""
    target = tmp_path / "multi.gcode.3mf"
    target.write_bytes(MULTI)
    fid = await _add_file(
        db_session,
        filename="multi.gcode.3mf",
        path=str(target),
        metadata={"plates": [{"index": 1, "objects": ["body.stl"]}, {"index": 2, "objects": ["lid.stl"]}]},
    )

    assert (await backfill_library_objects(_factory(test_engine))).filled == 1

    db_session.expire_all()
    row = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == fid))).scalar_one()
    plates = {p["index"]: p for p in row.file_metadata["plates"]}
    assert sorted(plates[1]["printable_objects"].values()) == ["body.stl"]
    assert sorted(plates[2]["printable_objects"].values()) == ["lid.stl", "lid.stl"]
    # ``object_count`` is derived from the instances, never from the
    # name-deduplicated ``objects`` list — two clones are two.
    assert plates[2]["object_count"] == 2
    assert plates[1]["objects"] == ["body.stl"]


@pytest.mark.asyncio
async def test_a_plate_the_cached_list_never_held_is_added(db_session, test_engine, tmp_path):
    """A cached plate list can be SHORTER than the file — the parse that wrote it
    saw fewer plates, or was cut off. Merging only into what is there leaves the
    product permanently blind to the plates nobody recorded."""
    target = tmp_path / "short.gcode.3mf"
    target.write_bytes(MULTI)
    fid = await _add_file(
        db_session,
        filename="short.gcode.3mf",
        path=str(target),
        metadata={"plates": [{"index": 1, "objects": ["body.stl"]}], "is_multi_plate": False},
    )

    assert (await backfill_library_objects(_factory(test_engine))).filled == 1

    db_session.expire_all()
    row = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == fid))).scalar_one()
    assert sorted(p["index"] for p in row.file_metadata["plates"]) == [1, 2]
    assert row.file_metadata["is_multi_plate"] is True


@pytest.mark.asyncio
async def test_skip_objects_supported_is_derived_like_a_fresh_upload(db_session, test_engine, tmp_path):
    """The badge column is denormalised out of the metadata at every write site.
    A backfilled row that gains the objects but keeps a stale ``False`` badges
    differently from the same file uploaded fresh."""
    target = tmp_path / "skippable.gcode.3mf"
    target.write_bytes(SKIPPABLE)
    fid = await _add_file(db_session, filename="skippable.gcode.3mf", path=str(target), metadata=None)

    assert (await backfill_library_objects(_factory(test_engine))).filled == 1

    db_session.expire_all()
    row = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == fid))).scalar_one()
    assert row.file_metadata["gcode_label_objects"] is True
    assert row.file_metadata["exclude_object"] is True
    assert row.skip_objects_supported is True


# ── What must never take the sweep (or the upgrade) down ─────────────────────


@pytest.mark.asyncio
async def test_an_unreachable_file_is_counted_and_nothing_is_written(db_session, test_engine, tmp_path):
    """A mount that is down is indistinguishable from a file somebody deleted,
    and the row still describes real objects. Skipping is the only safe answer —
    and the next boot tries again."""
    fid = await _add_file(
        db_session,
        filename="gone.gcode.3mf",
        path=str(tmp_path / "never" / "gone.gcode.3mf"),
        metadata={"print_time_seconds": 42},
    )

    summary = await backfill_library_objects(_factory(test_engine))

    assert (summary.scanned, summary.filled, summary.skipped_unreachable) == (1, 0, 1)
    db_session.expire_all()
    row = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == fid))).scalar_one()
    assert row.file_metadata == {"print_time_seconds": 42}


@pytest.mark.asyncio
async def test_a_corrupt_container_is_counted_and_never_raises(db_session, test_engine, tmp_path):
    """One truncated 3MF must not sink the sweep — nor the upgrade that calls it."""
    broken = tmp_path / "broken.gcode.3mf"
    broken.write_bytes(b"PK\x03\x04 truncated")
    good = tmp_path / "good.gcode.3mf"
    good.write_bytes(MONO)
    await _add_file(db_session, filename="broken.gcode.3mf", path=str(broken), metadata=None)
    good_id = await _add_file(db_session, filename="good.gcode.3mf", path=str(good), metadata=None)

    summary = await backfill_library_objects(_factory(test_engine))

    assert (summary.scanned, summary.filled, summary.skipped_unparseable) == (2, 1, 1)
    assert summary.filled_ids == [good_id]
    db_session.expire_all()
    row = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == good_id))).scalar_one()
    assert row.file_metadata["printable_objects"]


@pytest.mark.asyncio
async def test_a_worklist_that_cannot_be_read_returns_an_empty_summary(test_engine, monkeypatch):
    """⚠️ It runs inside ``m158.seed()``. An exception out of here leaves the
    upgrade committed with no ``_migrations`` row, and the next boot re-enters
    the whole migration — because one SELECT failed."""

    async def boom(session_factory):
        raise RuntimeError("no such column")

    monkeypatch.setattr(backfill, "files_missing_objects", boom)

    summary = await backfill_library_objects(_factory(test_engine))

    assert (summary.scanned, summary.filled) == (0, 0)


@pytest.mark.asyncio
async def test_a_chunk_that_will_not_write_does_not_take_the_others_down(
    db_session, test_engine, tmp_path, monkeypatch
):
    """Each chunk is its own transaction, so a chunk that fails costs its own
    files and nothing else — the sweep is resumable at the next start anyway."""
    ids = []
    for name in ("one.gcode.3mf", "two.gcode.3mf"):
        (tmp_path / name).write_bytes(MONO)
        ids.append(await _add_file(db_session, filename=name, path=str(tmp_path / name), metadata=None))

    real = backfill._write_chunk
    calls = {"n": 0}

    async def flaky(session_factory, chunk, *, sync_products):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("locked")
        return await real(session_factory, chunk, sync_products=sync_products)

    monkeypatch.setattr(backfill, "_write_chunk", flaky)

    summary = await backfill_library_objects(_factory(test_engine), batch_size=1)

    assert (summary.scanned, summary.filled) == (2, 1)
    # The failed chunk's file is not claimed as filled — the caller's follow-up
    # work must not be told about a row that was rolled back.
    assert summary.filled_ids == [ids[1]]
    db_session.expire_all()
    first = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == ids[0]))).scalar_one()
    second = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == ids[1]))).scalar_one()
    assert first.file_metadata is None
    assert second.file_metadata["printable_objects"]


@pytest.mark.asyncio
async def test_the_writes_go_in_chunks(db_session, test_engine, tmp_path, monkeypatch):
    """⚠️ The m148 rule: one transaction per chunk, sized for LOCK TIME. A single
    transaction around the whole library takes SQLite's write lock for the length
    of the sweep, and unrelated queries die naming an innocent victim."""
    for n in range(backfill.BATCH_SIZE + 1):
        name = f"f{n}.gcode.3mf"
        (tmp_path / name).write_bytes(MONO)
        await _add_file(db_session, filename=name, path=str(tmp_path / name), metadata=None)

    sizes: list[int] = []
    real = backfill._write_chunk

    async def spy(session_factory, chunk, *, sync_products):
        sizes.append(len(chunk))
        return await real(session_factory, chunk, sync_products=sync_products)

    monkeypatch.setattr(backfill, "_write_chunk", spy)

    summary = await backfill_library_objects(_factory(test_engine))

    assert sizes == [backfill.BATCH_SIZE, 1]
    assert summary.filled == backfill.BATCH_SIZE + 1


@pytest.mark.asyncio
async def test_the_parse_never_runs_on_the_event_loop(library, test_engine, monkeypatch):
    """⚠️ Unzipping a 3MF off a network share on the loop stalls every other
    request in the process — the m148 rule, which this sweep runs under too."""
    seen: list[str] = []
    real = backfill._extract_objects

    def spy(file_path):
        seen.append(threading.current_thread().name)
        return real(file_path)

    monkeypatch.setattr(backfill, "_extract_objects", spy)

    await backfill_library_objects(_factory(test_engine))

    assert seen and all(name != threading.main_thread().name for name in seen)


@pytest.mark.asyncio
async def test_the_sweep_is_cancellable(library, test_engine):
    """It rides the lifespan, so shutdown cancels it mid-file. Nothing here may
    hold a session across the await that gets cancelled."""
    task = asyncio.create_task(backfill_library_objects(_factory(test_engine)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── Products: the half that makes the objects visible ────────────────────────


@pytest.mark.asyncio
async def test_a_product_bound_to_the_file_gains_its_parts(db_session, test_engine, tmp_path, monkeypatch):
    """The objects alone change nothing a user can see. ``sync_product_for_file``
    is the single writer of the pivot, the plates and the seeded parts — the
    backfill is only finished when it has run."""
    target = tmp_path / "linked.gcode.3mf"
    target.write_bytes(MONO)
    fid = await _add_file(db_session, filename="linked.gcode.3mf", path=str(target), metadata=None)
    product = Product(name="Desk Lamp")
    db_session.add(product)
    await db_session.flush()
    await db_session.execute(insert(product_files).values(product_id=product.id, library_file_id=fid))
    await db_session.commit()
    pid = product.id

    calls: list[tuple[int, list[int]]] = []
    real = backfill.sync_product_for_file

    async def spy(db, *, library_file_id, product_ids):
        calls.append((library_file_id, list(product_ids)))
        await real(db, library_file_id=library_file_id, product_ids=product_ids)

    monkeypatch.setattr(backfill, "sync_product_for_file", spy)

    summary = await backfill_library_objects(_factory(test_engine))

    assert calls == [(fid, [pid])]
    assert summary.products_synced == 1

    db_session.expire_all()
    parts = (await db_session.execute(select(ProductPart).where(ProductPart.product_id == pid))).scalars().all()
    assert {p.name_key: p.qty_per_unit for p in parts} == {"hook.stl": 2, "clip.stl": 1}
    plates = (await db_session.execute(select(ProductPlate).where(ProductPlate.product_id == pid))).scalars().all()
    assert [p.plate_index for p in plates] == [0]


@pytest.mark.asyncio
async def test_sync_products_false_writes_the_objects_and_touches_no_product(
    db_session, test_engine, tmp_path, monkeypatch
):
    """⚠️ What ``m158.seed()`` asks for. ``sync_product_for_file`` emits
    entity-wide ORM selects against TODAY's models; a migration running mid-chain
    may not, or the day a later migration adds a column to ``product_parts`` this
    seed emits SQL the database does not have. The migration seeds the parts in
    text SQL instead."""
    target = tmp_path / "linked.gcode.3mf"
    target.write_bytes(MONO)
    fid = await _add_file(db_session, filename="linked.gcode.3mf", path=str(target), metadata=None)
    product = Product(name="Desk Lamp")
    db_session.add(product)
    await db_session.flush()
    await db_session.execute(insert(product_files).values(product_id=product.id, library_file_id=fid))
    await db_session.commit()

    calls: list[int] = []

    async def spy(db, *, library_file_id, product_ids):
        calls.append(library_file_id)

    monkeypatch.setattr(backfill, "sync_product_for_file", spy)

    summary = await backfill_library_objects(_factory(test_engine), sync_products=False)

    assert (summary.filled, summary.products_synced) == (1, 0)
    assert calls == []
    db_session.expire_all()
    row = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == fid))).scalar_one()
    assert row.file_metadata["printable_objects"]


@pytest.mark.asyncio
async def test_a_file_that_belongs_to_nobody_costs_no_sync(library, test_engine, monkeypatch):
    """The inverse: the overwhelming majority of a library belongs to no product,
    and a sync per file would be a plate reconciliation per file for nothing."""
    calls: list[int] = []

    async def spy(db, *, library_file_id, product_ids):
        calls.append(library_file_id)

    monkeypatch.setattr(backfill, "sync_product_for_file", spy)

    summary = await backfill_library_objects(_factory(test_engine))

    assert summary.filled == 1
    assert calls == [] and summary.products_synced == 0


# ── The boot half: the same sweep, wired into the lifespan ───────────────────


@pytest.fixture
def boot_task():
    """Own ``main``'s task handle for the duration of a test.

    The handle is a module global, so a test that leaves one behind makes the
    next ``start_...`` a no-op — the failure would land on a different test than
    the one that caused it.
    """
    from backend.app import main

    main._library_objects_backfill_task = None
    yield main
    task = main._library_objects_backfill_task
    main._library_objects_backfill_task = None
    if task is not None and not task.done():
        task.cancel()


@pytest.mark.asyncio
async def test_the_boot_sweep_runs_and_the_handle_is_released_on_shutdown(
    library, test_engine, boot_task, monkeypatch, db_session
):
    """The upgrade's other half: a file whose mount was down when m158 ran is
    filled at a later start, with nobody asked to run anything."""
    monkeypatch.setattr(boot_task, "async_session", _factory(test_engine))

    boot_task.start_library_objects_backfill()
    task = boot_task._library_objects_backfill_task
    assert task is not None and not task.done()

    await task
    await boot_task.stop_library_objects_backfill()

    assert boot_task._library_objects_backfill_task is None
    db_session.expire_all()
    row = (await db_session.execute(select(LibraryFile).where(LibraryFile.id == library["empty"]))).scalar_one()
    assert row.file_metadata["printable_objects"]


@pytest.mark.asyncio
async def test_shutdown_cancels_a_sweep_that_is_still_walking(boot_task, monkeypatch):
    """⚠️ Cancelled AND awaited. A sweep left in flight holds a session while the
    engine goes away underneath it — and the task outlives the loop."""
    started = asyncio.Event()

    async def slow(session_factory, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(backfill, "backfill_library_objects", slow)

    boot_task.start_library_objects_backfill()
    task = boot_task._library_objects_backfill_task
    await asyncio.wait_for(started.wait(), timeout=5)

    await boot_task.stop_library_objects_backfill()

    assert task.done()
    assert boot_task._library_objects_backfill_task is None


@pytest.mark.asyncio
async def test_a_failure_in_the_sweep_never_escapes_the_task(boot_task, monkeypatch):
    """Nobody awaits this task, so an exception would surface as an "exception
    was never retrieved" line at some unrelated moment — or not at all."""

    async def boom(session_factory, **kwargs):
        raise RuntimeError("the share went away mid-walk")

    monkeypatch.setattr(backfill, "backfill_library_objects", boom)

    boot_task.start_library_objects_backfill()
    task = boot_task._library_objects_backfill_task
    await task

    assert task.exception() is None


@pytest.mark.asyncio
async def test_starting_twice_leaves_one_sweep(boot_task, monkeypatch):
    """The guard the other loops in ``main`` all carry: a second call must not
    put a second walk of the same library on the loop."""

    async def slow(session_factory, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(backfill, "backfill_library_objects", slow)

    boot_task.start_library_objects_backfill()
    first = boot_task._library_objects_backfill_task
    boot_task.start_library_objects_backfill()

    assert boot_task._library_objects_backfill_task is first


def test_the_lifespan_starts_it_and_awaits_it_on_shutdown():
    """⚠️ The wiring, not the behaviour — there is no lifespan harness here, and
    a start/stop pair nothing ever calls is dead code that reads as a feature.
    ``stop`` must be AWAITED: a bare call returns a coroutine nobody runs, which
    linters accept and shutdown ignores.
    """
    import inspect

    from backend.app import main

    source = inspect.getsource(main.lifespan)
    assert "start_library_objects_backfill()" in source
    assert "await stop_library_objects_backfill()" in source
