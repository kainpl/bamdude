"""GH104: keep the standard picker shade's exact name without changing its RGB."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.color_catalog import ColorCatalogEntry

pytestmark = pytest.mark.integration


async def assert_cream_names(async_client):
    response = await async_client.get("/api/v1/inventory/colors/map")
    assert response.status_code == 200, response.text
    mapping = response.json()
    assert mapping["colors"].get("fffdd0") == "Cream"
    from backend.app.core.catalog_defaults import DEFAULT_COLOR_CATALOG

    for brand, name, hex_color, _ in DEFAULT_COLOR_CATALOG:
        if brand == "Generic":
            assert mapping["colors"][hex_color.lstrip("#").lower()] == name
    # Existing Bambu priority remains deliberate for these shared swatches.
    assert mapping["colors"]["ffffff"] == "Jade White"
    assert mapping["colors"]["f5f5dc"] == "Natural"
    assert mapping["colors"]["ffd700"] == "Pineapple Yellow"
    assert mapping["colors"]["f9dfb9"] == "Cream"  # Bambu's separate PETG HF shade.
    assert mapping["by_material"]["petg hf|ffffff"] == "White"
    response = await async_client.get(
        "/api/v1/inventory/colors/by-material", params={"hex": "FFFDD0", "material": "Bambu PETG HF"}
    )
    assert response.json() == {"color_name": "Cream"}


async def test_reset_catalog_names_the_standard_cream_swatch(async_client):
    response = await async_client.post("/api/v1/inventory/colors/reset")
    assert response.status_code == 200, response.text
    await assert_cream_names(async_client)


async def test_fresh_install_names_the_standard_cream_swatch(async_client, test_engine):
    from backend.app.migrations.m001_bamdude_baseline import _seed_color_catalog

    await _seed_color_catalog(async_sessionmaker(test_engine, expire_on_commit=False))
    await assert_cream_names(async_client)


async def test_seed_works_before_future_model_columns_exist(monkeypatch):
    from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.orm import registry

    from backend.app.migrations.m214_generic_cream_color import seed
    from backend.app.models import color_catalog

    # A future model has a Python default; the schema at this migration does not.
    future_table = Table(
        "color_catalog",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("manufacturer", String),
        Column("color_name", String),
        Column("hex_color", String),
        Column("material", String),
        Column("is_default", Boolean),
        Column("future_flag", Boolean, default=False),
    )

    class FutureEntry:
        pass

    mapper_registry = registry()
    mapper_registry.map_imperatively(FutureEntry, future_table)
    monkeypatch.setattr(color_catalog, "ColorCatalogEntry", FutureEntry)
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE color_catalog (id INTEGER PRIMARY KEY, manufacturer TEXT, "
                    "color_name TEXT, hex_color TEXT, material TEXT, is_default BOOLEAN)"
                )
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await seed(factory)
        await seed(factory)
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT color_name, hex_color FROM color_catalog"))).all()
        assert ("Cream", "#FFFDD0") in rows
        assert len(rows) == len(set(rows))
    finally:
        await engine.dispose()
        mapper_registry.dispose()


async def test_backfill_is_repeatable_and_does_not_replace_brand_colors(async_client, db_session, test_engine):
    from backend.app.migrations.m214_generic_cream_color import seed

    db_session.add_all(
        [
            ColorCatalogEntry(manufacturer="Bambu Lab", color_name="Cream", hex_color="#F9DFB9", material="PETG HF"),
            ColorCatalogEntry(manufacturer="Bambu Lab", color_name="White", hex_color="#FFFFFF", material="PETG HF"),
        ]
    )
    await db_session.commit()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    await seed(factory)
    await seed(factory)
    entries = (await db_session.scalars(select(ColorCatalogEntry).order_by(ColorCatalogEntry.id))).all()
    assert [(e.manufacturer, e.color_name, e.hex_color) for e in entries[:2]] == [
        ("Bambu Lab", "Cream", "#F9DFB9"),
        ("Bambu Lab", "White", "#FFFFFF"),
    ]
    from backend.app.core.catalog_defaults import DEFAULT_COLOR_CATALOG

    expected = {
        hex_color.lower().lstrip("#"): name for brand, name, hex_color, _ in DEFAULT_COLOR_CATALOG if brand == "Generic"
    }
    assert {e.hex_color.lower().lstrip("#"): e.color_name for e in entries[2:]} == expected
    assert all(e.is_default and e.material == "" for e in entries[2:])
    response = await async_client.get("/api/v1/inventory/colors/map")
    assert response.json() == {"colors": {"f9dfb9": "Cream", "ffffff": "White", **expected}, "by_material": {}}


@pytest.mark.parametrize("hex_color", ["#FFFDD0", "fffdd0", "#fFfDd0"])
async def test_backfill_preserves_an_existing_custom_hex_name(db_session, test_engine, hex_color):
    from backend.app.migrations.m214_generic_cream_color import seed

    entry = ColorCatalogEntry(
        manufacturer="Workshop", color_name="Warm custom shade", hex_color=hex_color, material="PLA"
    )
    db_session.add(entry)
    await db_session.commit()
    await seed(async_sessionmaker(test_engine, expire_on_commit=False))
    await db_session.refresh(entry)
    entries = (await db_session.scalars(select(ColorCatalogEntry))).all()
    assert sum(e.hex_color.lstrip("#").upper() == "FFFDD0" for e in entries) == 1
    assert (entry.manufacturer, entry.color_name, entry.hex_color, entry.material, entry.is_default) == (
        "Workshop",
        "Warm custom shade",
        hex_color,
        "PLA",
        False,
    )


async def test_backfill_preserves_an_edited_generic_cream(db_session, test_engine):
    from backend.app.migrations.m214_generic_cream_color import seed

    entry = ColorCatalogEntry(manufacturer="generic", color_name=" cream ", hex_color="#FFF4D8", material="PETG")
    db_session.add(entry)
    await db_session.commit()
    await seed(async_sessionmaker(test_engine, expire_on_commit=False))
    await db_session.refresh(entry)
    entries = (await db_session.scalars(select(ColorCatalogEntry))).all()
    assert sum(e.color_name.strip().lower() == "cream" for e in entries) == 1
    assert (entry.color_name, entry.hex_color, entry.material) == (" cream ", "#FFF4D8", "PETG")


async def test_backfill_preserves_edits_to_every_generic_picker_shade(db_session, test_engine):
    from backend.app.core.catalog_defaults import DEFAULT_COLOR_CATALOG
    from backend.app.migrations.m214_generic_cream_color import seed

    edited = [
        ("generic", " " + name.lower() + " ", "#123456", "PETG")
        for brand, name, _, _ in DEFAULT_COLOR_CATALOG
        if brand == "Generic"
    ]
    db_session.add_all(
        [ColorCatalogEntry(manufacturer=b, color_name=n, hex_color=h, material=m) for b, n, h, m in edited]
    )
    await db_session.commit()
    await seed(async_sessionmaker(test_engine, expire_on_commit=False))
    rows = (await db_session.scalars(select(ColorCatalogEntry).order_by(ColorCatalogEntry.id))).all()
    assert [(e.manufacturer, e.color_name, e.hex_color, e.material) for e in rows] == edited
    assert all(not e.is_default for e in rows)
