"""Full HTTP-stack regressions for request-local authentication."""

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import select
from starlette.requests import Request

from backend.app.core.auth import ALGORITHM, SECRET_KEY, create_access_token


@pytest.mark.asyncio
@pytest.mark.integration
async def test_permission_route_rejects_revoked_jwt(async_client):
    from backend.app.core.auth import revoke_jti

    token = create_access_token({"sub": "test_admin"})
    payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    await revoke_jti(payload["jti"], datetime.now(timezone.utc) + timedelta(hours=1), "test_admin")

    response = await async_client.get("/api/v1/queue/", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
async def test_permission_route_rejects_jwt_without_iat(async_client):
    token = jwt.encode(
        {"sub": "test_admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    response = await async_client.get("/api/v1/queue/", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
async def test_middleware_and_permission_reuse_full_jwt_resolution_per_request(async_client, monkeypatch):
    from backend.app.core import auth

    lookups = 0
    original = auth.get_user_by_username

    async def counted_lookup(db, username):
        nonlocal lookups
        lookups += 1
        return await original(db, username)

    monkeypatch.setattr(auth, "get_user_by_username", counted_lookup)
    token = create_access_token({"sub": "test_admin"})
    headers = {"Authorization": f"Bearer {token}"}

    assert (await async_client.get("/api/v1/queue/", headers=headers)).status_code == 200
    assert lookups == 1
    assert (await async_client.get("/api/v1/auth/me", headers=headers)).status_code == 200
    assert lookups == 2  # A new request must recheck authority.


@pytest.mark.asyncio
@pytest.mark.integration
async def test_api_key_hash_and_last_used_are_request_local(async_client, db_session, monkeypatch):
    from backend.app.core import auth
    from backend.app.models.api_key import APIKey

    raw, key_hash, key_prefix = auth.generate_api_key()
    key = APIKey(name="hot-read", key_hash=key_hash, key_prefix=key_prefix, enabled=True)
    db_session.add(key)
    await db_session.commit()
    scans = 0
    original = auth._match_api_key

    def counted_match(credential, candidates):
        nonlocal scans
        scans += 1
        return original(credential, candidates)

    monkeypatch.setattr(auth, "_match_api_key", counted_match)
    request = Request({"type": "http", "headers": []})
    first = await auth.resolve_api_key_authority(request, raw)
    second = await auth.resolve_api_key_authority(request, raw)
    assert first is second
    assert scans == 1
    assert first.key.last_used is not None

    await auth.resolve_api_key_authority(Request({"type": "http", "headers": []}), raw)
    assert scans == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_new_request_rechecks_password_floor_and_active_user(async_client, db_session):
    from backend.app.models.user import User

    token = create_access_token({"sub": "test_admin"})
    headers = {"Authorization": f"Bearer {token}"}
    assert (await async_client.get("/api/v1/queue/", headers=headers)).status_code == 200

    user = (await db_session.execute(select(User).where(User.username == "test_admin"))).scalar_one()
    user.password_changed_at = datetime.now(timezone.utc) + timedelta(seconds=5)
    await db_session.commit()
    assert (await async_client.get("/api/v1/queue/", headers=headers)).status_code == 401

    user.password_changed_at = None
    user.is_active = False
    await db_session.commit()
    assert (await async_client.get("/api/v1/queue/", headers=headers)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
async def test_new_request_rechecks_group_permissions(async_client, db_session):
    from backend.app.models.group import Group
    from backend.app.models.user import User

    group = Group(name="hot-read-group", permissions=["queue:read_own"])
    user = User(username="hot-read-user", password_hash="x", role="user", is_active=True)
    user.groups.append(group)
    db_session.add(user)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_access_token({'sub': user.username})}"}

    assert (await async_client.get("/api/v1/queue/?status=pending", headers=headers)).status_code == 200
    group.permissions = []
    await db_session.commit()
    assert (await async_client.get("/api/v1/queue/?status=pending", headers=headers)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.integration
async def test_api_key_changed_during_hash_is_not_accepted(async_client, db_session, monkeypatch):
    from backend.app.core import auth
    from backend.app.models.api_key import APIKey

    raw, key_hash, key_prefix = auth.generate_api_key()
    key = APIKey(name="change-during-hash", key_hash=key_hash, key_prefix=key_prefix, enabled=True)
    db_session.add(key)
    await db_session.commit()
    original = auth._scan_api_key

    async def rotate_after_hash(credential, candidates):
        winner = await original(credential, candidates)
        key.key_hash = auth.get_password_hash("other-credential")
        await db_session.commit()
        return winner

    monkeypatch.setattr(auth, "_scan_api_key", rotate_after_hash)
    with pytest.raises(auth.APIKeyValidationFailure, match="invalid"):
        await auth.resolve_api_key_authority(Request({"type": "http", "headers": []}), raw)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("change", ["disable_key", "disable_owner"])
async def test_api_key_authority_change_during_hash_fails_closed(async_client, db_session, monkeypatch, change):
    from backend.app.core import auth
    from backend.app.models.api_key import APIKey
    from backend.app.models.group import Group
    from backend.app.models.user import User

    group = Group(name=f"hot-key-{change}", permissions=["queue:read_all"])
    owner = User(username=f"hot-key-{change}", password_hash="x", role="user", is_active=True)
    owner.groups.append(group)
    db_session.add(owner)
    await db_session.flush()
    raw, key_hash, key_prefix = auth.generate_api_key()
    key = APIKey(
        name=f"hot-key-{change}",
        key_hash=key_hash,
        key_prefix=key_prefix,
        user_id=owner.id,
        enabled=True,
        can_read_status=True,
    )
    db_session.add(key)
    await db_session.commit()
    original = auth._scan_api_key

    async def change_after_hash(credential, candidates):
        winner = await original(credential, candidates)
        if change == "disable_key":
            key.enabled = False
        else:
            owner.is_active = False
        await db_session.commit()
        return winner

    monkeypatch.setattr(auth, "_scan_api_key", change_after_hash)
    response = await async_client.get("/api/v1/queue/?status=pending", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == (401 if change == "disable_key" else 403), response.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dual_header_invalid_key_falls_back_but_valid_denied_key_does_not(async_client, db_session):
    from backend.app.core import auth
    from backend.app.models.api_key import APIKey

    token = create_access_token({"sub": "test_admin"})
    invalid = await async_client.get(
        "/api/v1/queue/", headers={"X-API-Key": "bb_invalid", "Authorization": f"Bearer {token}"}
    )
    assert invalid.status_code == 200, invalid.text

    raw, key_hash, key_prefix = auth.generate_api_key()
    db_session.add(
        APIKey(
            name="no-status",
            key_hash=key_hash,
            key_prefix=key_prefix,
            enabled=True,
            can_read_status=False,
        )
    )
    await db_session.commit()
    denied = await async_client.get("/api/v1/queue/", headers={"X-API-Key": raw, "Authorization": f"Bearer {token}"})
    assert denied.status_code == 403, denied.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cancelled_request_keeps_hash_slot_until_thread_finishes(async_client, db_session, monkeypatch):
    from backend.app.core import auth
    from backend.app.models.api_key import APIKey

    raw, key_hash, key_prefix = auth.generate_api_key()
    db_session.add(APIKey(name="slow", key_hash=key_hash, key_prefix=key_prefix, enabled=True))
    await db_session.commit()
    entered = threading.Event()
    release = threading.Event()
    original = auth._match_api_key

    def slow_match(credential, candidates):
        entered.set()
        release.wait(5)
        return original(credential, candidates)

    monkeypatch.setattr(auth, "_match_api_key", slow_match)
    request = Request({"type": "http", "headers": []})
    caller = asyncio.create_task(auth.resolve_api_key_authority(request, raw))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert any(not task.done() for task in auth._api_key_tasks)
        assert auth._key_gates()[1]._value <= 3  # physical thread still owns a worker slot
    finally:
        release.set()
    async with asyncio.timeout(5):
        while auth._api_key_tasks:
            await asyncio.sleep(0.01)
    assert auth._key_gates()[1]._value == 4


@pytest.mark.asyncio
@pytest.mark.integration
async def test_api_key_hash_saturation_is_transient_503(async_client, db_session, monkeypatch):
    from backend.app.core import auth
    from backend.app.models.api_key import APIKey

    raw, key_hash, key_prefix = auth.generate_api_key()
    db_session.add(APIKey(name="overloaded", key_hash=key_hash, key_prefix=key_prefix, enabled=True))
    await db_session.commit()
    monkeypatch.setattr(auth, "_key_gates", lambda: (asyncio.BoundedSemaphore(0), asyncio.BoundedSemaphore(0)))
    response = await async_client.get("/api/v1/queue/", headers={"X-API-Key": raw})
    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "API key validation is busy"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ten_simultaneous_api_key_reads_fit_bounded_admission(async_client, db_session):
    from backend.app.core import auth
    from backend.app.models.api_key import APIKey

    raw, key_hash, key_prefix = auth.generate_api_key()
    db_session.add(APIKey(name="operator-burst", key_hash=key_hash, key_prefix=key_prefix, enabled=True))
    await db_session.commit()

    responses = await asyncio.gather(
        *(async_client.get("/api/v1/queue/?status=pending", headers={"X-API-Key": raw}) for _ in range(10))
    )
    assert [response.status_code for response in responses] == [200] * 10


@pytest.mark.asyncio
@pytest.mark.integration
async def test_api_key_permission_uses_immutable_request_snapshot(async_client, db_session):
    from fastapi import HTTPException

    from backend.app.core import auth
    from backend.app.core.permissions import Permission
    from backend.app.models.api_key import APIKey

    raw, key_hash, key_prefix = auth.generate_api_key()
    db_session.add(
        APIKey(
            name="snapshot",
            key_hash=key_hash,
            key_prefix=key_prefix,
            enabled=True,
            can_read_status=False,
        )
    )
    await db_session.commit()
    authority = await auth.resolve_api_key_authority(Request({"type": "http", "headers": []}), raw)
    authority.key.can_read_status = True  # mutating a detached compatibility ORM object grants nothing
    with pytest.raises(HTTPException) as denied:
        await auth.authorize_api_key(db_session, authority.key, [Permission.QUEUE_READ_ALL.value], authority=authority)
    assert denied.value.status_code == 403
