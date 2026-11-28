"""Who may talk to a printer's WLED (N02): ``printers:update`` and the printer's own scope (N62).

A person needs ``printers:update`` (a Viewer gets 403) and a printer their limited group reaches (another printer
answers 404 — the gate's answer for a printer outside the caller's reach). ``printers:update`` is denied to API keys
altogether, so a key gets 403 whatever printers it lists.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

PASSWORD = "UserPass1!"
ENDPOINTS = (
    ("presets", {"base_url": "http://wled.local"}),
    ("test-connection", {"base_url": "http://wled.local"}),
    ("test-preset", {"base_url": "http://wled.local", "preset_id": 3}),
)


async def _group_id(client: AsyncClient, name: str) -> int:
    response = await client.get("/api/v1/groups/")
    assert response.status_code == 200, response.text
    return next(g["id"] for g in response.json() if g["name"] == name)


async def _login(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    token = response.json().get("access_token")
    assert token, response.text
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def farm(async_client, printer_factory):
    a = await printer_factory(name="A")
    b = await printer_factory(name="B")
    team = await async_client.post(
        "/api/v1/groups/",
        json={
            "name": "WLED team",
            "permissions": ["printers:read", "printers:update"],
            "restrict_printers": True,
            "printer_ids": [a.id],
        },
    )
    assert team.status_code == 201, team.text
    alice = await async_client.post(
        "/api/v1/users/",
        json={"username": "alice", "password": PASSWORD, "role": "user", "group_ids": [team.json()["id"]]},
    )
    assert alice.status_code in (200, 201), alice.text
    viewers = await _group_id(async_client, "Viewers")
    bob = await async_client.post(
        "/api/v1/users/",
        json={"username": "bob", "password": PASSWORD, "role": "user", "group_ids": [viewers]},
    )
    assert bob.status_code in (200, 201), bob.text
    key = await async_client.post(
        "/api/v1/api-keys/", json={"name": "k", "can_control_printer": True, "printer_ids": [a.id]}
    )
    assert key.status_code == 200, key.text
    return {
        "a": a,
        "b": b,
        "alice": await _login(async_client, "alice"),
        "bob": await _login(async_client, "bob"),
        "key": {"X-API-Key": key.json()["key"]},
    }


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
async def test_a_printer_outside_the_persons_reach_is_not_there(async_client, farm, path, body):
    response = await async_client.post(f"/api/v1/printers/{farm['b'].id}/wled/{path}", json=body, headers=farm["alice"])
    assert response.status_code == 404, response.text


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
async def test_a_person_without_printers_update_is_refused(async_client, farm, path, body):
    response = await async_client.post(f"/api/v1/printers/{farm['a'].id}/wled/{path}", json=body, headers=farm["bob"])
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(("path", "body"), ENDPOINTS)
async def test_a_key_is_refused_whatever_it_lists(async_client, farm, path, body):
    response = await async_client.post(f"/api/v1/printers/{farm['a'].id}/wled/{path}", json=body, headers=farm["key"])
    assert response.status_code == 403, response.text


async def test_the_person_reaches_their_own_printer(async_client, farm):
    from unittest.mock import AsyncMock, patch

    with patch(
        "backend.app.api.routes.printers.wled_manager.get_info",
        new=AsyncMock(return_value={"name": None, "version": None}),
    ):
        response = await async_client.post(
            f"/api/v1/printers/{farm['a'].id}/wled/test-connection",
            json={"base_url": "http://wled.local"},
            headers=farm["alice"],
        )
    assert response.status_code == 200, response.text
