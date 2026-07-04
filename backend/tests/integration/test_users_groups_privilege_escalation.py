"""Privilege-escalation regression suite for the users/groups admin boundary.

USERS_* / GROUPS_* are admin-level capabilities. The original implementation
enforced ONLY the permission, not admin role — so any user holding
``users:update`` (or ``users:create`` / ``groups:update`` / ``groups:create``)
could grant themselves admin through the management routes.

This suite pins the fail-closed behaviour: every write route in ``users.py`` /
``groups.py`` now carries ``RequireAdmin()`` on top of the permission gate, and
system-group permission sets can't be rewritten even by an admin. Each negative
test grants the operator the minimum permission needed to *reach* the route gate,
then asserts the admin gate blocks them; companion positive tests verify the same
operation still succeeds for a real admin (so the gate doesn't over-block).

Ported/adapted from upstream Bambuddy security-hardening #1. Uses BamDude's
always-on auth fixtures (``async_client`` pre-seeds ``test_admin`` + the default
Administrators / Operators / Viewers groups); admin auth is a ``test_admin`` JWT.
"""

import secrets

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.core.auth import create_access_token
from backend.app.core.permissions import ALL_PERMISSIONS
from backend.app.models.group import Group
from backend.app.models.user import User

# Random per-run credential — tests exercise the admin gate, not password
# handling. The ``Aa1!`` prefix satisfies the complexity validator (upper +
# lower + digit + symbol); the random body keeps literals out of the source.
_PW = "Aa1!" + secrets.token_urlsafe(12)  # pragma: allowlist secret


def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {create_access_token(data={'sub': 'test_admin'})}"}


async def _make_operator(async_client: AsyncClient, *, username: str, permissions: list[str]) -> tuple[str, int]:
    """Create a non-admin user in a custom group carrying exactly ``permissions``.

    Returns ``(operator_token, user_id)``. The operator is NOT admin and NOT in
    Administrators — they hold ONLY the listed permission strings.
    """
    headers = _admin_headers()
    grp = await async_client.post(
        "/api/v1/groups/",
        headers=headers,
        json={"name": f"esc_{username}", "permissions": permissions},
    )
    assert grp.status_code == 201, grp.text
    gid = grp.json()["id"]

    user = await async_client.post(
        "/api/v1/users/",
        headers=headers,
        json={"username": username, "password": _PW, "role": "user", "group_ids": [gid]},
    )
    assert user.status_code == 201, user.text
    assert user.json()["is_admin"] is False
    uid = user.json()["id"]

    login = await async_client.post("/api/v1/auth/login", json={"username": username, "password": _PW})
    assert login.status_code == 200, login.text
    return login.json()["access_token"], uid


async def _admin_group_id(db_session) -> int:
    result = await db_session.execute(select(Group).where(Group.name == "Administrators"))
    return result.scalar_one().id


# ---------------------------------------------------------------------------
# 1. PATCH /users/{id} {role: "admin"} — USERS_UPDATE holder can't self-promote
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_users_update_holder_cannot_set_role_to_admin(async_client: AsyncClient, db_session):
    token, uid = await _make_operator(async_client, username="op_update", permissions=["users:update"])
    resp = await async_client.patch(
        f"/api/v1/users/{uid}",
        headers={"Authorization": f"Bearer {token}"},
        json={"role": "admin"},
    )
    assert resp.status_code == 403, resp.text
    # DB unchanged — still not admin.
    user = (await db_session.execute(select(User).where(User.id == uid))).scalar_one()
    assert user.role != "admin"


@pytest.mark.asyncio
async def test_users_update_holder_cannot_target_other_user(async_client: AsyncClient, db_session):
    token, _ = await _make_operator(async_client, username="op_update2", permissions=["users:update"])
    target = await async_client.post(
        "/api/v1/users/",
        headers=_admin_headers(),
        json={"username": "target", "password": _PW, "role": "user"},
    )
    assert target.status_code == 201, target.text
    tid = target.json()["id"]
    resp = await async_client.patch(
        f"/api/v1/users/{tid}",
        headers={"Authorization": f"Bearer {token}"},
        json={"role": "admin"},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 2. POST /users {role: "admin"} — USERS_CREATE holder can't mint an admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_users_create_holder_cannot_create_admin(async_client: AsyncClient, db_session):
    token, _ = await _make_operator(async_client, username="op_create", permissions=["users:create"])
    resp = await async_client.post(
        "/api/v1/users/",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": "newadmin", "password": _PW, "role": "admin"},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 3. PATCH /groups/{id} {permissions: ALL} — GROUPS_UPDATE holder can't rewrite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_groups_update_holder_cannot_rewrite_permissions(async_client: AsyncClient, db_session):
    token, _ = await _make_operator(async_client, username="op_grpupd", permissions=["groups:update"])
    create = await async_client.post(
        "/api/v1/groups/",
        headers=_admin_headers(),
        json={"name": "innocent", "permissions": ["printers:read"]},
    )
    assert create.status_code == 201, create.text
    gid = create.json()["id"]
    resp = await async_client.patch(
        f"/api/v1/groups/{gid}",
        headers={"Authorization": f"Bearer {token}"},
        json={"permissions": ALL_PERMISSIONS},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 4. POST /groups {permissions: ALL} — GROUPS_CREATE holder can't mint shadow-admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_groups_create_holder_cannot_create_admin_equivalent(async_client: AsyncClient, db_session):
    token, _ = await _make_operator(async_client, username="op_grpcreate", permissions=["groups:create"])
    resp = await async_client.post(
        "/api/v1/groups/",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "shadowadmins", "permissions": ALL_PERMISSIONS},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 5. POST /groups/{admin}/users/{self} — GROUPS_UPDATE holder can't self-add to Admins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_groups_update_holder_cannot_self_add_to_administrators(async_client: AsyncClient, db_session):
    token, uid = await _make_operator(async_client, username="op_selfadd", permissions=["groups:update"])
    admin_gid = await _admin_group_id(db_session)
    resp = await async_client.post(
        f"/api/v1/groups/{admin_gid}/users/{uid}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 6. Even an admin can't strip a system group's permissions (DoS guard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_cannot_strip_administrators_group_permissions(async_client: AsyncClient, db_session):
    admin_gid = await _admin_group_id(db_session)
    resp = await async_client.patch(
        f"/api/v1/groups/{admin_gid}",
        headers=_admin_headers(),
        json={"permissions": []},
    )
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# 7. Positive — admin CAN still change a user's role
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_can_still_perform_user_role_change(async_client: AsyncClient, db_session):
    target = await async_client.post(
        "/api/v1/users/",
        headers=_admin_headers(),
        json={"username": "promoteme", "password": _PW, "role": "user"},
    )
    assert target.status_code == 201, target.text
    tid = target.json()["id"]
    resp = await async_client.patch(
        f"/api/v1/users/{tid}",
        headers=_admin_headers(),
        json={"role": "admin"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "admin"


# ---------------------------------------------------------------------------
# 8. Positive — admin status via Administrators-group membership passes the gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_administrators_group_member_passes_admin_gate(async_client: AsyncClient, db_session):
    """A user made admin by Administrators-group membership (role still 'user')
    must pass the admin gate — the gate checks ``is_admin``, not the bare role."""
    headers = _admin_headers()
    admin_gid = await _admin_group_id(db_session)
    user_resp = await async_client.post(
        "/api/v1/users/",
        headers=headers,
        json={"username": "groupadmin", "password": _PW, "role": "user"},
    )
    assert user_resp.status_code == 201, user_resp.text
    uid = user_resp.json()["id"]
    add = await async_client.post(f"/api/v1/groups/{admin_gid}/users/{uid}", headers=headers)
    assert add.status_code == 204, add.text

    target_resp = await async_client.post(
        "/api/v1/users/",
        headers=headers,
        json={"username": "target_member", "password": _PW, "role": "user"},
    )
    assert target_resp.status_code == 201, target_resp.text
    tid = target_resp.json()["id"]

    login = await async_client.post("/api/v1/auth/login", json={"username": "groupadmin", "password": _PW})
    assert login.status_code == 200, login.text
    gtoken = login.json()["access_token"]

    resp = await async_client.patch(
        f"/api/v1/users/{tid}",
        headers={"Authorization": f"Bearer {gtoken}"},
        json={"is_active": False},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 9. Positive — read endpoints stay delegable to non-admins (USERS_READ only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_users_read_remains_delegable_to_non_admin(async_client: AsyncClient, db_session):
    """Operator UIs (Stats filter-by-user, Print-Log username column) consume
    GET /users via a custom ``users:read`` grant — the admin gate applies only
    to writes, not reads."""
    token, _ = await _make_operator(async_client, username="op_reader", permissions=["users:read"])
    resp = await async_client.get("/api/v1/users/", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
