"""Bambu refresh-token hardening, the persistence half (spec A §3):
the refresh token is stored at login, loaded into the service, and on a
genuine expiry 401 the flow refreshes-then-persists instead of declaring
the credential dead; only a failed refresh falls back to the sign-out.
"""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.app.api.routes.cloud import (
    _refresh_or_invalidate,
    build_authenticated_cloud,
    get_stored_refresh_token,
    store_token,
)
from backend.app.models.user import User


async def _seed_user(db_session, **extra) -> User:
    user = User(username="cloudy", role="operator", **extra)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.mark.asyncio
async def test_store_token_persists_refresh_token_per_user(db_session, async_client):
    user = await _seed_user(db_session)
    await store_token(db_session, "acc-1", "a@b.c", "global", user, refresh_token="ref-1")
    row = (await db_session.execute(select(User).where(User.id == user.id))).scalar_one()
    assert (row.cloud_token, row.cloud_refresh_token) == ("acc-1", "ref-1")
    assert await get_stored_refresh_token(db_session, row) == "ref-1"

    # A later token-auth sign-in (no refresh token) must CLEAR the stale one —
    # a refresh token from a previous pair cannot renew the new access token.
    await store_token(db_session, "acc-2", "a@b.c", "global", user)
    row = (await db_session.execute(select(User).where(User.id == user.id))).scalar_one()
    assert (row.cloud_token, row.cloud_refresh_token) == ("acc-2", None)


@pytest.mark.asyncio
async def test_store_token_persists_refresh_token_globally(db_session, async_client):
    """Auth-disabled mode: the pair lives in the Settings table."""
    await store_token(db_session, "acc-g", "g@b.c", "global", None, refresh_token="ref-g")
    assert await get_stored_refresh_token(db_session, None) == "ref-g"


@pytest.mark.asyncio
async def test_build_loads_refresh_token_into_the_service(db_session, async_client):
    user = await _seed_user(db_session, cloud_token="acc", cloud_refresh_token="ref")
    cloud = await build_authenticated_cloud(db_session, user)
    try:
        assert cloud is not None
        assert cloud.access_token == "acc" and cloud.refresh_token == "ref"
    finally:
        await cloud.close()


@pytest.mark.asyncio
async def test_refresh_success_persists_pair_and_keeps_connection_alive(db_session, async_client):
    user = await _seed_user(db_session, cloud_token="dead-acc", cloud_refresh_token="ref")

    cloud = AsyncMock()
    cloud.refresh_access_token.return_value = True
    cloud.access_token = "fresh-acc"
    cloud.refresh_token = "fresh-ref"
    await _refresh_or_invalidate(cloud, user.id)

    user_id = user.id
    db_session.expire_all()  # the write went through its own session
    row = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    assert (row.cloud_token, row.cloud_refresh_token) == ("fresh-acc", "fresh-ref")
    assert row.cloud_token_invalid_at is None


@pytest.mark.asyncio
async def test_refresh_failure_falls_back_to_sign_out(db_session, async_client):
    user = await _seed_user(db_session, cloud_token="dead-acc", cloud_refresh_token="dead-ref")

    cloud = AsyncMock()
    cloud.refresh_access_token.return_value = False
    await _refresh_or_invalidate(cloud, user.id)

    user_id = user.id
    db_session.expire_all()  # the write went through its own session
    row = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    assert row.cloud_token == "dead-acc"  # token untouched — the UI explains, the user re-logs
    assert row.cloud_token_invalid_at is not None
