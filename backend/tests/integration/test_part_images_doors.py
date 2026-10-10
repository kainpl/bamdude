"""Every door of spec §12.3 says so after its commit and never after a rollback (plan E4, task 32).

Three kinds of doors, three ways to prove "never after a rollback":
* a route whose module never commits (get_db does): the same request through ``async_client``,
  whose session closes without a commit;
* a service in the caller's transaction: the caller rolls back;
* a service that commits inside: its commit fails.
"""

import pytest

from backend.app.services import part_images
from backend.app.services.library_trash import library_trash_service
from backend.app.services.product_sync import purge_file_product_links, sync_product_for_file
from backend.tests.fixtures.part_images import rendered_farm

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def events(monkeypatch):
    from backend.app.core.websocket import ws_manager

    sent: list[dict] = []

    async def record(message):
        sent.append(message)

    monkeypatch.setattr(ws_manager, "broadcast", record)
    return sent


def products_in(events) -> set[int]:
    return {pid for e in events for pid in e["data"]["product_ids"]}


ROUTE_DOORS = {
    "create_part": lambda f: ("POST", f"/api/v1/products/{f.product.id}/parts", {"kind": "printed", "name": "Cube"}),
    "update_part": lambda f: (
        "PATCH",
        f"/api/v1/products/{f.product.id}/parts/{f.lid.id}",
        {"aliases": ["lid", "cube"]},
    ),
    "add_alias": lambda f: ("POST", f"/api/v1/products/{f.product.id}/parts/{f.lid.id}/aliases", {"name_key": "cube"}),
    "merge": lambda f: (
        "POST",
        f"/api/v1/products/{f.product.id}/parts/{f.body.id}/merge",
        {"source_part_id": f.lid.id},
    ),
    "delete_part": lambda f: ("DELETE", f"/api/v1/products/{f.product.id}/parts/{f.lid.id}", None),
    "delete_product": lambda f: ("DELETE", f"/api/v1/products/{f.product.id}", None),
    "unlink_file": lambda f: ("DELETE", f"/api/v1/products/{f.product.id}/files/{f.file.id}", None),
    # full: the farm's plate is ready -- only a full re-render moves it, and only a move is an event (D23)
    "rerender": lambda f: ("POST", f"/api/v1/products/{f.product.id}/part-images/rerender", {"full": True}),
    "choose_auto": lambda f: ("PUT", f"/api/v1/product-parts/{f.body.id}/image", {"source": "auto"}),
}


async def _through(client, db_session, events, door) -> bool:
    farm = await rendered_farm(db_session)
    product_id = farm.product.id
    method, url, body = ROUTE_DOORS[door](farm)
    r = await client.request(method, url, json=body)
    assert r.status_code < 300, r.text
    await part_images.drain()
    return product_id in products_in(events)


# Two tests, not one with getfixturevalue: an async client fixture cannot be set up from inside the running loop.
@pytest.mark.parametrize("door", sorted(ROUTE_DOORS))
async def test_a_route_door_says_so_after_its_commit(committing_client, db_session, events, door):
    assert await _through(committing_client, db_session, events, door)


@pytest.mark.parametrize("door", sorted(ROUTE_DOORS))
async def test_a_route_door_says_nothing_without_a_commit(async_client, db_session, events, door):
    assert not await _through(async_client, db_session, events, door)


async def test_remove_alias(committing_client, db_session, events):
    farm = await rendered_farm(db_session)
    farm.lid.aliases = ["lid", "cap"]
    await db_session.commit()
    r = await committing_client.delete(
        f"/api/v1/products/{farm.product.id}/parts/{farm.lid.id}/aliases", params={"name_key": "cap"}
    )
    assert r.status_code == 200, r.text
    await part_images.drain()
    assert farm.product.id in products_in(events)


SERVICE_DOORS = {
    "sync_link": lambda db, f: sync_product_for_file(db, library_file_id=f.file.id, product_ids=[f.product.id]),
    "purge_links": lambda db, f: purge_file_product_links(db, [f.file.id]),
    "trash": lambda db, f: library_trash_service.trash_or_purge(db, f.file),
}


@pytest.mark.parametrize("door", sorted(SERVICE_DOORS))
@pytest.mark.parametrize("commits", [True, False], ids=["commit", "rollback"])
async def test_a_service_door_in_the_callers_transaction(db_session, events, door, commits):
    farm = await rendered_farm(db_session)
    product_id = farm.product.id  # read now: a rollback expires every row
    await SERVICE_DOORS[door](db_session, farm)
    if commits:
        await db_session.commit()
    else:
        await db_session.rollback()
    await part_images.drain()
    assert (product_id in products_in(events)) is commits


@pytest.mark.parametrize("door", ["restore", "purge_older_than"])
@pytest.mark.parametrize("commits", [True, False], ids=["commit", "failed-commit"])
async def test_a_service_door_that_commits_inside(db_session, events, monkeypatch, door, commits):
    from datetime import datetime, timedelta, timezone

    from backend.app.services import part_renders

    farm = await rendered_farm(db_session)
    if door == "restore":
        farm.file.deleted_at = part_renders.utcnow()
    else:
        farm.file.created_at = datetime.now(timezone.utc) - timedelta(days=400)  # as the purge tests stamp it
    await db_session.commit()
    product_id = farm.product.id  # read now: a rollback expires every row
    events.clear()
    if not commits:

        async def failing_commit():
            raise RuntimeError("disk full")

        monkeypatch.setattr(db_session, "commit", failing_commit)
    call = (
        library_trash_service.restore(db_session, farm.file)
        if door == "restore"
        else library_trash_service.purge_older_than(db_session, 30, include_never_printed=True)
    )
    if commits:
        await call
    else:
        with pytest.raises(RuntimeError):
            await call
        monkeypatch.undo()
        await db_session.rollback()
    await part_images.drain()
    assert (product_id in products_in(events)) is commits


@pytest.mark.parametrize("commits", [True, False], ids=["commit", "rollback"])
async def test_the_dedupe_of_an_ingest_marks_the_loser_products(db_session, events, commits):
    """library_ingest.trash_duplicate_rows trashes the duplicate rows of one content in the caller's transaction."""
    from backend.app.services import library_ingest

    farm = await rendered_farm(db_session)
    twin = await rendered_farm(db_session, name="Twin", status=None)  # same hash, another row and product
    product_ids = {farm.product.id, twin.product.id}  # read now: a rollback expires every row
    _groups, trashed = await library_ingest.trash_duplicate_rows(db_session)
    assert trashed == 1
    if commits:
        await db_session.commit()
    else:
        await db_session.rollback()
    await part_images.drain()
    assert bool(product_ids & products_in(events)) is commits
