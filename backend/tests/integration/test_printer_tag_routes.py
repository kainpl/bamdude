"""Printer tags travel as objects and are written as ids."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def _connection_probe():
    """Add-Printer probes MQTT before persisting; default it to success."""
    with patch(
        "backend.app.api.routes.printers.printer_manager.test_connection",
        new=AsyncMock(return_value={"success": True, "state": "IDLE", "model": "P1S"}),
    ):
        yield


async def _tag(client: AsyncClient, name="Фаза 1") -> dict:
    rsp = await client.post("/api/v1/printer-tags", json={"name": name})
    assert rsp.status_code == 201, rsp.text
    return rsp.json()


async def _printer(db_session, **extra):
    from backend.app.models.printer import Printer

    printer = Printer(name="p1", ip_address="10.0.0.5", serial_number="S-1", access_code="1", **extra)
    db_session.add(printer)
    await db_session.commit()
    await db_session.refresh(printer)
    return printer


async def _link_count(db_session) -> int:
    from backend.app.models.printer_tag import PrinterTagLink

    return (await db_session.execute(select(func.count()).select_from(PrinterTagLink))).scalar_one()


async def test_create_list_and_rename(async_client):
    tag = await _tag(async_client)

    listed = (await async_client.get("/api/v1/printer-tags")).json()["tags"]
    assert listed == [{"id": tag["id"], "name": "Фаза 1", "printer_count": 0, "is_stagger_group": False}]

    renamed = await async_client.patch(f"/api/v1/printer-tags/{tag['id']}", json={"name": " Фаза A "})
    assert renamed.status_code == 200
    assert renamed.json() == {"id": tag["id"], "name": "Фаза A"}


async def test_a_duplicate_differing_only_in_case_or_spacing_is_409(async_client):
    await _tag(async_client, "Фаза 1")
    assert (await async_client.post("/api/v1/printer-tags", json={"name": "фаза 1 "})).status_code == 409


async def test_renaming_onto_another_tag_is_409_and_onto_itself_is_not(async_client):
    a = await _tag(async_client, "A")
    await _tag(async_client, "B")
    assert (await async_client.patch(f"/api/v1/printer-tags/{a['id']}", json={"name": "b"})).status_code == 409
    assert (await async_client.patch(f"/api/v1/printer-tags/{a['id']}", json={"name": "a"})).status_code == 200


async def test_a_printer_carries_its_tags_and_is_written_by_ids(async_client, db_session):
    from backend.app.services.printer_tag_service import replace_links

    tag = await _tag(async_client)
    printer = await _printer(db_session)
    await replace_links(db_session, printer.id, [tag["id"]])
    await db_session.commit()

    listed = (await async_client.get("/api/v1/printers/")).json()
    assert listed[0]["tags"] == [{"id": tag["id"], "name": "Фаза 1"}]
    assert listed[0]["tag_ids"] == [tag["id"]]

    other = await _tag(async_client, "Фаза 2")
    rsp = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"tag_ids": [other["id"]]})
    assert rsp.status_code == 200, rsp.text
    assert [t["name"] for t in rsp.json()["tags"]] == ["Фаза 2"]
    assert rsp.json()["tag_ids"] == [other["id"]]

    counted = (await async_client.get("/api/v1/printer-tags")).json()["tags"]
    assert {t["name"]: t["printer_count"] for t in counted} == {"Фаза 1": 0, "Фаза 2": 1}


async def test_a_printer_can_be_created_with_tags(async_client, _connection_probe):
    """``tag_ids`` is not a printer column — it has to be popped before ``Printer(**data)``
    and written as links once the row has an id."""
    tag = await _tag(async_client)

    rsp = await async_client.post(
        "/api/v1/printers/",
        json={
            "name": "p1",
            "ip_address": "10.0.0.5",
            "serial_number": "S-1",
            "access_code": "12345678",
            "tag_ids": [tag["id"]],
        },
    )

    assert rsp.status_code == 200, rsp.text
    assert rsp.json()["tags"] == [{"id": tag["id"], "name": "Фаза 1"}]
    assert (await async_client.get("/api/v1/printer-tags")).json()["tags"][0]["printer_count"] == 1


async def test_creating_a_printer_with_an_unknown_tag_is_422(async_client, _connection_probe):
    rsp = await async_client.post(
        "/api/v1/printers/",
        json={
            "name": "p1",
            "ip_address": "10.0.0.5",
            "serial_number": "S-1",
            "access_code": "12345678",
            "tag_ids": [999],
        },
    )

    assert rsp.status_code == 422
    assert "999" in rsp.json()["detail"]


async def test_an_unknown_tag_id_is_422_and_writes_nothing(async_client, db_session):
    printer = await _printer(db_session)
    rsp = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"tag_ids": [999]})
    assert rsp.status_code == 422
    assert "999" in rsp.json()["detail"]
    assert await _link_count(db_session) == 0


async def test_an_explicit_null_clears_the_tags_instead_of_failing(async_client, db_session):
    """A GET → edit → PATCH client sends the field back; null reads as "no tags"."""
    from backend.app.services.printer_tag_service import replace_links

    tag = await _tag(async_client)
    printer = await _printer(db_session)
    await replace_links(db_session, printer.id, [tag["id"]])
    await db_session.commit()

    rsp = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"tag_ids": None})

    assert rsp.status_code == 200, rsp.text
    assert rsp.json()["tags"] == []
    assert await _link_count(db_session) == 0


async def test_deleting_a_tag_removes_its_links(async_client, db_session):
    from backend.app.services.printer_tag_service import replace_links

    tag = await _tag(async_client)
    printer = await _printer(db_session)
    await replace_links(db_session, printer.id, [tag["id"]])
    await db_session.commit()

    assert (await async_client.delete(f"/api/v1/printer-tags/{tag['id']}")).json() == {"deleted": tag["id"]}
    assert await _link_count(db_session) == 0
    assert (await async_client.get("/api/v1/printers/")).json()[0]["tags"] == []


async def test_deleting_a_printer_removes_its_links(async_client, db_session):
    """SQLite enforces no foreign keys, so the link rows would otherwise point at nothing."""
    from backend.app.services.printer_tag_service import replace_links

    tag = await _tag(async_client)
    printer = await _printer(db_session)
    await replace_links(db_session, printer.id, [tag["id"]])
    await db_session.commit()

    rsp = await async_client.delete(f"/api/v1/printers/{printer.id}?delete_archives=false")

    assert rsp.status_code == 200, rsp.text
    assert await _link_count(db_session) == 0


async def test_an_archived_printer_is_not_counted(async_client, db_session):
    from backend.app.services.printer_tag_service import replace_links

    tag = await _tag(async_client)
    printer = await _printer(db_session, archived=True)
    await replace_links(db_session, printer.id, [tag["id"]])
    await db_session.commit()

    assert (await async_client.get("/api/v1/printer-tags")).json()["tags"][0]["printer_count"] == 0


async def test_a_missing_tag_is_404(async_client):
    assert (await async_client.patch("/api/v1/printer-tags/42", json={"name": "x"})).status_code == 404
    assert (await async_client.delete("/api/v1/printer-tags/42")).status_code == 404
