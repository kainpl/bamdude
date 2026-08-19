"""An API key never out-ranks the user it belongs to (upstream #1894).

Scope flags are chosen when the key is minted, by whoever holds
``api_keys:create``. That is admin-only in the default groups — but a custom
group can grant it, and the flags are just booleans on a row. Without a check
against the owner, such a user could mint themselves a key carrying
``can_control_printer`` and act through it beyond their own permissions. The
allowlist is a **ceiling**, not a grant.

The same resolution does a second job: a key whose owner has been deactivated
must be dead. Returning "no owner" there would fail open — disabling a user
would leave their credentials working at full scope authority.

⚠️ Two cases look alike and must not be conflated:

- ``user_id IS NULL`` — a **legacy** key predating per-user ownership. Nothing
  to narrow against; the scope flags stand alone, exactly as before.
- ``user_id`` set but the row is gone or deactivated — 403, not "no owner".
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from backend.app.core.auth import (
    _check_apikey_permissions,
    apikey_effective_permissions,
    generate_api_key,
    resolve_apikey_owner,
)
from backend.app.core.permissions import Permission
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.user import User

pytestmark = pytest.mark.integration


async def _user(db_session, username: str, permissions: list[str], *, is_active: bool = True) -> User:
    """A user whose authority is exactly ``permissions``, via one custom group."""
    group = Group(name=f"grp-{username}", description="test", permissions=permissions)
    db_session.add(group)
    await db_session.flush()
    user = User(
        username=username,
        email=f"{username}@example.com",
        password_hash="x",
        role="user",
        is_active=is_active,
    )
    user.groups.append(group)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def _key(db_session, *, owner: User | None = None, **flags) -> tuple[str, APIKey]:
    raw, key_hash, key_prefix = generate_api_key()
    api_key = APIKey(
        name=f"k-{key_prefix}",
        key_hash=key_hash,
        key_prefix=key_prefix,
        enabled=True,
        user_id=owner.id if owner else None,
        **flags,
    )
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(api_key)
    return raw, api_key


class TestTheKeyCannotExceedItsOwner:
    @pytest.mark.asyncio
    async def test_a_scope_flag_alone_does_not_grant_what_the_owner_lacks(self, db_session):
        """⚠️ The whole point. `can_control_printer` on the row is not the last
        word — the owner has to hold the permission too."""
        owner = await _user(db_session, "reader", [Permission.PRINTERS_READ.value])
        _, api_key = await _key(db_session, owner=owner, can_control_printer=True)

        resolved = await resolve_apikey_owner(db_session, api_key)

        with pytest.raises(Exception) as caught:
            _check_apikey_permissions(api_key, [Permission.PRINTERS_CONTROL.value], owner=resolved)
        assert caught.value.status_code == 403
        assert "owner does not have" in caught.value.detail

    @pytest.mark.asyncio
    async def test_what_both_hold_still_passes(self, db_session):
        owner = await _user(db_session, "operator", [Permission.PRINTERS_CONTROL.value])
        _, api_key = await _key(db_session, owner=owner, can_control_printer=True)

        resolved = await resolve_apikey_owner(db_session, api_key)

        _check_apikey_permissions(api_key, [Permission.PRINTERS_CONTROL.value], owner=resolved)

    @pytest.mark.asyncio
    async def test_the_scope_flag_still_gates_an_all_powerful_owner(self, db_session):
        """The narrowing is an AND, not a replacement: an administrator's key
        with the flag off is still refused."""
        owner = await _user(db_session, "admin-ish", [Permission.PRINTERS_CONTROL.value])
        _, api_key = await _key(db_session, owner=owner, can_control_printer=False)

        resolved = await resolve_apikey_owner(db_session, api_key)

        with pytest.raises(Exception) as caught:
            _check_apikey_permissions(api_key, [Permission.PRINTERS_CONTROL.value], owner=resolved)
        assert caught.value.status_code == 403
        assert "can_control_printer" in caught.value.detail


class TestADeadOwnerKillsTheKey:
    @pytest.mark.asyncio
    async def test_a_deactivated_owner_is_403_not_no_owner(self, db_session):
        """⚠️ Returning None here would FAIL OPEN — the key would fall back to
        the legacy branch and keep its full scope authority."""
        owner = await _user(db_session, "gone", [Permission.PRINTERS_READ.value], is_active=False)
        _, api_key = await _key(db_session, owner=owner, can_read_status=True)

        with pytest.raises(Exception) as caught:
            await resolve_apikey_owner(db_session, api_key)
        assert caught.value.status_code == 403

    @pytest.mark.asyncio
    async def test_an_owner_row_that_no_longer_exists_is_403(self, db_session):
        _, api_key = await _key(db_session, can_read_status=True)
        api_key.user_id = 999_999
        await db_session.commit()

        with pytest.raises(Exception) as caught:
            await resolve_apikey_owner(db_session, api_key)
        assert caught.value.status_code == 403


class TestTheLegacyKeyIsNotBroken:
    @pytest.mark.asyncio
    async def test_no_user_id_means_the_scope_flags_stand_alone(self, db_session):
        """A key minted before per-user ownership has nobody to be narrowed
        against, and must keep working exactly as it did."""
        _, api_key = await _key(db_session, can_control_printer=True)

        assert await resolve_apikey_owner(db_session, api_key) is None
        _check_apikey_permissions(api_key, [Permission.PRINTERS_CONTROL.value], owner=None)


class TestWhatAuthMeReports:
    @pytest.mark.asyncio
    async def test_it_is_the_set_the_gate_will_actually_let_through(self, db_session):
        """⚠️ Reported and enforced must not drift — over-reporting here is the
        defect: a client renders actions that then 403."""
        owner = await _user(db_session, "narrow", [Permission.PRINTERS_READ.value])
        _, api_key = await _key(db_session, owner=owner, can_read_status=True, can_control_printer=True)
        resolved = await resolve_apikey_owner(db_session, api_key)

        reported = apikey_effective_permissions(api_key, resolved)

        assert Permission.PRINTERS_READ.value in reported
        assert Permission.PRINTERS_CONTROL.value not in reported, "the owner does not hold it"
        for perm in reported:
            # Every reported permission must survive the real gate.
            _check_apikey_permissions(api_key, [perm], owner=resolved)

    @pytest.mark.asyncio
    async def test_an_administrative_permission_never_appears(self, db_session):
        owner = await _user(db_session, "everything", [p.value for p in Permission])
        _, api_key = await _key(db_session, owner=owner, can_read_status=True)

        reported = apikey_effective_permissions(api_key, await resolve_apikey_owner(db_session, api_key))

        assert Permission.USERS_CREATE.value not in reported
        assert Permission.API_KEYS_CREATE.value not in reported

    @pytest.mark.asyncio
    async def test_the_endpoint_reports_the_owners_identity(self, async_client: AsyncClient, db_session):
        owner = await _user(db_session, "namesake", [Permission.PRINTERS_READ.value])
        raw, _ = await _key(db_session, owner=owner, can_read_status=True)

        response = await async_client.get("/api/v1/auth/me", headers={"X-API-Key": raw})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == owner.id
        assert body["username"] == "namesake"
        assert body["is_admin"] is False
        # ⚠️ Not the owner's groups: the key is not a member of them and does
        # not inherit what they grant.
        assert body["groups"] == []

    @pytest.mark.asyncio
    async def test_a_deactivated_owner_makes_the_endpoint_refuse(self, async_client: AsyncClient, db_session):
        owner = await _user(db_session, "retired", [Permission.PRINTERS_READ.value], is_active=False)
        raw, _ = await _key(db_session, owner=owner, can_read_status=True)

        response = await async_client.get("/api/v1/auth/me", headers={"X-API-Key": raw})

        assert response.status_code == 403, response.text


class TestTheWebhookDoorIsNotAWayAround:
    @pytest.mark.asyncio
    async def test_the_owner_is_checked_there_too(self, async_client: AsyncClient, db_session):
        """⚠️ /webhook/* reaches its scope flags through `check_permission`, not
        the modern gate, so it does not pick the narrowing up for free. Without
        it, a key refused printer control on /printers/{id}/stop could stop the
        print through /webhook/printer/{id}/stop."""
        owner = await _user(db_session, "webhooker", [Permission.PRINTERS_READ.value])
        raw, _ = await _key(db_session, owner=owner, can_read_status=True, can_control_printer=True)

        del async_client.headers["Authorization"]
        response = await async_client.post("/api/v1/webhook/printer/1/stop", headers={"X-API-Key": raw})

        assert response.status_code == 403, response.text
        assert "owner does not have" in response.json()["detail"]
