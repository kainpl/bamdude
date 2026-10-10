"""part_renders: the queue -- who gets a row, which row goes next, and every transition (plan E3, task 20)."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import settings
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.plate_render import PlateRender
from backend.app.models.product import Product, ProductPlate
from backend.app.services import part_images, part_renders
from backend.app.services.part_render_protocol import RENDERER_VERSION

GEN = "g" * 32


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def live_generation():
    part_renders.set_live_generation(GEN)
    yield
    part_renders.set_live_generation(None)


@pytest.fixture
def changed():
    seen: list[set] = []
    unsubscribe = part_images.subscribe(seen.append)
    yield seen
    unsubscribe()


async def linked(
    db, *, sha="a" * 64, plates=(1,), name="p.gcode.3mf", file_type="gcode", deleted=False, folder=None, metadata=None
):
    file = LibraryFile(
        file_metadata=metadata,
        filename=name,
        file_path=f"library/{name}" if folder is None else f"{folder.external_path}/{name}",
        file_type=file_type,
        file_size=10,
        file_hash=sha,
        is_external=folder is not None,
        folder_id=folder.id if folder is not None else None,
        deleted_at=part_renders.utcnow() if deleted else None,
    )
    product = Product(name="Lamp")
    db.add_all([file, product])
    await db.flush()
    db.add_all([ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=p) for p in plates])
    await db.commit()
    return file


async def rows(db) -> list[PlateRender]:
    return (await db.execute(select(PlateRender).order_by(PlateRender.id))).scalars().all()


async def test_ensure_puts_one_pending_row_per_plate_and_is_idempotent(db_session):
    file = await linked(db_session, plates=(1, 2))
    assert await part_renders.ensure_for_files(db_session, [file.id]) == 2
    assert await part_renders.ensure_for_files(db_session, [file.id]) == 0
    await db_session.commit()
    got = await rows(db_session)
    assert [(r.plate_index, r.status, r.phase, r.priority, r.renderer_version) for r in got] == [
        (1, "pending", "render", 1, RENDERER_VERSION),
        (2, "pending", "render", 1, RENDERER_VERSION),
    ]


async def test_ensure_skips_unsliced_trashed_and_unhashed_files(db_session):
    unsliced = await linked(db_session, sha="b" * 64, name="m.3mf", file_type="3mf")
    trashed = await linked(db_session, sha="c" * 64, deleted=True)
    unhashed = await linked(db_session, sha=None)
    assert await part_renders.ensure_for_files(db_session, [unsliced.id, trashed.id, unhashed.id]) == 0


async def test_ensure_never_commits(db_session):
    file = await linked(db_session)
    await part_renders.ensure_for_files(db_session, [file.id])
    await db_session.rollback()
    assert await rows(db_session) == []


async def test_next_task_takes_the_highest_priority_then_the_oldest(db_session):
    a = await linked(db_session, sha="a" * 64)
    b = await linked(db_session, sha="b" * 64)
    await part_renders.ensure_for_files(db_session, [a.id], priority=0)
    await part_renders.ensure_for_files(db_session, [b.id], priority=2)
    await db_session.commit()
    task = await part_renders.next_task(db_session, part_renders.utcnow())
    assert task.file_sha256 == "b" * 64 and task.priority == 2


async def test_a_row_waiting_for_its_retry_is_not_picked(db_session, factory):
    file = await linked(db_session)
    await part_renders.ensure_for_files(db_session, [file.id])
    await db_session.commit()
    now = part_renders.utcnow()
    task = await part_renders.next_task(db_session, now)
    assert await part_renders.record_attempt_failure(factory, task, "timeout", GEN, now)
    assert await part_renders.next_task(db_session, now) is None  # a restart reads the same row: no memory needed
    assert (await part_renders.next_task(db_session, now + timedelta(seconds=61))).attempts == 1


async def test_the_queue_skips_a_hash_whose_files_are_all_in_the_trash(db_session):
    old = await linked(db_session, sha="a" * 64)
    twin = await linked(db_session, sha="a" * 64, name="copy.gcode.3mf")
    other = await linked(db_session, sha="b" * 64)
    await part_renders.ensure_for_files(db_session, [old.id, other.id])
    await db_session.commit()
    old.deleted_at = part_renders.utcnow()
    await db_session.commit()
    task = await part_renders.next_task(db_session, part_renders.utcnow())
    assert task.file_sha256 == "a" * 64 and task.source.library_file_id == twin.id  # the live copy is read
    twin.deleted_at = part_renders.utcnow()
    await db_session.commit()
    task = await part_renders.next_task(db_session, part_renders.utcnow())
    assert task.file_sha256 == "b" * 64  # skipped, not parked: the younger row still runs
    twin.deleted_at = None
    await db_session.commit()
    assert (await part_renders.next_task(db_session, part_renders.utcnow())).file_sha256 == "a" * 64


async def test_the_source_is_composed_from_the_row_alone(db_session):
    managed = await linked(db_session, sha="a" * 64, name="m.gcode.3mf")
    folder = LibraryFolder(name="NAS", is_external=True, external_path="/mnt/nas/prints")
    db_session.add(folder)
    await db_session.commit()
    external = await linked(db_session, sha="b" * 64, name="x.gcode", folder=folder)
    await part_renders.ensure_for_files(db_session, [managed.id, external.id])
    await db_session.commit()
    sources = {}
    for _ in range(2):
        task = await part_renders.next_task(db_session, part_renders.utcnow())
        sources[task.file_sha256] = task.source
        await db_session.execute(
            PlateRender.__table__.update().where(PlateRender.id == task.render_id).values(status="failed")
        )
        await db_session.commit()
    assert sources["a" * 64].root == str(settings.library_dir)
    assert sources["a" * 64].path == str(settings.base_dir / "library" / "m.gcode.3mf")
    assert sources["a" * 64].kind == "3mf"
    assert (sources["b" * 64].root, sources["b" * 64].kind) == ("/mnt/nas/prints", "gcode")  # no stat, no resolve


async def _one(db_session, **kwargs):
    file = await linked(db_session, **kwargs)
    await part_renders.ensure_for_files(db_session, [file.id])
    await db_session.commit()
    return await part_renders.next_task(db_session, part_renders.utcnow())


async def _row(db_session, task) -> PlateRender:
    db_session.expire_all()
    return await db_session.get(PlateRender, task.render_id)


async def test_a_transient_failure_backs_off(db_session, factory, changed):
    task = await _one(db_session)
    now = part_renders.utcnow()
    assert await part_renders.record_attempt_failure(factory, task, "crashed", GEN, now)
    row = await _row(db_session, task)
    assert (row.status, row.phase, row.attempts, row.last_error) == ("pending", "render", 1, "crashed")
    assert row.next_attempt_at == now + timedelta(seconds=60)
    assert changed == []  # still pending, the same result: no picture moved (E4 final review)


async def test_only_a_visible_transition_tells_the_picture_collector(db_session, factory, changed):
    """E4 final review: a back-off, a switch to the fallback phase or a defer leave the plate pending with its
    result as it was -- every client refetching its Workshop views for that is load with nothing to show.
    A status change (here a terminal one) is a picture change."""
    task = await _one(db_session)
    now = part_renders.utcnow()
    assert await part_renders.enter_fallback(factory, task, "no_runtime", GEN, now)
    assert await part_renders.defer(factory, task, "settle_failed", GEN, now)
    assert changed == []
    assert await part_renders.mark_terminal(factory, task, "failed", "source_changed", GEN, now)
    assert changed == [{(task.file_sha256, task.plate_index)}]


async def test_the_last_failure_with_a_result_keeps_the_old_pictures(db_session, factory):
    task = await _one(db_session)
    await db_session.execute(
        PlateRender.__table__.update().where(PlateRender.id == task.render_id).values(attempts=2, result_dir="r")
    )
    await db_session.commit()
    task = await part_renders.next_task(db_session, part_renders.utcnow())
    assert await part_renders.record_attempt_failure(factory, task, "timeout", GEN, part_renders.utcnow())
    row = await _row(db_session, task)
    assert (row.status, row.reason, row.result_dir) == ("ready", "rerender_failed", "r")


async def test_the_last_failure_without_a_result_goes_to_the_fallback(db_session, factory):
    task = await _one(db_session)
    await db_session.execute(PlateRender.__table__.update().where(PlateRender.id == task.render_id).values(attempts=2))
    await db_session.commit()
    task = await part_renders.next_task(db_session, part_renders.utcnow())
    now = part_renders.utcnow()
    assert await part_renders.record_attempt_failure(factory, task, "timeout", GEN, now)
    row = await _row(db_session, task)
    assert (row.status, row.phase, row.reason, row.next_attempt_at) == ("pending", "fallback", "render_failed", now)


async def test_memory_limit_goes_to_the_fallback_without_counting(db_session, factory):
    task = await _one(db_session)
    assert await part_renders.enter_fallback(factory, task, "memory_limit", GEN, part_renders.utcnow())
    row = await _row(db_session, task)
    assert (row.phase, row.reason, row.attempts) == ("fallback", "memory_limit", 0)


async def test_a_terminal_mark_is_final(db_session, factory):
    task = await _one(db_session)
    assert await part_renders.mark_terminal(factory, task, "failed", "source_changed", GEN, part_renders.utcnow())
    row = await _row(db_session, task)
    assert (row.status, row.reason) == ("failed", "source_changed")
    assert await part_renders.next_task(db_session, part_renders.utcnow()) is None


async def test_a_stale_generation_writes_nothing(db_session, factory, changed):
    task = await _one(db_session)
    assert not await part_renders.mark_terminal(factory, task, "failed", "crashed", "x" * 32, part_renders.utcnow())
    assert (await _row(db_session, task)).status == "pending"
    assert changed == []


async def test_a_row_that_moved_on_is_not_written(db_session, factory):
    task = await _one(db_session)
    await db_session.execute(
        PlateRender.__table__.update().where(PlateRender.id == task.render_id).values(status="ready")
    )
    await db_session.commit()
    assert not await part_renders.record_attempt_failure(factory, task, "crashed", GEN, part_renders.utcnow())
    assert (await _row(db_session, task)).status == "ready"


async def test_no_runtime_rows_go_back_to_the_queue_with_their_pictures(db_session, factory):
    task = await _one(db_session)
    await db_session.execute(
        PlateRender.__table__.update()
        .where(PlateRender.id == task.render_id)
        .values(status="ready", reason="no_runtime", result_dir="r")
    )
    await db_session.commit()
    assert await part_renders.requeue_no_runtime(factory) == 1
    row = await _row(db_session, task)
    assert (row.status, row.phase, row.priority, row.result_dir) == ("pending", "render", 0, "r")


async def test_rerender_reopens_failed_plates_and_with_full_ready_ones(db_session):
    task = await _one(db_session)
    product_id = (await db_session.execute(select(ProductPlate.product_id))).scalar_one()
    await db_session.execute(
        PlateRender.__table__.update().where(PlateRender.id == task.render_id).values(status="ready")
    )
    await db_session.commit()
    assert await part_renders.rerender_for_product(db_session, product_id, full=False) == 0
    assert await part_renders.rerender_for_product(db_session, product_id, full=True) == 1
    await db_session.commit()
    row = await _row(db_session, task)
    assert (row.status, row.priority, row.attempts) == ("pending", 2, 0)


async def test_backfill_queues_every_linked_sliced_file_at_backfill_priority(db_session, factory):
    await linked(db_session, sha="a" * 64, plates=(1, 2))
    assert await part_renders.backfill(factory) == 2
    assert {r.priority for r in await rows(db_session)} == {0}


async def test_a_sliced_3mf_without_the_gcode_suffix_is_queued_and_an_unsliced_one_is_not(db_session):
    """Final review I1: a plate exported from the slicer reaches the library as Foo.3mf (file_type "3mf")
    with its G-code intact; LibraryFile.is_printable is the one answer to "sliced"."""
    sliced = await linked(
        db_session, sha="b" * 64, name="Foo.3mf", file_type="3mf", metadata={"has_sliced_gcode": True}
    )
    plain = await linked(
        db_session, sha="c" * 64, name="Bar.3mf", file_type="3mf", metadata={"has_sliced_gcode": False}
    )
    assert await part_renders.ensure_for_files(db_session, [sliced.id, plain.id]) == 1
    await db_session.commit()
    assert [r.file_sha256 for r in await rows(db_session)] == ["b" * 64]
    task = await part_renders.next_task(db_session, part_renders.utcnow())
    assert (task.file_sha256, task.source.kind) == ("b" * 64, "3mf")
