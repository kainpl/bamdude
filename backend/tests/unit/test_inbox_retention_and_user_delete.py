"""inbox_retention_days is a real setting, and a deleted user takes their inbox with them."""

import pytest
from sqlalchemy import select

from backend.app.models.user import User
from backend.app.models.user_notification import UserNotification
from backend.app.schemas.settings import AppSettings, AppSettingsUpdate


def test_the_setting_has_the_agreed_default_and_bounds():
    assert AppSettings().inbox_retention_days == 30
    assert AppSettingsUpdate(inbox_retention_days=7).inbox_retention_days == 7
    with pytest.raises(ValueError):
        AppSettingsUpdate(inbox_retention_days=0)
    with pytest.raises(ValueError):
        AppSettingsUpdate(inbox_retention_days=366)


@pytest.mark.asyncio
async def test_get_settings_returns_it_as_an_integer(async_client):
    r = await async_client.patch("/api/v1/settings/", json={"inbox_retention_days": 12})
    assert r.status_code == 200, r.text
    r = await async_client.get("/api/v1/settings/")
    assert r.json()["inbox_retention_days"] == 12


@pytest.mark.asyncio
async def test_deleting_a_user_deletes_their_inbox_rows(async_client, db_session):
    user = User(username="doomed", password_hash="x", role="user", is_active=True)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    db_session.add(
        UserNotification(user_id=user.id, event_type="print_failed", severity="error", title="t", message="m")
    )
    await db_session.commit()

    # Read the id out before the delete: ``expire_all`` below would otherwise
    # make ``user.id`` a lazy reload of a row that no longer exists.
    user_id = user.id

    r = await async_client.delete(f"/api/v1/users/{user_id}")
    assert r.status_code == 204, r.text
    db_session.expire_all()
    result = await db_session.execute(select(UserNotification).where(UserNotification.user_id == user_id))
    assert result.scalars().all() == []
