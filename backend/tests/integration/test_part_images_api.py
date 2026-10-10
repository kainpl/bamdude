"""The part picture API (spec §11; plan E4, task 31)."""

import pytest

from backend.app.core.auth import create_media_token
from backend.app.services import part_images
from backend.tests.fixtures.part_images import rendered_farm
from backend.tests.unit.test_part_images_writer import image_bytes

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _bare_get(client, path: str):
    request = client.build_request("GET", path)
    request.headers.pop("Authorization", None)
    return await client.send(request)


async def test_the_part_image_routers_are_mounted():
    """Consilium E4-R1: the new module imports, and main mounts both routers under the API prefix."""
    from fastapi.routing import APIRoute

    from backend.app.main import app

    paths = {(sorted(r.methods)[0], r.path) for r in app.routes if isinstance(r, APIRoute)}
    assert {
        ("GET", "/api/v1/product-parts/{part_id}/image"),
        ("GET", "/api/v1/product-parts/{part_id}/image-candidates"),
        ("PUT", "/api/v1/product-parts/{part_id}/image"),
        ("POST", "/api/v1/product-parts/{part_id}/image/photo"),
        ("DELETE", "/api/v1/product-parts/{part_id}/image/photo"),
        (
            "GET",
            "/api/v1/products/{product_id}/files/{library_file_id}/plates/{plate_index}/objects/{identify_id}/image",
        ),
        ("POST", "/api/v1/products/{product_id}/part-images/rerender"),
    } <= paths


async def test_the_picture_is_served_with_a_media_token(committing_client, db_session):
    farm = await rendered_farm(db_session, write_files=True)
    product = (await committing_client.get(f"/api/v1/products/{farm.product.id}")).json()
    v = next(p for p in product["parts"] if p["name"] == "Body")["image"]["v"]
    token = await create_media_token("test_admin")
    r = await _bare_get(committing_client, f"/api/v1/product-parts/{farm.body.id}/image?v={v}&size=lg&token={token}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert (await _bare_get(committing_client, f"/api/v1/product-parts/{farm.body.id}/image")).status_code == 401


@pytest.mark.parametrize("v", [None, "stale"])
async def test_without_the_current_v_the_picture_is_revalidated(committing_client, db_session, v):
    farm = await rendered_farm(db_session, write_files=True)
    query = f"?v={v}" if v else ""
    r = await committing_client.get(f"/api/v1/product-parts/{farm.body.id}/image{query}")
    assert r.status_code == 200 and r.headers["cache-control"] == "private, no-cache"


async def test_no_picture_is_one_404(committing_client, db_session):
    farm = await rendered_farm(db_session, status="pending")
    assert (await committing_client.get(f"/api/v1/product-parts/{farm.body.id}/image")).status_code == 404
    assert (await committing_client.get("/api/v1/product-parts/999999/image")).status_code == 404


async def test_the_size_is_one_of_two(committing_client, db_session):
    farm = await rendered_farm(db_session, write_files=True)
    assert (await committing_client.get(f"/api/v1/product-parts/{farm.body.id}/image?size=xl")).status_code == 422


async def test_an_object_picture_is_served_only_for_a_plate_the_product_links(committing_client, db_session):
    farm = await rendered_farm(db_session, write_files=True)
    base = f"/api/v1/products/{farm.product.id}/files/{farm.file.id}/plates/1/objects"
    assert (await committing_client.get(f"{base}/14/image")).status_code == 200
    assert (await committing_client.get(f"{base}/99/image")).status_code == 404
    other = f"/api/v1/products/{farm.product.id + 1}/files/{farm.file.id}/plates/1/objects/14/image"
    assert (await committing_client.get(other)).status_code == 404


async def test_pin_photo_and_back_through_the_doors(committing_client, db_session):
    farm = await rendered_farm(db_session, write_files=True)
    candidates = (await committing_client.get(f"/api/v1/product-parts/{farm.body.id}/image-candidates")).json()
    assert [c["identify_id"] for c in candidates] == [11, 12]
    pinned = await committing_client.put(
        f"/api/v1/product-parts/{farm.body.id}/image",
        json={"source": "instance", "instance": {"library_file_id": farm.file.id, "plate_index": 1, "identify_id": 12}},
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["image_choice"]["pin"] == {"valid": True, "reason": None}
    photo = await committing_client.post(
        f"/api/v1/product-parts/{farm.body.id}/image/photo", files={"file": ("p.png", image_bytes(), "image/png")}
    )
    assert photo.status_code == 200, photo.text
    assert photo.json()["image"]["kind"] == "photo" and photo.json()["image_choice"]["source"] == "photo"
    cleared = await committing_client.delete(f"/api/v1/product-parts/{farm.body.id}/image/photo")
    assert cleared.status_code == 200 and cleared.json()["image_choice"]["source"] == "auto"
    await part_images.drain()


async def test_a_refusal_is_a_sentence(committing_client, db_session):
    farm = await rendered_farm(db_session)
    r = await committing_client.put(
        f"/api/v1/product-parts/{farm.body.id}/image",
        json={"source": "instance", "instance": {"library_file_id": farm.file.id, "plate_index": 1, "identify_id": 13}},
    )
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


@pytest.mark.parametrize("field", ["library_file_id", "plate_index"])
async def test_a_pin_past_the_columns_is_refused_by_the_body(committing_client, db_session, field):
    """D22: the body is bounded like the columns, so nothing past them reaches a query or a write."""
    farm = await rendered_farm(db_session)
    instance = {"library_file_id": farm.file.id, "plate_index": 1, "identify_id": 11, field: 2**31}
    r = await committing_client.put(
        f"/api/v1/product-parts/{farm.body.id}/image", json={"source": "instance", "instance": instance}
    )
    assert r.status_code == 422


async def test_rerender_queues_the_product_plates(committing_client, db_session):
    farm = await rendered_farm(db_session, status="failed")
    r = await committing_client.post(f"/api/v1/products/{farm.product.id}/part-images/rerender", json={})
    assert r.status_code == 200 and r.json() == {"queued": 1}
    full = await committing_client.post(f"/api/v1/products/{farm.product.id}/part-images/rerender", json={"full": True})
    assert full.status_code == 200
    assert (await committing_client.post("/api/v1/products/999999/part-images/rerender", json={})).status_code == 404
