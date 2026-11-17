"""Several printers are placed -- a location, tags -- in one request, all or none."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.printer import Printer
from backend.app.models.printer_location import PrinterLocation
from backend.app.models.printer_tag import PrinterTag, PrinterTagLink

URL = "/api/v1/printers/placement"

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def farm(db_session, printer_factory):
    room = PrinterLocation(name="Room", name_key="room")
    shelf = PrinterLocation(name="Shelf", name_key="shelf")
    fast = PrinterTag(name="Fast", name_key="fast")
    abs_ = PrinterTag(name="ABS", name_key="abs")
    db_session.add_all([room, shelf, fast, abs_])
    await db_session.flush()
    printers = [await printer_factory(name=f"P{i}") for i in range(3)]
    printers[0].location_id = room.id
    db_session.add(PrinterTagLink(printer_id=printers[0].id, tag_id=fast.id))
    await db_session.commit()
    return {"room": room.id, "shelf": shelf.id, "fast": fast.id, "abs": abs_.id, "ids": [p.id for p in printers]}


async def _places(db_session) -> dict[int, int | None]:
    db_session.expire_all()
    return dict((await db_session.execute(select(Printer.id, Printer.location_id))).all())


async def _tags(db_session) -> set[tuple[int, int]]:
    return set((await db_session.execute(select(PrinterTagLink.printer_id, PrinterTagLink.tag_id))).all())


async def test_a_location_is_set_for_every_named_printer(async_client: AsyncClient, db_session, farm):
    ids = farm["ids"]

    response = await async_client.post(
        URL, json={"printer_ids": ids[:2], "set_location": True, "location_id": farm["shelf"]}
    )

    assert response.status_code == 200
    assert response.json() == {"updated": 2}
    assert await _places(db_session) == {ids[0]: farm["shelf"], ids[1]: farm["shelf"], ids[2]: None}


async def test_a_null_location_takes_them_out_of_their_place(async_client: AsyncClient, db_session, farm):
    ids = farm["ids"]

    response = await async_client.post(URL, json={"printer_ids": ids, "set_location": True, "location_id": None})

    assert response.status_code == 200
    assert set((await _places(db_session)).values()) == {None}


async def test_the_place_is_left_alone_unless_it_is_asked_for(async_client: AsyncClient, db_session, farm):
    ids = farm["ids"]

    response = await async_client.post(URL, json={"printer_ids": ids, "add_tag_ids": [farm["abs"]]})

    assert response.status_code == 200
    assert (await _places(db_session))[ids[0]] == farm["room"]


async def test_tags_are_added_and_removed_and_the_others_stay(async_client: AsyncClient, db_session, farm):
    ids = farm["ids"]

    response = await async_client.post(
        URL, json={"printer_ids": ids, "add_tag_ids": [farm["abs"]], "remove_tag_ids": [farm["fast"]]}
    )

    assert response.status_code == 200
    assert await _tags(db_session) == {(pid, farm["abs"]) for pid in ids}


async def test_a_tag_a_printer_already_carries_is_not_added_twice(async_client: AsyncClient, db_session, farm):
    ids = farm["ids"]

    response = await async_client.post(URL, json={"printer_ids": ids, "add_tag_ids": [farm["fast"]]})

    assert response.status_code == 200
    assert await _tags(db_session) == {(pid, farm["fast"]) for pid in ids}


async def test_an_unknown_printer_changes_nothing(async_client: AsyncClient, db_session, farm):
    ids = farm["ids"]
    before = (await _places(db_session), await _tags(db_session))

    response = await async_client.post(
        URL,
        json={
            "printer_ids": [*ids, 999999],
            "set_location": True,
            "location_id": farm["shelf"],
            "add_tag_ids": [farm["abs"]],
        },
    )

    assert response.status_code == 404
    assert (await _places(db_session), await _tags(db_session)) == before


async def test_an_archived_printer_is_refused_and_nothing_changes(async_client: AsyncClient, db_session, farm):
    ids = farm["ids"]
    archived = await db_session.get(Printer, ids[2])
    archived.archived = True
    await db_session.commit()
    before = await _places(db_session)

    response = await async_client.post(
        URL, json={"printer_ids": ids, "set_location": True, "location_id": farm["shelf"]}
    )

    assert response.status_code == 409
    assert await _places(db_session) == before


@pytest.mark.parametrize(
    "body",
    [
        {},  # nothing asked
        {"add_tag_ids": [1], "remove_tag_ids": [1]},  # the same tag both ways
    ],
)
async def test_a_request_that_asks_for_nothing_or_contradicts_itself_is_refused(async_client: AsyncClient, farm, body):
    response = await async_client.post(URL, json={"printer_ids": farm["ids"], **body})

    assert response.status_code == 422


async def test_a_location_or_tag_that_names_nothing_is_refused(async_client: AsyncClient, db_session, farm):
    ids = farm["ids"]
    before = await _places(db_session)

    place = await async_client.post(URL, json={"printer_ids": ids, "set_location": True, "location_id": 999999})
    tag = await async_client.post(URL, json={"printer_ids": ids, "add_tag_ids": [999999]})

    assert place.status_code == 422
    assert tag.status_code == 422
    assert await _places(db_session) == before
