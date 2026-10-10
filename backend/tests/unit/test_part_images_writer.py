"""part_images: the writer -- one source, its own data, files after the commit (spec §10.3; plan E4, task 29)."""

import asyncio
import io
import threading
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import text

from backend.app.schemas.part_image import InstanceKey
from backend.app.services import part_images
from backend.app.services.product_files import product_part_images_dir
from backend.tests.fixtures.part_images import part, rendered_farm


def image_bytes(fmt: str = "PNG", size=(40, 20), exif_rotate: bool = False) -> bytes:
    image = Image.new("RGB", size, (10, 120, 200))
    buffer = io.BytesIO()
    if exif_rotate:
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90° clockwise on display
        image.save(buffer, fmt, exif=exif)
    else:
        image.save(buffer, fmt)
    return buffer.getvalue()


async def test_a_pin_must_be_a_ready_candidate_of_the_part(db_session):
    farm = await rendered_farm(db_session, methods={11: "toolpath", 12: "skipped", 13: "top_mask", 14: "toolpath"})
    for identify_id in (12, 13, 14, 99):  # skipped, another part's, unassigned, absent
        with pytest.raises(HTTPException) as refused:
            await part_images.set_choice(
                db_session,
                farm.body.id,
                "instance",
                InstanceKey(library_file_id=farm.file.id, plate_index=1, identify_id=identify_id),
            )
        assert refused.value.status_code == 422
    part_ = await part_images.set_choice(
        db_session, farm.body.id, "instance", InstanceKey(library_file_id=farm.file.id, plate_index=1, identify_id=11)
    )
    assert (part_.image_source, part_.image_file_id, part_.image_plate_index, part_.image_identify_id) == (
        "instance",
        farm.file.id,
        1,
        11,
    )


async def test_a_purchased_part_takes_no_pin(db_session):
    farm = await rendered_farm(db_session)
    bought = part(farm.product.id, "Screw", kind="purchased")
    db_session.add(bought)
    await db_session.commit()
    with pytest.raises(HTTPException) as refused:
        await part_images.set_choice(
            db_session, bought.id, "instance", InstanceKey(library_file_id=farm.file.id, plate_index=1, identify_id=11)
        )
    assert refused.value.status_code == 422


async def test_auto_clears_the_pin_and_the_photo(db_session):
    farm = await rendered_farm(db_session)
    await part_images.set_photo(db_session, farm.body.id, image_bytes())
    await db_session.commit()
    photo = product_part_images_dir(farm.product.id) / farm.body.image_photo
    part_ = await part_images.set_choice(db_session, farm.body.id, "auto", None)
    await db_session.commit()
    await part_images.drain()
    assert (part_.image_source, part_.image_photo, part_.image_file_id) == ("auto", None, None)
    assert not photo.exists() and not photo.with_name(part_images.small_photo_name(photo.name)).exists()


@pytest.mark.parametrize(("fmt", "ext"), [("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp")])
async def test_a_photo_is_kept_as_sent_with_a_small_copy(db_session, fmt, ext):
    farm = await rendered_farm(db_session)
    content = image_bytes(fmt)
    part_ = await part_images.set_photo(db_session, farm.body.id, content)
    await db_session.commit()
    path = product_part_images_dir(farm.product.id) / part_.image_photo
    assert part_.image_source == "photo" and path.suffix == f".{ext}" and path.read_bytes() == content
    with Image.open(path.with_name(part_images.small_photo_name(path.name))) as small:
        assert small.format == "PNG" and max(small.size) <= 128


async def test_the_small_photo_is_upright(db_session):
    farm = await rendered_farm(db_session)
    part_ = await part_images.set_photo(db_session, farm.body.id, image_bytes("JPEG", (40, 20), exif_rotate=True))
    await db_session.commit()
    small = product_part_images_dir(farm.product.id) / part_images.small_photo_name(part_.image_photo)
    with Image.open(small) as image:
        assert image.size[1] > image.size[0]  # 40x20 turned on its side


@pytest.mark.parametrize("content", [b"not an image", image_bytes("GIF"), image_bytes("BMP")])
async def test_anything_but_png_jpeg_webp_is_refused(db_session, content):
    farm = await rendered_farm(db_session)
    with pytest.raises(HTTPException) as refused:
        await part_images.set_photo(db_session, farm.body.id, content)
    assert refused.value.status_code == 422
    assert not product_part_images_dir(farm.product.id).exists() or not any(
        product_part_images_dir(farm.product.id).iterdir()
    )


async def test_a_photo_past_the_pixel_cap_is_refused_before_decoding(db_session, monkeypatch):
    farm = await rendered_farm(db_session)
    monkeypatch.setattr(part_images, "PHOTO_MAX_PIXELS", 40 * 20 - 1)
    with pytest.raises(HTTPException) as refused:
        await part_images.set_photo(db_session, farm.body.id, image_bytes())
    assert refused.value.status_code == 422 and "pixels" in refused.value.detail


async def test_a_photo_past_the_size_cap_is_413(db_session, monkeypatch):
    from backend.app.services import product_files

    farm = await rendered_farm(db_session)
    monkeypatch.setattr(product_files, "MAX_ATTACHMENT_BYTES", 10)
    with pytest.raises(HTTPException) as refused:
        await part_images.set_photo(db_session, farm.body.id, image_bytes())
    assert refused.value.status_code == 413


async def test_a_rolled_back_photo_leaves_no_file_and_keeps_the_old_one(db_session):
    farm = await rendered_farm(db_session)
    first = await part_images.set_photo(db_session, farm.body.id, image_bytes())
    await db_session.commit()
    old_name = first.image_photo
    directory = product_part_images_dir(farm.product.id)
    await part_images.set_photo(db_session, farm.body.id, image_bytes("JPEG"))
    await db_session.rollback()
    await part_images.drain()
    await db_session.refresh(farm.body)
    assert farm.body.image_photo == old_name
    assert sorted(p.name for p in directory.iterdir()) == sorted([old_name, part_images.small_photo_name(old_name)])


def _barrier(monkeypatch, name: str):
    """Hold ``part_images.<name>`` (a function that runs in a thread) until released."""
    original = getattr(part_images, name)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def held(*args):
        entered.set()
        assert release.wait(10)
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(part_images, name, held)
    return entered, release, finished


async def _cancel_while_held(coro, entered, release, finished):
    """Consilium E4-R2 A: cancel the caller while its thread is held, then let the thread finish."""
    task = asyncio.create_task(coro)
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.2)
    assert not task.done()  # the cancelled caller still owns its running thread
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()  # the thread is over before the caller's cleanup and the rollback


async def test_a_cancelled_upload_leaves_nothing_and_keeps_the_old_photo(db_session, monkeypatch):
    farm = await rendered_farm(db_session)
    old = (await part_images.set_photo(db_session, farm.body.id, image_bytes())).image_photo
    await db_session.commit()
    directory = product_part_images_dir(farm.product.id)  # read now: the rollback expires every row
    held = _barrier(monkeypatch, "_write_photo")
    await _cancel_while_held(part_images.set_photo(db_session, farm.body.id, image_bytes("JPEG")), *held)
    await db_session.rollback()
    await part_images.drain()
    await db_session.refresh(farm.body)
    assert farm.body.image_photo == old
    assert sorted(p.name for p in directory.iterdir()) == sorted([old, part_images.small_photo_name(old)])


async def test_a_cancelled_duplicate_copy_leaves_nothing(db_session, monkeypatch):
    from backend.app.models.product import Product

    farm = await rendered_farm(db_session)
    await part_images.set_photo(db_session, farm.lid.id, image_bytes())
    copy = Product(name="Copy")
    db_session.add(copy)
    await db_session.flush()
    copy_lid = part(copy.id, "Lid")
    db_session.add(copy_lid)
    await db_session.commit()
    target = product_part_images_dir(copy.id)  # read now: the rollback expires every row
    source = product_part_images_dir(farm.product.id) / farm.lid.image_photo
    held = _barrier(monkeypatch, "_copy_photos")
    await _cancel_while_held(part_images.copy_for_duplicate(db_session, [(farm.lid, copy_lid)], copy.id), *held)
    await db_session.rollback()
    await part_images.drain()
    assert not target.exists() or list(target.iterdir()) == []
    assert source.is_file()  # the source stays


def _fail_small_writes(monkeypatch) -> None:
    original = Path.write_bytes

    def fail_small(path, content):
        if path.name.endswith(".sm.png"):
            raise OSError("injected ENOSPC on the small copy")
        return original(path, content)

    monkeypatch.setattr(Path, "write_bytes", fail_small)


async def test_an_imported_photo_whose_small_copy_fails_leaves_nothing_when_the_import_commits(db_session, monkeypatch):
    """Consilium E4-R2 B: the import catches the error and commits -- the half-written photo must not stay."""
    farm = await rendered_farm(db_session)
    member = "attachments/part-images/" + "e" * 32 + ".png"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(member, image_bytes())
    _fail_small_writes(monkeypatch)
    with zipfile.ZipFile(buffer) as zf:
        await part_images.import_choices(
            db_session,
            zf,
            {member},
            farm.product.id,
            [(farm.lid, {"source": "photo", "photo": member.removeprefix("attachments/")})],
            {},
        )
    monkeypatch.undo()
    await db_session.commit()
    await part_images.drain()
    assert farm.lid.image_source == "auto"
    directory = product_part_images_dir(farm.product.id)
    assert not directory.exists() or list(directory.iterdir()) == []


async def test_a_replaced_photo_goes_after_the_commit(db_session):
    farm = await rendered_farm(db_session)
    first = (await part_images.set_photo(db_session, farm.body.id, image_bytes())).image_photo
    await db_session.commit()
    second = (await part_images.set_photo(db_session, farm.body.id, image_bytes("WEBP"))).image_photo
    directory = product_part_images_dir(farm.product.id)
    await part_images.drain()
    assert (directory / first).exists()  # not before the commit
    await db_session.commit()
    await part_images.drain()
    assert not (directory / first).exists() and (directory / second).exists()


async def test_clearing_a_photo_returns_to_auto_and_is_idempotent(db_session):
    farm = await rendered_farm(db_session)
    await part_images.set_photo(db_session, farm.body.id, image_bytes())
    await db_session.commit()
    part_ = await part_images.clear_photo(db_session, farm.body.id)
    assert (part_.image_source, part_.image_photo) == ("auto", None)
    again = await part_images.clear_photo(db_session, farm.body.id)
    assert again.image_source == "auto"


async def test_the_writer_marks_the_product_changed(db_session, monkeypatch):
    from backend.app.core.websocket import ws_manager

    sent = []

    async def record(message):
        sent.append(message)

    monkeypatch.setattr(ws_manager, "broadcast", record)
    farm = await rendered_farm(db_session)
    await part_images.set_choice(db_session, farm.body.id, "auto", None)
    await db_session.commit()
    await part_images.drain()
    assert sent == [{"type": "part_images_changed", "data": {"product_ids": [farm.product.id]}}]


async def test_an_unknown_part_is_404(db_session):
    await db_session.execute(text("select 1"))
    with pytest.raises(HTTPException) as refused:
        await part_images.clear_photo(db_session, 999_999)
    assert refused.value.status_code == 404
