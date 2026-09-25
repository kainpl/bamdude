"""Tests for ``services/bambu_cloud_credentials`` — the credential seam.

Ported from upstream (#2845). The seam holds where the stored Bambu Cloud
credential lives and whether Bambu has rejected it; both identity shapes are
real inputs here: a signed-in ``User`` carries its own columns, and ``None``
(an API key without an owner) reads and writes the global ``Settings`` rows.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.core.auth import get_password_hash
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services.bambu_cloud_credentials import (
    CLOUD_EMAIL_KEY,
    CLOUD_REFRESH_TOKEN_KEY,
    CLOUD_REGION_KEY,
    CLOUD_TOKEN_INVALID_KEY,
    CLOUD_TOKEN_KEY,
    get_stored_refresh_token,
    get_stored_token,
    is_cloud_token_invalid,
    mark_cloud_token_invalid,
)

pytestmark = pytest.mark.asyncio


class _SharedSessionCtx:
    """Route ``mark`` through the fixture's session: the function opens its own
    session through ``core.database.async_session`` (late import, the same
    seam every own-session writer in this codebase uses)."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def shared_session(db_session, monkeypatch):
    monkeypatch.setattr("backend.app.core.database.async_session", lambda: _SharedSessionCtx(db_session))


async def _make_user(db, username: str = "cred-user", **fields) -> User:
    user = User(
        username=username,
        password_hash=get_password_hash("AdminPass1!"),
        role="admin",
        is_active=True,
        **fields,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def test_get_stored_token_reads_the_users_own_columns(db_session):
    user = await _make_user(db_session, cloud_token="tok-u", cloud_email="u@example.com", cloud_region="china")

    assert await get_stored_token(db_session, user) == ("tok-u", "u@example.com", "china")


async def test_get_stored_token_normalises_an_unknown_region(db_session):
    user = await _make_user(db_session, cloud_token="tok-u", cloud_region="mars")

    _token, _email, region = await get_stored_token(db_session, user)

    assert region == "global"


async def test_get_stored_token_without_a_user_reads_settings(db_session):
    db_session.add_all(
        [
            Settings(key=CLOUD_TOKEN_KEY, value="tok-g"),
            Settings(key=CLOUD_EMAIL_KEY, value="g@example.com"),
            Settings(key=CLOUD_REGION_KEY, value=""),
        ]
    )
    await db_session.commit()

    assert await get_stored_token(db_session, None) == ("tok-g", "g@example.com", "global")


async def test_get_stored_refresh_token_for_both_identities(db_session):
    user = await _make_user(db_session, cloud_refresh_token="ref-u")
    db_session.add(Settings(key=CLOUD_REFRESH_TOKEN_KEY, value="ref-g"))
    await db_session.commit()

    assert await get_stored_refresh_token(db_session, user) == "ref-u"
    assert await get_stored_refresh_token(db_session, None) == "ref-g"


async def test_is_cloud_token_invalid_follows_the_user_column(db_session):
    user = await _make_user(db_session)
    assert await is_cloud_token_invalid(db_session, user) is False

    user.cloud_token_invalid_at = datetime.now(timezone.utc)
    await db_session.commit()

    assert await is_cloud_token_invalid(db_session, user) is True


async def test_is_cloud_token_invalid_without_a_user_reads_settings(db_session):
    assert await is_cloud_token_invalid(db_session, None) is False

    db_session.add(Settings(key=CLOUD_TOKEN_INVALID_KEY, value=datetime.now(timezone.utc).isoformat()))
    await db_session.commit()

    assert await is_cloud_token_invalid(db_session, None) is True


async def test_mark_sets_the_per_user_flag(db_session):
    """user_id set → the rejection lands on that user's column."""
    user = await _make_user(db_session)

    await mark_cloud_token_invalid(user.id)
    await db_session.refresh(user)

    assert user.cloud_token_invalid_at is not None


async def test_mark_none_writes_the_global_settings_flag(db_session):
    """user_id=None (an ownerless API key) → the global ``Settings`` row.
    A second call updates the existing row rather than adding another."""
    await mark_cloud_token_invalid(None)

    result = await db_session.execute(select(Settings).where(Settings.key == CLOUD_TOKEN_INVALID_KEY))
    rows = result.scalars().all()
    assert len(rows) == 1
    # Stored value parses as ISO — the status endpoints compare it as a date.
    datetime.fromisoformat(rows[0].value)

    first_value = rows[0].value
    await mark_cloud_token_invalid(None)
    result = await db_session.execute(select(Settings).where(Settings.key == CLOUD_TOKEN_INVALID_KEY))
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].value >= first_value


async def test_mark_is_best_effort(monkeypatch):
    """A bookkeeping failure must never replace the 401 the caller needs to
    see — the function swallows everything."""

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("db gone")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("backend.app.core.database.async_session", lambda: _Boom())

    # Must not raise.
    await mark_cloud_token_invalid(None)
