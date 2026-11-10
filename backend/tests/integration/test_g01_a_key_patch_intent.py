"""Explicit null is a key-manager scope change; omission is not a change."""

import pytest


@pytest.mark.parametrize("cleared_scope", [[], None])
async def test_key_manager_explicit_empty_vs_null_has_intentional_effective_scope(
    async_client, printer_factory, cleared_scope
):
    first, second = await printer_factory(), await printer_factory()
    response = await async_client.post(
        "/api/v1/api-keys/",
        json={"name": "Explicit scope change", "printer_ids": [first.id], "can_read_status": True},
    )
    assert response.status_code == 200, response.text
    key = response.json()
    path = f"/api/v1/api-keys/{key['id']}"
    headers = {"X-API-Key": key["key"], "Authorization": ""}
    omitted = await async_client.patch(path, json={"name": "Omission preserves scope"})
    assert omitted.status_code == 200
    assert omitted.json()["printer_ids"] == [first.id]
    assert (await async_client.get(f"/api/v1/printers/{first.id}/status", headers=headers)).status_code == 200
    assert (await async_client.get(f"/api/v1/printers/{second.id}/status", headers=headers)).status_code == 403

    explicit = await async_client.patch(path, json={"printer_ids": cleared_scope})
    assert explicit.status_code == 200, explicit.text
    assert explicit.json()["printer_ids"] == cleared_scope
    for printer in (first, second):
        status = await async_client.get(f"/api/v1/printers/{printer.id}/status", headers=headers)
        assert status.status_code == (200 if cleared_scope is None else 403), status.text
