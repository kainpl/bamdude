"""The batch read of part pictures (spec procurement-stock-images R4; consilium O7, O8, O12).

Stock and order views stop re-reading their figures when only a picture changed; they read the
pictures here instead. One answer per unique part asked: the part's product and its picture, off
the same loaded row, through the one resolver every other surface uses.
"""

import pytest
from sqlalchemy import event

from backend.app.core.auth import create_media_token
from backend.app.services import part_images
from backend.tests.auth_helpers import mint_test_authority
from backend.tests.fixtures.part_images import part, rendered_farm
from backend.tests.integration.test_workshop_library_rights import _jwt, _key, _user
from backend.tests.unit.test_part_images_writer import image_bytes

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

URL = "/api/v1/product-parts/images"


def _query(*ids) -> str:
    return URL + ("?" + "&".join(f"part_ids={i}" for i in ids) if ids else "")


async def _farm_with_purchases(db):
    farm = await rendered_farm(db)
    screw = part(farm.product.id, "Screw", kind="purchased")
    nut = part(farm.product.id, "Nut", kind="purchased")
    db.add_all([screw, nut])
    await db.commit()
    await part_images.set_photo(db, screw.id, image_bytes())
    await db.commit()
    return farm, screw, nut


async def test_the_route_is_mounted():
    from fastapi.routing import APIRoute

    from backend.app.main import app

    assert ("GET", "/api/v1/product-parts/images") in {
        (sorted(r.methods)[0], r.path) for r in app.routes if isinstance(r, APIRoute)
    }


async def test_one_answer_per_unique_part_with_its_product(committing_client, db_session):
    farm, screw, nut = await _farm_with_purchases(db_session)
    r = await committing_client.get(_query(nut.id, farm.body.id, screw.id, 999_999, farm.body.id))
    assert r.status_code == 200, r.text
    answers = {row["part_id"]: row for row in r.json()}
    assert len(r.json()) == len(answers) == 4
    assert answers[farm.body.id]["product_id"] == farm.product.id
    assert answers[farm.body.id]["image"]["kind"] == "render" and answers[farm.body.id]["image"]["status"] == "ready"
    assert answers[screw.id]["product_id"] == farm.product.id and answers[screw.id]["image"]["kind"] == "photo"
    # A part without a picture names its product; a part that does not exist names nothing.
    assert answers[nut.id] == {"part_id": nut.id, "product_id": farm.product.id, "image": None}
    assert answers[999_999] == {"part_id": 999_999, "product_id": None, "image": None}


async def test_the_answer_is_what_every_other_surface_shows(committing_client, db_session):
    farm, screw, _nut = await _farm_with_purchases(db_session)
    product = (await committing_client.get(f"/api/v1/products/{farm.product.id}")).json()
    shown = {p["id"]: p["image"] for p in product["parts"]}
    answers = {row["part_id"]: row["image"] for row in (await committing_client.get(_query(*shown))).json()}
    assert answers == shown


async def test_a_removed_photo_reads_back_as_none(committing_client, db_session):
    farm, screw, _nut = await _farm_with_purchases(db_session)
    await part_images.clear_photo(db_session, screw.id)
    await db_session.commit()
    (answer,) = (await committing_client.get(_query(screw.id))).json()
    assert answer == {"part_id": screw.id, "product_id": farm.product.id, "image": None}


async def test_nothing_asked_is_an_empty_answer_without_a_read(committing_client, monkeypatch):
    async def never(*args, **kwargs):
        raise AssertionError("an empty batch reads nothing")

    monkeypatch.setattr(part_images, "_load", never)
    r = await committing_client.get(URL)
    assert r.status_code == 200 and r.json() == []


@pytest.mark.parametrize(
    "query",
    [
        "&".join(f"part_ids={i}" for i in range(1, 202)),  # 201: past the batch cap (spec R4)
        "part_ids=0",
        "part_ids=-3",
        f"part_ids={2**31}",  # past INTEGER on PostgreSQL
        "part_ids=abc",
        "part_ids=1.5",
    ],
)
async def test_a_batch_outside_its_bounds_is_refused_by_validation(committing_client, query):
    r = await committing_client.get(f"{URL}?{query}")
    assert r.status_code == 422, r.text
    assert isinstance(r.json()["detail"], list)  # the validator's answer, not a sentence of ours (O8)


async def test_two_hundred_is_still_one_batch(committing_client, db_session):
    farm = await rendered_farm(db_session)
    ids = [farm.body.id] + list(range(10_000, 10_199))
    r = await committing_client.get(_query(*ids))
    assert r.status_code == 200 and len(r.json()) == 200


async def test_the_reads_do_not_grow_with_the_batch(committing_client, db_session, test_engine):
    farm, screw, nut = await _farm_with_purchases(db_session)
    statements: list[str] = []

    def count(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    await committing_client.get(_query(farm.body.id))  # the first request warms the auth caches
    event.listen(test_engine.sync_engine, "before_cursor_execute", count)
    try:
        await committing_client.get(_query(farm.body.id))
        one = len(statements)
        statements.clear()
        await committing_client.get(_query(farm.body.id, farm.lid.id, screw.id, nut.id))
        many = len(statements)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", count)
    assert many == one


async def test_a_resolver_that_fails_never_answers_no_pictures(committing_client, db_session, monkeypatch):
    """O7: unlike a business answer's best-effort picture, this read has nothing else to say."""
    farm = await rendered_farm(db_session)

    async def broken(*args, **kwargs):
        raise RuntimeError("picture lookup failed")

    monkeypatch.setattr(part_images, "_load", broken)
    with pytest.raises(RuntimeError, match="picture lookup failed"):
        await committing_client.get(_query(farm.body.id))


@pytest.mark.parametrize("permission", ["orders:read", "stock:read", "products:read"])
async def test_a_reader_of_any_domain_may_ask(committing_client, db_session, permission):
    farm = await rendered_farm(db_session)
    await _user(db_session, f"pir_{permission.split(':')[0]}", [permission])
    r = await committing_client.get(_query(farm.body.id), headers=await _jwt(f"pir_{permission.split(':')[0]}"))
    assert r.status_code == 200, r.text
    assert r.json()[0]["image"]["status"] == "ready"


async def test_a_reader_of_none_of_them_is_refused(committing_client, db_session):
    farm = await rendered_farm(db_session)
    await _user(db_session, "pir_printers", ["printers:read"])
    r = await committing_client.get(_query(farm.body.id), headers=await _jwt("pir_printers"))
    assert r.status_code == 403


async def test_a_media_token_alone_opens_no_json(committing_client, db_session):
    farm = await rendered_farm(db_session)
    token = await create_media_token(authority=await mint_test_authority(username="test_admin"))
    request = committing_client.build_request("GET", _query(farm.body.id) + f"&token={token}")
    request.headers.pop("Authorization", None)
    assert (await committing_client.send(request)).status_code == 401


@pytest.mark.parametrize(
    ("owner_permissions", "flags", "allowed"),
    [
        (["stock:read"], {"can_read_status": True}, True),
        (["stock:read"], {"can_read_status": False}, False),
        (["printers:read"], {"can_read_status": True}, False),  # the key never exceeds its owner
    ],
)
async def test_an_api_key_needs_its_scope_and_an_owner_who_may_read(
    committing_client, db_session, owner_permissions, flags, allowed
):
    farm = await rendered_farm(db_session)
    owner = await _user(
        db_session, f"pir_key_{len(owner_permissions[0])}_{flags['can_read_status']}", owner_permissions
    )
    raw = await _key(db_session, owner, **flags)
    request = committing_client.build_request("GET", _query(farm.body.id))
    request.headers.pop("Authorization", None)
    request.headers["X-API-Key"] = raw
    r = await committing_client.send(request)
    assert r.status_code == (200 if allowed else 403), r.text
