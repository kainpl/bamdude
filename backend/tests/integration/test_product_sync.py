"""The one door files walk through to join a product (spec §Composition sync).

Every assertion here is about the same three questions: which plates a product
owns for a file, which parts those plates seeded, and what survives when the
link goes away. Parts outlive links — targets belong to the product, not to the
file that happened to introduce them.
"""

import pytest
from sqlalchemy import select

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.services.product_sync import (
    apply_folder_products,
    inherit_folder_products,
    resync_file_products,
    sync_product_for_file,
    wanted_plate_indices,
)

pytestmark = pytest.mark.integration

MULTI = {
    "plates": [
        {
            "index": 1,
            "printable_objects": {"1": "bracket.stl", "2": "bracket.stl_2", "3": "lid.stl"},
            "print_time_seconds": 10,
        },
        {"index": 2, "printable_objects": {str(i): f"clip.stl_{i}" for i in range(1, 11)}, "print_time_seconds": 20},
    ]
}
SINGLE = {"plates": [{"index": 1, "printable_objects": {"1": "hook.stl"}, "print_time_seconds": 5}]}


async def _product(db, name="Prod"):
    p = Product(name=name)
    db.add(p)
    await db.flush()
    return p


async def _file(db, name, meta, folder_id=None, file_type="gcode"):
    f = LibraryFile(
        filename=name, file_path=name, file_size=1, file_type=file_type, file_metadata=meta, folder_id=folder_id
    )
    db.add(f)
    await db.flush()
    return f


async def _plates(db, product_id):
    rows = (await db.execute(select(ProductPlate).where(ProductPlate.product_id == product_id))).scalars().all()
    return sorted((r.library_file_id, r.plate_index) for r in rows)


async def _parts(db, product_id):
    rows = (await db.execute(select(ProductPart).where(ProductPart.product_id == product_id))).scalars().all()
    return {r.name_key: r for r in rows}


def test_wanted_plates_follow_the_zero_convention():
    assert wanted_plate_indices(SINGLE) == {0}
    assert wanted_plate_indices(MULTI) == {1, 2}
    assert wanted_plate_indices(None) == {0}


@pytest.mark.asyncio
async def test_linking_a_file_plants_plates_and_auto_parts(db_session):
    product = await _product(db_session)
    file = await _file(db_session, "m.gcode.3mf", MULTI)
    await sync_product_for_file(db_session, library_file_id=file.id, product_ids=[product.id])
    await db_session.commit()

    assert await _plates(db_session, product.id) == [(file.id, 1), (file.id, 2)]
    parts = await _parts(db_session, product.id)
    assert parts["bracket.stl"].qty_per_unit == 2 and parts["bracket.stl"].auto is True
    assert parts["lid.stl"].qty_per_unit == 1
    assert parts["clip.stl"].qty_per_unit == 10 and parts["clip.stl"].aliases == ["clip.stl"]


@pytest.mark.asyncio
async def test_relink_is_idempotent_and_never_overwrites_an_edited_part(db_session):
    product = await _product(db_session)
    file = await _file(db_session, "m.gcode.3mf", MULTI)
    await sync_product_for_file(db_session, library_file_id=file.id, product_ids=[product.id])
    parts = await _parts(db_session, product.id)
    parts["bracket.stl"].qty_per_unit = 7
    parts["bracket.stl"].auto = False
    await db_session.commit()

    await sync_product_for_file(db_session, library_file_id=file.id, product_ids=[product.id])
    await db_session.commit()
    parts = await _parts(db_session, product.id)
    assert parts["bracket.stl"].qty_per_unit == 7 and len(parts) == 3
    assert await _plates(db_session, product.id) == [(file.id, 1), (file.id, 2)]


@pytest.mark.asyncio
async def test_reslice_drops_vanished_plates_and_adds_new_ones(db_session):
    product = await _product(db_session)
    file = await _file(db_session, "m.gcode.3mf", MULTI)
    await sync_product_for_file(db_session, library_file_id=file.id, product_ids=[product.id])
    file.file_metadata = {
        "plates": [MULTI["plates"][0], {"index": 3, "printable_objects": {"1": "foot.stl"}, "print_time_seconds": 1}]
    }
    await db_session.commit()

    await resync_file_products(db_session, file.id)
    await db_session.commit()
    assert await _plates(db_session, product.id) == [(file.id, 1), (file.id, 3)]
    parts = await _parts(db_session, product.id)
    assert "foot.stl" in parts and "clip.stl" in parts  # parts are never deleted by a sync


@pytest.mark.asyncio
async def test_unlinking_drops_plates_but_keeps_parts(db_session):
    product = await _product(db_session)
    file = await _file(db_session, "m.gcode.3mf", MULTI)
    await sync_product_for_file(db_session, library_file_id=file.id, product_ids=[product.id])
    await sync_product_for_file(db_session, library_file_id=file.id, product_ids=[])
    await db_session.commit()
    assert await _plates(db_session, product.id) == []
    assert len(await _parts(db_session, product.id)) == 3


@pytest.mark.asyncio
async def test_geometry_files_own_no_plates(db_session):
    product = await _product(db_session)
    stl = await _file(db_session, "x.stl", None, file_type="stl")
    await sync_product_for_file(db_session, library_file_id=stl.id, product_ids=[product.id])
    await db_session.commit()
    assert await _plates(db_session, product.id) == []


@pytest.mark.asyncio
async def test_a_file_created_in_a_product_folder_inherits_the_products(db_session):
    product = await _product(db_session)
    # ⚠️ Linked at construction, not after the flush: assigning a collection on
    # an already-persistent row lazy-loads the old one first, which under an
    # async session is a MissingGreenlet. Routes dodge it with selectinload.
    folder = LibraryFolder(name="F", products=[product])
    db_session.add(folder)
    await db_session.flush()
    file = await _file(db_session, "s.gcode.3mf", SINGLE, folder_id=folder.id)

    await inherit_folder_products(db_session, file, folder)
    await db_session.commit()
    await db_session.refresh(file, ["products"])
    assert [p.id for p in file.products] == [product.id]
    assert await _plates(db_session, product.id) == [(file.id, 0)]
    assert (await _parts(db_session, product.id))["hook.stl"].qty_per_unit == 1


@pytest.mark.asyncio
async def test_applying_products_to_a_folder_mirrors_onto_every_child_file(db_session):
    first = await _product(db_session, "A")
    second = await _product(db_session, "B")
    folder = LibraryFolder(name="F")
    db_session.add(folder)
    await db_session.flush()
    one = await _file(db_session, "one.gcode.3mf", SINGLE, folder_id=folder.id)
    two = await _file(db_session, "two.gcode.3mf", MULTI, folder_id=folder.id)

    await apply_folder_products(db_session, folder_id=folder.id, product_ids=[first.id, second.id])
    await db_session.commit()
    for child in (one, two):
        await db_session.refresh(child, ["products"])
        assert sorted(p.id for p in child.products) == sorted([first.id, second.id])
    for product in (first, second):
        assert await _plates(db_session, product.id) == [(one.id, 0), (two.id, 1), (two.id, 2)]

    await apply_folder_products(db_session, folder_id=folder.id, product_ids=[])
    await db_session.commit()
    for child in (one, two):
        await db_session.refresh(child, ["products"])
        assert child.products == []
    assert await _plates(db_session, first.id) == []
    assert await _plates(db_session, second.id) == []

    with pytest.raises(ValueError, match="9999"):
        await apply_folder_products(db_session, folder_id=folder.id, product_ids=[9999])
