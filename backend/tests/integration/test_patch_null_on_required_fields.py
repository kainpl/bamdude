"""An explicit null for a field that cannot be empty leaves it unchanged (upstream 8e5116fe).

These routes apply what was sent with ``model_dump(exclude_unset=True)`` + ``setattr``; a JSON
null used to go straight into a NOT NULL column and the request ended in a 500. The update
schema now drops such a null before the route sees it; a null for a column that may be empty
still clears it.
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.integration


async def test_external_link(async_client: AsyncClient):
    created = await async_client.post(
        "/api/v1/external-links/", json={"name": "Wiki", "url": "https://example.com", "icon": "link"}
    )
    assert created.status_code == 200, created.text
    link_id = created.json()["id"]

    kept = await async_client.patch(
        f"/api/v1/external-links/{link_id}", json={"name": None, "url": None, "open_in_new_tab": None}
    )

    assert kept.status_code == 200, kept.text
    assert (kept.json()["name"], kept.json()["url"]) == ("Wiki", "https://example.com")


async def test_maintenance_type_and_item(async_client: AsyncClient, printer_factory):
    printer = await printer_factory()
    created = await async_client.post(
        "/api/v1/maintenance/types",
        json={"name": "Grease rods", "default_interval_hours": 50, "wiki_url": "https://example.com/w"},
    )
    assert created.status_code == 200, created.text
    type_id = created.json()["id"]

    kept = await async_client.patch(f"/api/v1/maintenance/types/{type_id}", json={"name": None})
    cleared = await async_client.patch(f"/api/v1/maintenance/types/{type_id}", json={"wiki_url": None})

    assert kept.status_code == 200, kept.text
    assert kept.json()["name"] == "Grease rods"
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["wiki_url"] is None

    assigned = await async_client.post(f"/api/v1/maintenance/printers/{printer.id}/assign/{type_id}")
    assert assigned.status_code == 200, assigned.text
    item = await async_client.patch(f"/api/v1/maintenance/items/{assigned.json()['id']}", json={"enabled": None})

    assert item.status_code == 200, item.text
    assert item.json()["enabled"] is True


async def test_printer(async_client: AsyncClient, printer_factory):
    printer = await printer_factory(cleanup_after_print=True)

    kept = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"cleanup_after_print": None, "name": None})

    assert kept.status_code == 200, kept.text
    assert (kept.json()["cleanup_after_print"], kept.json()["name"]) == (True, "Test Printer")


async def test_archive(async_client: AsyncClient, printer_factory, archive_factory):
    printer = await printer_factory()
    archive = await archive_factory(printer.id, notes="first", quantity=3)

    kept = await async_client.patch(f"/api/v1/archives/{archive.id}", json={"quantity": None})
    cleared = await async_client.patch(f"/api/v1/archives/{archive.id}", json={"notes": None})

    assert kept.status_code == 200, kept.text
    assert kept.json()["quantity"] == 3
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["notes"] is None


async def test_notification_provider(async_client: AsyncClient, notification_provider_factory):
    provider = await notification_provider_factory(name="Phone")

    kept = await async_client.patch(f"/api/v1/notifications/{provider.id}", json={"name": None})

    assert kept.status_code == 200, kept.text
    assert kept.json()["name"] == "Phone"


async def test_smart_plug(async_client: AsyncClient, smart_plug_factory):
    plug = await smart_plug_factory(username="admin", controls_printer_power=False)

    kept = await async_client.patch(f"/api/v1/smart-plugs/{plug.id}", json={"controls_printer_power": None})

    assert kept.status_code == 200, kept.text
    assert kept.json()["controls_printer_power"] is False
