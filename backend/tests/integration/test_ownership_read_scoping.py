"""Ownership read-split regression suite (maziggy/bambuddy-security #2).

The archive/library/queue read routes gate on the ``*:read_own`` / ``*:read_all``
split. A caller holding only ``archives:read_own`` must see ONLY their own rows;
ownerless rows require ``read_all``. Detail routes 404 (not 403) on a row the
caller can't see, so ids can't be enumerated.

BamDude ships Operators/Viewers with ``read_all`` (shared farm — see
``permissions.DEFAULT_GROUPS``), so these tests use a purpose-built custom
``read_own`` group to exercise the scoped path.
"""

import secrets

import pytest
from httpx import AsyncClient

from backend.app.core.auth import create_access_token

_PW = "Aa1!" + secrets.token_urlsafe(12)  # pragma: allowlist secret


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {create_access_token(data={'sub': 'test_admin'})}"}


async def _make_user(async_client: AsyncClient, *, username: str, permissions: list[str]) -> tuple[str, int]:
    headers = _admin_headers()
    grp = await async_client.post(
        "/api/v1/groups/", headers=headers, json={"name": f"rs_{username}", "permissions": permissions}
    )
    assert grp.status_code == 201, grp.text
    gid = grp.json()["id"]
    user = await async_client.post(
        "/api/v1/users/",
        headers=headers,
        json={"username": username, "password": _PW, "role": "user", "group_ids": [gid]},
    )
    assert user.status_code == 201, user.text
    uid = user.json()["id"]
    login = await async_client.post("/api/v1/auth/login", json={"username": username, "password": _PW})
    assert login.status_code == 200, login.text
    return login.json()["access_token"], uid


@pytest.mark.asyncio
async def test_read_own_lists_only_own_archives(async_client: AsyncClient, printer_factory, archive_factory):
    token_a, uid_a = await _make_user(async_client, username="rown_a", permissions=["archives:read_own"])
    _, uid_b = await _make_user(async_client, username="rown_b", permissions=["archives:read_own"])
    printer = await printer_factory()

    a_arc = await archive_factory(printer.id, print_name="A print", created_by_id=uid_a)
    await archive_factory(printer.id, print_name="B print", created_by_id=uid_b)
    await archive_factory(printer.id, print_name="Ownerless print", created_by_id=None)

    resp = await async_client.get("/api/v1/archives/", headers={"Authorization": f"Bearer {token_a}"})
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]
    ids = {row["id"] for row in items}
    assert a_arc.id in ids
    assert len(ids) == 1  # only A's own row — not B's, not the ownerless one


@pytest.mark.asyncio
async def test_read_own_cannot_get_other_users_archive(async_client: AsyncClient, printer_factory, archive_factory):
    token_a, _ = await _make_user(async_client, username="rown_c", permissions=["archives:read_own"])
    _, uid_b = await _make_user(async_client, username="rown_d", permissions=["archives:read_own"])
    printer = await printer_factory()
    b_arc = await archive_factory(printer.id, print_name="B private", created_by_id=uid_b)

    resp = await async_client.get(f"/api/v1/archives/{b_arc.id}", headers={"Authorization": f"Bearer {token_a}"})
    # 404, not 403 — id existence is not leaked.
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_read_own_can_get_own_archive(async_client: AsyncClient, printer_factory, archive_factory):
    token_a, uid_a = await _make_user(async_client, username="rown_e", permissions=["archives:read_own"])
    printer = await printer_factory()
    a_arc = await archive_factory(printer.id, print_name="A own detail", created_by_id=uid_a)
    resp = await async_client.get(f"/api/v1/archives/{a_arc.id}", headers={"Authorization": f"Bearer {token_a}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == a_arc.id


@pytest.mark.asyncio
async def test_read_all_lists_every_archive(async_client: AsyncClient, printer_factory, archive_factory):
    token, _ = await _make_user(async_client, username="rall_a", permissions=["archives:read_all"])
    _, uid_b = await _make_user(async_client, username="rall_b", permissions=["archives:read_own"])
    printer = await printer_factory()
    await archive_factory(printer.id, print_name="X", created_by_id=uid_b)
    await archive_factory(printer.id, print_name="Y ownerless", created_by_id=None)

    resp = await async_client.get("/api/v1/archives/", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]
    assert len(items) >= 2  # sees other users' + ownerless rows


@pytest.mark.asyncio
async def test_default_groups_have_farm_wide_reads(async_client: AsyncClient):
    """Operators + Viewers get read_all (shared-farm policy); Operators get
    orca_cloud:auth. Proves DEFAULT_GROUPS seed + the m091 backfill."""
    resp = await async_client.get("/api/v1/groups/", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    by_name = {g["name"]: set(g["permissions"]) for g in resp.json()}
    for grp in ("Operators", "Viewers"):
        assert "archives:read_all" in by_name[grp], grp
        assert "queue:read_all" in by_name[grp], grp
        assert "library:read_all" in by_name[grp], grp
    assert "orca_cloud:auth" in by_name["Operators"]
    assert "orca_cloud:auth" not in by_name["Viewers"]
