"""Merge, delete, duplicate, export / import: what happens to a part's picture (spec §10.3; plan E4, task 29)."""

import io
import json
import shutil
import zipfile

import pytest
from sqlalchemy import delete, select

from backend.app.models.plate_render import PlateRender, PlateRenderObject
from backend.app.models.product import ProductPart, ProductPlate
from backend.app.services import part_images
from backend.app.services.product_files import product_part_images_dir
from backend.tests.fixtures.part_images import rendered_farm
from backend.tests.unit.test_part_images_writer import image_bytes

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _pin(db, farm, part_, identify_id=11, plate=1):
    part_.image_source, part_.image_file_id = "instance", farm.file.id
    part_.image_plate_index, part_.image_identify_id = plate, identify_id
    await db.commit()


async def test_a_merge_keeps_the_target_choice_and_drops_the_source_photo(committing_client, db_session):
    farm = await rendered_farm(db_session)
    await _pin(db_session, farm, farm.body)
    await part_images.set_photo(db_session, farm.lid.id, image_bytes())
    await db_session.commit()
    photo = product_part_images_dir(farm.product.id) / farm.lid.image_photo
    r = await committing_client.post(
        f"/api/v1/products/{farm.product.id}/parts/{farm.body.id}/merge", json={"source_part_id": farm.lid.id}
    )
    assert r.status_code == 200, r.text
    await part_images.drain()
    await db_session.refresh(farm.body)
    assert farm.body.image_source == "instance"  # the target keeps its own choice
    assert r.json()["image_choice"]["source"] == "instance"  # and the answer says so (task 30's decorator)
    assert not photo.exists()


async def test_deleting_a_part_drops_its_photo(committing_client, db_session):
    farm = await rendered_farm(db_session)
    await part_images.set_photo(db_session, farm.lid.id, image_bytes())
    await db_session.commit()
    photo = product_part_images_dir(farm.product.id) / farm.lid.image_photo
    r = await committing_client.delete(f"/api/v1/products/{farm.product.id}/parts/{farm.lid.id}")
    assert r.status_code == 200, r.text
    await part_images.drain()
    assert not photo.exists()


async def test_deleting_a_product_drops_its_part_photos(committing_client, db_session):
    farm = await rendered_farm(db_session)
    await part_images.set_photo(db_session, farm.lid.id, image_bytes())
    await db_session.commit()
    r = await committing_client.delete(f"/api/v1/products/{farm.product.id}")
    assert r.status_code == 200, r.text
    await part_images.drain()
    assert not product_part_images_dir(farm.product.id).exists()


async def test_a_duplicate_copies_the_photo_and_a_pin_whose_file_it_linked(committing_client, db_session):
    farm = await rendered_farm(db_session)
    await _pin(db_session, farm, farm.body)
    await part_images.set_photo(db_session, farm.lid.id, image_bytes())
    await db_session.commit()
    r = await committing_client.post(f"/api/v1/products/{farm.product.id}/duplicate", json={})
    assert r.status_code == 200, r.text
    copy_id = r.json()["id"]
    parts = {p["name"]: p for p in r.json()["parts"]}
    assert parts["Body"]["image"]["kind"] == "render" and parts["Lid"]["image"]["kind"] == "photo"
    rows = {
        p.name: p
        for p in (await db_session.execute(select(ProductPart).where(ProductPart.product_id == copy_id))).scalars()
    }
    assert rows["Body"].image_source == "instance" and rows["Body"].image_file_id == farm.file.id
    assert rows["Lid"].image_source == "photo"
    assert (product_part_images_dir(copy_id) / rows["Lid"].image_photo).is_file()


async def test_a_duplicate_without_the_link_makes_the_pin_auto(committing_client, db_session, monkeypatch):
    farm = await rendered_farm(db_session)
    await _pin(db_session, farm, farm.body)
    from backend.app.api.routes import products as products_routes

    async def refused(*args, **kwargs):  # the caller may not link library files
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Missing required permissions")

    monkeypatch.setattr(products_routes.RequestCredentials, "check", refused)
    r = await committing_client.post(f"/api/v1/products/{farm.product.id}/duplicate", json={})
    assert r.status_code == 200, r.text
    assert r.json()["links_skipped"] is True
    body = (
        await db_session.execute(
            select(ProductPart).where(ProductPart.product_id == r.json()["id"], ProductPart.name == "Body")
        )
    ).scalar_one()
    assert body.image_source == "auto" and body.image_file_id is None


async def test_a_duplicate_whose_photo_copy_fails_keeps_nothing_and_stays_auto(
    committing_client, db_session, monkeypatch
):
    """Consilium E4-R2 B, duplicate side: the copy fails after the original landed; the duplicate commits."""
    farm = await rendered_farm(db_session)
    await part_images.set_photo(db_session, farm.lid.id, image_bytes())
    await db_session.commit()
    original = shutil.copyfile

    def fail_small(source, target, *args, **kwargs):
        if str(target).endswith(".sm.png"):
            raise OSError("injected ENOSPC on the small copy")
        return original(source, target, *args, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", fail_small)
    r = await committing_client.post(f"/api/v1/products/{farm.product.id}/duplicate", json={})
    assert r.status_code == 200, r.text
    await part_images.drain()
    copy_id = r.json()["id"]
    lid = (
        await db_session.execute(
            select(ProductPart).where(ProductPart.product_id == copy_id, ProductPart.name == "Lid")
        )
    ).scalar_one()
    assert (lid.image_source, lid.image_photo) == ("auto", None)
    target = product_part_images_dir(copy_id)
    assert not target.exists() or list(target.iterdir()) == []


async def _export(client, farm) -> bytes:
    exported = await client.get(f"/api/v1/products/{farm.product.id}/export")
    assert exported.status_code == 200, exported.text
    return exported.content


def _with_images(archive: bytes, images) -> bytes:
    """The archive with each part's image entry replaced by ``images(part name, manifest)``."""
    source = zipfile.ZipFile(io.BytesIO(archive))
    manifest = json.loads(source.read("product.json"))
    for entry in manifest["parts"]:
        entry["image"] = images(entry["name"], manifest)
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(rebuilt, "w") as zf:
        for info in source.infolist():
            zf.writestr(info, json.dumps(manifest) if info.filename == "product.json" else source.read(info))
    return rebuilt.getvalue()


async def _import(client, db, archive: bytes) -> dict[str, ProductPart]:
    imported = await client.post("/api/v1/products/import", files={"file": ("p.zip", archive, "application/zip")})
    assert imported.status_code == 200, imported.text
    new_id = imported.json()["product"]["id"]
    rows = (await db.execute(select(ProductPart).where(ProductPart.product_id == new_id))).scalars()
    return {p.name: p for p in rows}


async def _plates_of(db, product_id: int) -> set[int]:
    """The plates an import really linked -- asserted BEFORE any image choice is judged (consilium E4.2-R1):
    a pin refused because its plate never got linked would otherwise pass for a refusal of its object."""
    rows = await db.execute(select(ProductPlate.plate_index).where(ProductPlate.product_id == product_id))
    return set(rows.scalars())


async def test_export_and_import_carry_the_choice(committing_client, db_session):
    farm = await rendered_farm(db_session, on_disk=True)  # the export hashes real bytes; the import reuses the row
    await _pin(db_session, farm, farm.body)
    await part_images.set_photo(db_session, farm.lid.id, image_bytes())
    await db_session.commit()
    archive = await _export(committing_client, farm)
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        manifest = json.loads(zf.read("product.json"))
        images = {p["name"]: p["image"] for p in manifest["parts"]}
        assert images["Body"] == {
            "source": "instance",
            "instance": {"file_hash": farm.file.file_hash, "plate": 1, "identify_id": 11},
        }
        assert images["Lid"]["source"] == "photo"
        assert f"attachments/{images['Lid']['photo']}" in zf.namelist()
    rows = await _import(committing_client, db_session, archive)
    assert await _plates_of(db_session, rows["Body"].product_id) == {1, 2}
    assert (rows["Body"].image_source, rows["Body"].image_plate_index, rows["Body"].image_identify_id) == (
        "instance",
        1,
        11,
    )
    assert rows["Lid"].image_source == "photo"
    assert (product_part_images_dir(rows["Lid"].product_id) / rows["Lid"].image_photo).is_file()


async def test_a_single_plate_pin_survives_export_and_import(committing_client, db_session):
    """The commonest real file: the sync links its one plate as 0, and the pin keeps plate 0 through the archive."""
    farm = await rendered_farm(db_session, on_disk=True, single_plate=True)
    await _pin(db_session, farm, farm.body, plate=0)
    rows = await _import(committing_client, db_session, await _export(committing_client, farm))
    assert await _plates_of(db_session, rows["Body"].product_id) == {0}
    assert (rows["Body"].image_source, rows["Body"].image_plate_index, rows["Body"].image_identify_id) == (
        "instance",
        0,
        11,
    )


async def test_a_pin_that_holds_is_kept_before_its_pixels_exist(committing_client, db_session):
    """D22: on a new farm the plate renders after the import; a structurally sound pin waits for it."""
    farm = await rendered_farm(db_session, on_disk=True)
    await _pin(db_session, farm, farm.body)
    archive = await _export(committing_client, farm)
    await db_session.execute(delete(PlateRenderObject))
    await db_session.execute(delete(PlateRender))
    await db_session.commit()
    rows = await _import(committing_client, db_session, archive)
    assert await _plates_of(db_session, rows["Body"].product_id) == {1, 2}
    assert (rows["Body"].image_source, rows["Body"].image_plate_index, rows["Body"].image_identify_id) == (
        "instance",
        1,
        11,
    )


@pytest.mark.parametrize(
    ("plate", "identify_id"),
    [
        (2**100, 11),
        (2**31, 11),
        (-1, 11),
        (1, 2**32),
        (1, 2**100),
        (True, 11),
        (1, True),
        (1, "11"),
        (5, 11),  # a plate the new product does not link at all
        (2, 11),  # a linked plate the object is not on
        (1, 13),  # the linked plate 1, another part's object -- fails if the ownership check goes
        (1, 99),  # the linked plate 1, no such object
    ],
    ids=[
        "plate-2^100",
        "plate-past-int32",
        "plate-negative",
        "id-past-u32",
        "id-2^100",
        "plate-bool",
        "id-bool",
        "id-numeric-string",
        "plate-not-linked",
        "object-not-on-that-plate",
        "another-parts-object",
        "absent-object",
    ],
)
async def test_a_pin_that_does_not_hold_on_this_farm_imports_as_auto(committing_client, db_session, plate, identify_id):
    """Consilium E4-R3 / E4.2-R1: a malformed or foreign pin never aborts the import -- the part is simply auto.
    The plates the import linked are asserted first, so a refusal is the validator's, not a missing plate's."""
    farm = await rendered_farm(db_session, on_disk=True)
    archive = await _export(committing_client, farm)

    def body_pin(name, manifest):
        if name != "Body":
            return {"source": "auto"}
        digest = manifest["files"][0]["hash"]
        return {"source": "instance", "instance": {"file_hash": digest, "plate": plate, "identify_id": identify_id}}

    rows = await _import(committing_client, db_session, _with_images(archive, body_pin))
    assert await _plates_of(db_session, rows["Body"].product_id) == {1, 2}
    assert (rows["Body"].image_source, rows["Body"].image_file_id) == ("auto", None)


@pytest.mark.parametrize(
    "image",
    [
        {"source": "instance", "instance": {"file_hash": "0" * 64, "plate": 1, "identify_id": 11}},
        {"source": "instance", "instance": {"file_hash": None, "plate": "x"}},
        {"source": "photo", "photo": "part-images/absent.png"},
        {"source": "photo", "photo": "../../etc/passwd"},
        "garbage",
    ],
)
async def test_an_unreadable_choice_imports_as_auto(committing_client, db_session, image):
    farm = await rendered_farm(db_session)
    archive = _with_images(await _export(committing_client, farm), lambda _name, _manifest: image)
    rows = await _import(committing_client, db_session, archive)
    assert {p.image_source for p in rows.values()} == {"auto"}
