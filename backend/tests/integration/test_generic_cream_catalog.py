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
    assert [(e.manufacturer, e.color_name, e.hex_color) for e in entries] == [
        ("Bambu Lab", "Cream", "#F9DFB9"),
        ("Bambu Lab", "White", "#FFFFFF"),
        ("Generic", "Cream", "#FFFDD0"),
    ]
    assert entries[-1].is_default is True
    response = await async_client.get("/api/v1/inventory/colors/map")
    assert response.json() == {"colors": {"f9dfb9": "Cream", "ffffff": "White", "fffdd0": "Cream"}, "by_material": {}}


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
    assert len(entries) == 1
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
    assert len((await db_session.scalars(select(ColorCatalogEntry))).all()) == 1
    assert (entry.color_name, entry.hex_color, entry.material) == (" cream ", "#FFF4D8", "PETG")
