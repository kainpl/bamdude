"""m196 and the model: a part's picture choice (spec part-thumbnails §8.6; plan E4, task 26)."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

COLUMNS = {"image_source", "image_file_id", "image_plate_index", "image_identify_id", "image_photo"}
PARTS_DDL = (
    "CREATE TABLE product_parts (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, kind VARCHAR(16) NOT NULL,"
    " name VARCHAR(512) NOT NULL, name_key VARCHAR(512) NOT NULL)"
)


@pytest.fixture
async def bare():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    yield engine
    await engine.dispose()


async def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in (await conn.exec_driver_sql(f"PRAGMA table_info({table})")).all()}


async def test_the_migration_adds_the_columns_and_is_idempotent(bare):
    from backend.app.migrations import m196_part_images as m196

    async with bare.begin() as conn:
        await conn.exec_driver_sql(PARTS_DDL)
        await conn.exec_driver_sql("INSERT INTO product_parts VALUES (1, 1, 'printed', 'Body', 'body')")
        await m196.upgrade(conn)
        await m196.upgrade(conn)  # DEBUG=true re-runs the newest migration on every start
        assert await _columns(conn, "product_parts") >= COLUMNS
        row = (await conn.exec_driver_sql("SELECT image_source, image_photo FROM product_parts")).one()
    assert tuple(row) == ("auto", None)  # an existing part starts on the automatic choice


async def test_the_model_and_the_migration_agree(bare, test_engine):
    from backend.app.migrations import m196_part_images as m196

    async with bare.begin() as conn:
        await conn.exec_driver_sql(PARTS_DDL)
        await m196.upgrade(conn)
        migrated = await _columns(conn, "product_parts") & COLUMNS
    async with test_engine.begin() as conn:
        created = await _columns(conn, "product_parts") & COLUMNS
    assert migrated == created == COLUMNS


def test_the_photos_live_under_the_products_root():
    from backend.app.core.config import settings
    from backend.app.services.product_files import product_part_images_dir

    assert product_part_images_dir(9) == settings.products_dir / "9" / "part-images"
