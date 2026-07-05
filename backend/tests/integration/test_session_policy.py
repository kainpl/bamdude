"""Integration tests for the admin-set session-lifetime ceiling (#1706, adapted).

Upstream (#1706) clamps the 24 h ACCESS token. In BamDude the access token is
deliberately short (1 h) and auto-refreshes, so the real session lifetime is the
REFRESH token TTL. The ceiling therefore clamps the refresh TTL at the single
choke point ``_issue_refresh_cookie`` — login, /auth/refresh, 2FA and OIDC all
route through it. These tests assert the REFRESH-token DB TTL, NOT the
access-token ``exp`` (which is intentionally left at 1 h and untouched).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import (
    REFRESH_TOKEN_EXPIRE_DAYS_REMEMBER,
    REFRESH_TOKEN_EXPIRE_HOURS_SESSION,
    SESSION_MAX_HOURS_DEFAULT,
    SESSION_MAX_HOURS_HARD_CEILING,
    resolve_session_max_hours,
)
from backend.app.models.auth_ephemeral import AuthEphemeralToken, TokenType
from backend.app.models.settings import Settings

# The admin seeded by the async_client fixture (see backend/tests/conftest.py).
ADMIN_USERNAME = "test_admin"
ADMIN_PASSWORD = "Test_AdminPass1!"


async def _set_session_max_hours(db: AsyncSession, value: str | None) -> None:
    """Upsert the ``session_max_hours`` setting row (``value=None`` deletes it)."""
    result = await db.execute(select(Settings).where(Settings.key == "session_max_hours"))
    existing = result.scalar_one_or_none()
    if value is None:
        if existing is not None:
            await db.delete(existing)
            await db.commit()
        return
    if existing is None:
        db.add(Settings(key="session_max_hours", value=value))
    else:
        existing.value = value
    await db.commit()


async def _latest_refresh_ttl_hours(db: AsyncSession) -> float:
    """Return the TTL (hours) of the most recently created REFRESH row.

    ``expires_at - created_at`` is the persisted DB-side lifetime — the real
    session ceiling in BamDude. Both columns come from the same row so their
    tz-awareness matches and the subtraction is a plain timedelta.
    """
    await db.rollback()  # drop any stale snapshot so the app's commit is visible
    result = await db.execute(
        select(AuthEphemeralToken)
        .where(AuthEphemeralToken.token_type == TokenType.REFRESH)
        .order_by(AuthEphemeralToken.id.desc())
    )
    row = result.scalars().first()
    assert row is not None, "expected a persisted REFRESH token row after login"
    return (row.expires_at - row.created_at).total_seconds() / 3600.0


class TestResolveSessionMaxHours:
    """Clamping resolver — hours in, hours out, clamped to [1, 720], 720 default."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_missing_row_returns_default(self, db_session: AsyncSession):
        await _set_session_max_hours(db_session, None)
        assert await resolve_session_max_hours(db_session) == SESSION_MAX_HOURS_DEFAULT
        assert SESSION_MAX_HOURS_DEFAULT == 720

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_empty_string_returns_default(self, db_session: AsyncSession):
        await _set_session_max_hours(db_session, "")
        assert await resolve_session_max_hours(db_session) == SESSION_MAX_HOURS_DEFAULT

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unparseable_returns_default(self, db_session: AsyncSession):
        await _set_session_max_hours(db_session, "not-a-number")
        assert await resolve_session_max_hours(db_session) == SESSION_MAX_HOURS_DEFAULT

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_zero_returns_default(self, db_session: AsyncSession):
        await _set_session_max_hours(db_session, "0")
        assert await resolve_session_max_hours(db_session) == SESSION_MAX_HOURS_DEFAULT

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_negative_returns_default(self, db_session: AsyncSession):
        await _set_session_max_hours(db_session, "-5")
        assert await resolve_session_max_hours(db_session) == SESSION_MAX_HOURS_DEFAULT

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_one_hour_minimum(self, db_session: AsyncSession):
        await _set_session_max_hours(db_session, "1")
        assert await resolve_session_max_hours(db_session) == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_seven_days_passthrough(self, db_session: AsyncSession):
        await _set_session_max_hours(db_session, "168")
        assert await resolve_session_max_hours(db_session) == 168

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_thirty_days_passthrough(self, db_session: AsyncSession):
        await _set_session_max_hours(db_session, "720")
        assert await resolve_session_max_hours(db_session) == 720

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_above_ceiling_clamped(self, db_session: AsyncSession):
        await _set_session_max_hours(db_session, "99999")
        assert await resolve_session_max_hours(db_session) == SESSION_MAX_HOURS_HARD_CEILING


class TestLoginClampsRefreshTTL:
    """Login persists a REFRESH row whose DB TTL honours the admin ceiling — the
    real session lifetime in BamDude. The 1 h access token is untouched and is
    deliberately NOT asserted here."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_remember_me_default_ceiling_is_30_days(self, async_client: AsyncClient, db_session: AsyncSession):
        await _set_session_max_hours(db_session, None)  # unset → default 720
        resp = await async_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "remember_me": True},
        )
        assert resp.status_code == 200, resp.text
        ttl_hours = await _latest_refresh_ttl_hours(db_session)
        assert abs(ttl_hours - REFRESH_TOKEN_EXPIRE_DAYS_REMEMBER * 24) < 0.5

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_remember_me_clamped_to_seven_days(self, async_client: AsyncClient, db_session: AsyncSession):
        await _set_session_max_hours(db_session, "168")
        resp = await async_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "remember_me": True},
        )
        assert resp.status_code == 200, resp.text
        ttl_hours = await _latest_refresh_ttl_hours(db_session)
        assert abs(ttl_hours - 168) < 0.5

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_remember_me_clamped_to_24_hours(self, async_client: AsyncClient, db_session: AsyncSession):
        await _set_session_max_hours(db_session, "24")
        resp = await async_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "remember_me": True},
        )
        assert resp.status_code == 200, resp.text
        ttl_hours = await _latest_refresh_ttl_hours(db_session)
        assert abs(ttl_hours - 24) < 0.5

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_session_login_uses_min_of_session_ttl_and_ceiling(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        # Without remember_me the base TTL is the 12 h session cap; a high ceiling
        # (default 720) can't lengthen it — min(12 h, 720 h) = 12 h.
        await _set_session_max_hours(db_session, None)
        resp = await async_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "remember_me": False},
        )
        assert resp.status_code == 200, resp.text
        ttl_hours = await _latest_refresh_ttl_hours(db_session)
        assert abs(ttl_hours - REFRESH_TOKEN_EXPIRE_HOURS_SESSION) < 0.5


class TestRefreshClampsRefreshTTL:
    """Lowering the ceiling shortens the NEXT rotation's refresh TTL."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_refresh_reflects_lowered_ceiling(self, async_client: AsyncClient, db_session: AsyncSession):
        # 1. Login with the default 720 ceiling → 30-day remember-me row.
        await _set_session_max_hours(db_session, None)
        login = await async_client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "remember_me": True},
        )
        assert login.status_code == 200, login.text
        assert abs(await _latest_refresh_ttl_hours(db_session) - REFRESH_TOKEN_EXPIRE_DAYS_REMEMBER * 24) < 0.5

        # 2. Admin lowers the ceiling to 24 h.
        await _set_session_max_hours(db_session, "24")

        # 3. Rotate. The client carries the bamdude_refresh cookie set at login.
        refresh = await async_client.post("/api/v1/auth/refresh")
        assert refresh.status_code == 200, refresh.text

        # 4. The freshly-rotated refresh row honours the lowered ceiling.
        assert abs(await _latest_refresh_ttl_hours(db_session) - 24) < 0.5


class TestSettingsAPISessionMaxHours:
    """Round-trip ``session_max_hours`` through the settings API."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_defaults_to_720_when_unset(self, async_client: AsyncClient, db_session: AsyncSession):
        await _set_session_max_hours(db_session, None)
        resp = await async_client.get("/api/v1/settings/")
        assert resp.status_code == 200, resp.text
        assert resp.json()["session_max_hours"] == 720

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_put_persists_valid_value(self, async_client: AsyncClient, db_session: AsyncSession):
        resp = await async_client.put("/api/v1/settings/", json={"session_max_hours": 168})
        assert resp.status_code == 200, resp.text
        assert resp.json()["session_max_hours"] == 168
        # Persisted as its string form.
        await db_session.rollback()
        row = (
            await db_session.execute(select(Settings).where(Settings.key == "session_max_hours"))
        ).scalar_one_or_none()
        assert row is not None and row.value == "168"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_put_rejects_zero(self, async_client: AsyncClient):
        resp = await async_client.put("/api/v1/settings/", json={"session_max_hours": 0})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_put_rejects_above_ceiling(self, async_client: AsyncClient):
        resp = await async_client.put("/api/v1/settings/", json={"session_max_hours": 721})
        assert resp.status_code == 422
