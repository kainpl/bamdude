"""part_images: the change collector -- an event after the root commit, never after a rollback (plan E4, task 27)."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import settings
from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductPlate
from backend.app.services import part_images, part_renders

GEN = "g" * 32


@pytest.fixture
def events(monkeypatch):
    from backend.app.core.websocket import ws_manager

    sent: list[dict] = []

    async def record(message):
        sent.append(message)

    monkeypatch.setattr(ws_manager, "broadcast", record)
    return sent


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


def _event(*ids):
    return {"type": "part_images_changed", "data": {"product_ids": list(ids)}}


def _photo(product_id: int, name: str):
    path = settings.products_dir / str(product_id) / "part-images" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


async def test_a_root_commit_sends_one_event_with_every_product(db_session, events):
    await db_session.execute(text("select 1"))
    part_images.mark_changed(db_session, [3, 1])
    part_images.mark_changed(db_session, [1, 2])
    await db_session.commit()
    await part_images.drain()
    assert events == [_event(1, 2, 3)]


@pytest.mark.parametrize("end", ["rollback", "close"])
async def test_a_transaction_that_does_not_commit_says_nothing(test_engine, events, end):
    session = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)()
    await session.execute(text("select 1"))
    part_images.mark_changed(session, [7])
    doomed = _photo(7, "a" * 32 + ".png")
    part_images.unlink_on_rollback(session, doomed)
    if end == "rollback":
        await session.rollback()
    await session.close()
    await part_images.drain()
    assert events == []
    assert not doomed.exists()  # written for a write that did not land


async def test_a_rolled_back_savepoint_takes_only_its_own_marks_and_files(db_session, events):
    await db_session.execute(text("select 1"))
    part_images.mark_changed(db_session, [1])
    kept = _photo(1, "b" * 32 + ".png")
    part_images.unlink_on_rollback(db_session, kept)
    savepoint = await db_session.begin_nested()
    part_images.mark_changed(db_session, [2])
    doomed = _photo(1, "c" * 32 + ".png")
    part_images.unlink_on_rollback(db_session, doomed)
    part_images.unlink_after_commit(db_session, kept)  # undone with the savepoint
    await savepoint.rollback()
    await part_images.drain()
    assert not doomed.exists() and kept.exists()
    await db_session.commit()
    await part_images.drain()
    assert events == [_event(1)]
    assert kept.exists()


async def test_a_released_savepoint_keeps_its_marks(db_session, events):
    await db_session.execute(text("select 1"))
    async with db_session.begin_nested():
        part_images.mark_changed(db_session, [5])
    await db_session.commit()
    await part_images.drain()
    assert events == [_event(5)]


async def test_a_file_dropped_after_commit_goes_only_after_it(db_session, events):
    await db_session.execute(text("select 1"))
    old = _photo(4, "d" * 32 + ".png")
    part_images.unlink_after_commit(db_session, old)
    await part_images.drain()
    assert old.exists()
    await db_session.commit()
    await part_images.drain()
    assert not old.exists()


async def test_nothing_outside_the_products_root_is_removed(db_session, tmp_path):
    stray = tmp_path / "elsewhere.png"
    stray.write_bytes(b"x")
    await db_session.execute(text("select 1"))
    part_images.unlink_after_commit(db_session, stray)
    await db_session.commit()
    await part_images.drain()
    assert stray.exists()


async def _linked(db, *, sha: str, plate: int, deleted: bool = False) -> int:
    file = LibraryFile(
        filename=f"{sha[:4]}.gcode.3mf",
        file_path=f"library/{sha[:4]}.gcode.3mf",
        file_type="gcode",
        file_size=10,
        file_hash=sha,
        deleted_at=part_renders.utcnow() if deleted else None,
    )
    product = Product(name="Lamp")
    db.add_all([file, product])
    await db.flush()
    db.add(ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=plate))
    await db.commit()
    return product.id


async def test_a_render_change_reaches_every_product_that_links_the_plate(db_session, events):
    live = await _linked(db_session, sha="a" * 64, plate=1)
    trashed = await _linked(db_session, sha="a" * 64, plate=1, deleted=True)
    await _linked(db_session, sha="a" * 64, plate=2)  # another plate of the same file
    heard: list[set] = []
    unsubscribe = part_images.subscribe(heard.append)
    try:
        await part_images.mark_renders_changed(db_session, [("a" * 64, 1)])
        await part_images.drain()
        assert events == [] and heard == []  # nothing before the commit
        await db_session.commit()
        await part_images.drain()
    finally:
        unsubscribe()
    assert events == [_event(*sorted([live, trashed]))]
    assert heard == [{("a" * 64, 1)}]


async def test_a_failed_lookup_costs_the_event_not_the_transaction(db_session, events, monkeypatch):
    async def broken(db, keys):
        await db.execute(text("select * from no_such_table"))

    monkeypatch.setattr(part_images, "products_of_plates", broken)
    await db_session.execute(text("select 1"))
    await part_images.mark_renders_changed(db_session, [("a" * 64, 1)])
    await db_session.execute(text("select 1"))  # the transaction is still usable
    await db_session.commit()


async def test_requeue_no_runtime_says_which_plates_went_back(db_session, factory, events):
    pid = await _linked(db_session, sha="b" * 64, plate=1)
    from backend.app.models.plate_render import PlateRender
    from backend.app.services.part_render_protocol import RENDERER_VERSION

    db_session.add(
        PlateRender(
            file_sha256="b" * 64, plate_index=1, renderer_version=RENDERER_VERSION, status="ready", reason="no_runtime"
        )
    )
    await db_session.commit()
    assert await part_renders.requeue_no_runtime(factory) == 1
    await part_images.drain()
    assert events == [_event(pid)]


async def test_a_rerender_names_every_product_that_shares_the_plate(db_session, events):
    """Consilium note 3 / D23: a re-render moves a row every linked product shows, not only the asker's."""
    from backend.app.models.plate_render import PlateRender
    from backend.app.services.part_render_protocol import RENDERER_VERSION

    asker = await _linked(db_session, sha="e" * 64, plate=1)
    sharer = await _linked(db_session, sha="e" * 64, plate=1)
    db_session.add(PlateRender(file_sha256="e" * 64, plate_index=1, renderer_version=RENDERER_VERSION, status="failed"))
    await db_session.commit()
    assert await part_renders.rerender_for_product(db_session, asker, full=False) == 1
    await db_session.commit()
    await part_images.drain()
    assert events == [_event(*sorted([asker, sharer]))]


async def test_a_rerender_that_moves_nothing_says_nothing(db_session, events):
    from backend.app.models.plate_render import PlateRender
    from backend.app.services.part_render_protocol import RENDERER_VERSION

    asker = await _linked(db_session, sha="f" * 64, plate=1)
    db_session.add(PlateRender(file_sha256="f" * 64, plate_index=1, renderer_version=RENDERER_VERSION, status="ready"))
    await db_session.commit()
    assert await part_renders.rerender_for_product(db_session, asker, full=False) == 0
    await db_session.commit()
    await part_images.drain()
    assert events == []


async def test_a_backfill_that_inserts_nothing_says_nothing(db_session, factory, events):
    await _linked(db_session, sha="c" * 64, plate=1)
    assert await part_renders.backfill(factory) == 1
    await part_images.drain()
    assert len(events) == 1
    events.clear()
    assert await part_renders.backfill(factory) == 0
    await part_images.drain()
    assert events == []
