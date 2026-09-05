"""The four split settings round-trip, validate, and guard the entities they name."""

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_defaults_and_round_trip(async_client):
    before = (await async_client.get("/api/v1/settings/")).json()
    assert before["stagger_split_by_tags"] is False
    assert before["stagger_group_tag_ids"] == "[]"
    assert before["stagger_split_by_location"] is False
    assert before["stagger_group_location_ids"] == "[]"

    rsp = await async_client.put(
        "/api/v1/settings/", json={"stagger_split_by_tags": True, "stagger_group_tag_ids": "[3, 1, 3]"}
    )
    assert rsp.status_code == 200, rsp.text
    after = (await async_client.get("/api/v1/settings/")).json()
    assert after["stagger_split_by_tags"] is True
    assert after["stagger_group_tag_ids"] == "[1, 3]"  # sorted, de-duplicated


# Both keys, because the validator is registered on both names and a
# single-key test would pass with either one of them left out.
@pytest.mark.parametrize("key", ["stagger_group_tag_ids", "stagger_group_location_ids"])
@pytest.mark.parametrize("bad", ["nope", '{"a": 1}', "[true]", '["1"]'])
async def test_a_non_array_is_422(async_client, key, bad):
    assert (await async_client.put("/api/v1/settings/", json={key: bad})).status_code == 422


async def test_a_tag_that_is_a_group_is_badged_and_cannot_be_deleted(async_client):
    tag = (await async_client.post("/api/v1/printer-tags", json={"name": "Фаза 1"})).json()
    await async_client.put("/api/v1/settings/", json={"stagger_group_tag_ids": f"[{tag['id']}]"})

    listed = (await async_client.get("/api/v1/printer-tags")).json()["tags"]
    assert next(t for t in listed if t["id"] == tag["id"])["is_stagger_group"] is True
    rsp = await async_client.delete(f"/api/v1/printer-tags/{tag['id']}")
    assert rsp.status_code == 409
    assert "Staggered start" in rsp.json()["detail"]

    await async_client.put("/api/v1/settings/", json={"stagger_group_tag_ids": "[]"})
    assert (await async_client.delete(f"/api/v1/printer-tags/{tag['id']}")).status_code == 200


async def test_a_location_that_is_a_group_cannot_be_deleted(async_client):
    loc = (await async_client.post("/api/v1/printer-locations", json={"name": "Цех A"})).json()
    await async_client.put("/api/v1/settings/", json={"stagger_group_location_ids": f"[{loc['id']}]"})
    assert (await async_client.delete(f"/api/v1/printer-locations/{loc['id']}")).status_code == 409


async def test_the_resolver_loads_its_groups_from_real_rows(async_client, db_session):
    """``load`` against the tables — the unit tests hand the resolver its dictionaries ready-made.

    One printer sits two levels under a picked location and carries a picked
    tag; the other carries neither, so it is a wildcard on both axes at once.
    A stale location id is left in Settings to prove ``load`` drops what no
    longer exists.
    """
    from backend.app.models.printer import Printer
    from backend.app.services.printer_tag_service import replace_links
    from backend.app.services.stagger_groups import StaggerGroupResolver, StaggerSplit

    tag1 = (await async_client.post("/api/v1/printer-tags", json={"name": "Фаза 1"})).json()["id"]
    tag2 = (await async_client.post("/api/v1/printer-tags", json={"name": "Фаза 2"})).json()["id"]
    parent = (await async_client.post("/api/v1/printer-locations", json={"name": "Цех A"})).json()["id"]
    child = (await async_client.post("/api/v1/printer-locations", json={"name": "Ряд 1", "parent_id": parent})).json()[
        "id"
    ]

    tagged = Printer(name="p1", ip_address="10.0.0.5", serial_number="S-1", access_code="1", location_id=child)
    bare = Printer(name="p2", ip_address="10.0.0.6", serial_number="S-2", access_code="1")
    db_session.add_all([tagged, bare])
    await db_session.commit()
    await replace_links(db_session, tagged.id, [tag1])
    await db_session.commit()

    rsp = await async_client.put(
        "/api/v1/settings/",
        json={
            "stagger_split_by_tags": True,
            "stagger_group_tag_ids": f"[{tag1}, {tag2}]",
            "stagger_split_by_location": True,
            "stagger_group_location_ids": f"[{parent}, 9999]",
        },
    )
    assert rsp.status_code == 200, rsp.text

    # The route committed on its own session; end this one's read transaction so
    # the next query starts fresh against what the route wrote.
    await db_session.commit()
    split = await StaggerSplit.from_settings(db_session)
    resolver = await StaggerGroupResolver.load(db_session, split)

    assert resolver.tags_split and resolver.location_split
    assert resolver.groups_for(tagged.id) == {(tag1, parent)}  # own tag; Ряд 1 → Цех A
    assert resolver.groups_for(bare.id) == {(tag1, parent), (tag2, parent)}  # wildcard on both axes
    assert resolver.is_wildcard(bare.id) is True
    assert resolver.is_wildcard(tagged.id) is False
    assert resolver.universe == {(tag1, parent), (tag2, parent)}  # 9999 is nobody's group
    assert resolver.label((tag1, parent)) == "Фаза 1 · Цех A"
