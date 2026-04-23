"""Sliding-session refresh flow (§18.14).

Tests the full lifecycle: login → refresh → rotate → reuse-detect → logout +
remember-me TTL differentiation + password-change revocation.

Uses the ``async_client`` fixture which already seeds ``test_admin`` and
attaches its bearer token; for the refresh-cookie paths we explicitly
manage ``client.cookies`` so the test sees exactly what a browser would.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.core.auth import (
    REFRESH_TOKEN_COOKIE_NAME,
    REFRESH_TOKEN_EXPIRE_DAYS_REMEMBER,
    REFRESH_TOKEN_EXPIRE_HOURS_SESSION,
    _hash_refresh_token,
)
from backend.app.models.auth_ephemeral import AuthEphemeralToken, TokenType

# async_client ships with a bearer token attached for the seeded test_admin.
# The refresh flow needs a fresh, browser-like client that carries ONLY the
# refresh cookie — otherwise the bearer auth leaks across the test and
# verify_and_consume is never exercised in isolation.
pytestmark = pytest.mark.asyncio


async def _login(client: AsyncClient, *, remember_me: bool = False, password: str = "Test_AdminPass1!"):
    """Hit /login with the seeded test_admin creds; returns (response, refresh_cookie_value)."""
    resp = await client.post(
        "/api/v1/auth/login",
        json={"username": "test_admin", "password": password, "remember_me": remember_me},
    )
    assert resp.status_code == 200, resp.text
    cookie = resp.cookies.get(REFRESH_TOKEN_COOKIE_NAME)
    assert cookie, "login did not set the refresh cookie"
    return resp, cookie


async def test_login_sets_refresh_cookie_and_access_token(async_client: AsyncClient, db_session):
    resp, cookie = await _login(async_client)
    body = resp.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    # DB row created with a SHA-256 hash of the raw cookie — plaintext never
    # hits storage, verified by re-hashing and looking up the row.
    row = (
        await db_session.execute(
            select(AuthEphemeralToken)
            .where(AuthEphemeralToken.token == _hash_refresh_token(cookie))
            .where(AuthEphemeralToken.token_type == TokenType.REFRESH)
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.used_at is None
    assert row.family_id is not None


async def test_refresh_rotates_token_and_mints_new_access(async_client: AsyncClient, db_session):
    _, old_cookie = await _login(async_client)

    # Fresh client so nothing but the refresh cookie is sent.
    async with AsyncClient(transport=async_client._transport, base_url="http://test") as browser:
        browser.cookies.set(REFRESH_TOKEN_COOKIE_NAME, old_cookie, path="/api/v1/auth")
        resp = await browser.post("/api/v1/auth/refresh")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["access_token"]
        new_cookie = resp.cookies.get(REFRESH_TOKEN_COOKIE_NAME)
        assert new_cookie and new_cookie != old_cookie

    # Old row now carries used_at; new row is fresh.
    old_row = (
        await db_session.execute(
            select(AuthEphemeralToken).where(AuthEphemeralToken.token == _hash_refresh_token(old_cookie))
        )
    ).scalar_one_or_none()
    assert old_row is not None and old_row.used_at is not None
    new_row = (
        await db_session.execute(
            select(AuthEphemeralToken).where(AuthEphemeralToken.token == _hash_refresh_token(new_cookie))
        )
    ).scalar_one_or_none()
    assert new_row is not None and new_row.used_at is None
    # Same family preserved across rotation (reuse-detection lineage).
    assert new_row.family_id == old_row.family_id


async def test_refresh_replay_revokes_whole_family(async_client: AsyncClient, db_session):
    _, old_cookie = await _login(async_client)

    async with AsyncClient(transport=async_client._transport, base_url="http://test") as browser:
        # Bypass the cookie jar entirely — the jar auto-updates from
        # Set-Cookie on each response, which is exactly what a real browser
        # does but here we want to simulate a STOLEN cookie replayed out of
        # band. Sending via raw Cookie header keeps each request's cookie
        # exactly what we specify.
        first = await browser.post(
            "/api/v1/auth/refresh",
            headers={"Cookie": f"{REFRESH_TOKEN_COOKIE_NAME}={old_cookie}"},
        )
        assert first.status_code == 200
        replay = await browser.post(
            "/api/v1/auth/refresh",
            headers={"Cookie": f"{REFRESH_TOKEN_COOKIE_NAME}={old_cookie}"},
        )
        assert replay.status_code == 401
        assert "replay" in replay.json()["detail"].lower()

    # Whole family should be gone from the DB — not just the replayed row.
    remaining = (
        (await db_session.execute(select(AuthEphemeralToken).where(AuthEphemeralToken.token_type == TokenType.REFRESH)))
        .scalars()
        .all()
    )
    assert remaining == [], f"family should have been revoked, got {len(remaining)} rows"


async def test_refresh_without_cookie_returns_401(async_client: AsyncClient):
    async with AsyncClient(transport=async_client._transport, base_url="http://test") as browser:
        resp = await browser.post("/api/v1/auth/refresh")
        assert resp.status_code == 401
        assert "no refresh" in resp.json()["detail"].lower()


async def test_remember_me_extends_db_ttl_to_30_days(async_client: AsyncClient, db_session):
    _, cookie = await _login(async_client, remember_me=True)
    row = (
        await db_session.execute(
            select(AuthEphemeralToken).where(AuthEphemeralToken.token == _hash_refresh_token(cookie))
        )
    ).scalar_one_or_none()
    assert row is not None
    lifespan_hours = (row.expires_at - row.created_at).total_seconds() / 3600
    # Allow ± 1 h slack for test timing + TZ jitter.
    expected = REFRESH_TOKEN_EXPIRE_DAYS_REMEMBER * 24
    assert abs(lifespan_hours - expected) < 1


async def test_no_remember_me_caps_db_ttl_at_session_window(async_client: AsyncClient, db_session):
    _, cookie = await _login(async_client, remember_me=False)
    row = (
        await db_session.execute(
            select(AuthEphemeralToken).where(AuthEphemeralToken.token == _hash_refresh_token(cookie))
        )
    ).scalar_one_or_none()
    assert row is not None
    lifespan_hours = (row.expires_at - row.created_at).total_seconds() / 3600
    assert abs(lifespan_hours - REFRESH_TOKEN_EXPIRE_HOURS_SESSION) < 1


async def test_logout_clears_refresh_family(async_client: AsyncClient, db_session):
    resp, cookie = await _login(async_client)
    access_token = resp.json()["access_token"]

    async with AsyncClient(transport=async_client._transport, base_url="http://test") as browser:
        browser.cookies.set(REFRESH_TOKEN_COOKIE_NAME, cookie, path="/api/v1/auth")
        logout_resp = await browser.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert logout_resp.status_code == 200

    remaining = (
        (await db_session.execute(select(AuthEphemeralToken).where(AuthEphemeralToken.token_type == TokenType.REFRESH)))
        .scalars()
        .all()
    )
    assert remaining == []


async def test_password_change_revokes_all_user_refresh_tokens(async_client: AsyncClient, db_session):
    # Seed: user logs in twice (from two "devices") — two families.
    _, _ = await _login(async_client)
    _, _ = await _login(async_client)
    refresh_rows_before = (
        (await db_session.execute(select(AuthEphemeralToken).where(AuthEphemeralToken.token_type == TokenType.REFRESH)))
        .scalars()
        .all()
    )
    assert len(refresh_rows_before) == 2

    # Change password on one of those sessions — bearer is already attached via
    # async_client's seeded token.
    change_resp = await async_client.post(
        "/api/v1/users/me/change-password",
        json={"current_password": "Test_AdminPass1!", "new_password": "NewPass-9!Xyz"},
    )
    assert change_resp.status_code == 200, change_resp.text

    # Both "devices" refresh rows should be gone.
    refresh_rows_after = (
        (await db_session.execute(select(AuthEphemeralToken).where(AuthEphemeralToken.token_type == TokenType.REFRESH)))
        .scalars()
        .all()
    )
    assert refresh_rows_after == []
