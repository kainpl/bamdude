"""Notify configuration validation and lifecycle cleanup through the public API."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.notification import NotificationProvider
from backend.app.models.notification_live_activity import NotificationLiveActivity
from backend.app.models.notification_lock_screen_widget import NotificationLockScreenWidget
from backend.app.services.notify_live_activities import _credential_key

CONFIG = {
    "device_id": "IO12345678901234",
    "token": "example-token",
    "live_activities": True,
    "lock_screen_widgets": True,
}
# Ours (G01): a response never carries the token, only that one is configured.
SHOWN = {key: value for key, value in CONFIG.items() if key != "token"}
# Ours: an edit sends the public fields and keeps the stored token explicitly.
KEEP = {"secret_actions": {"token": "keep"}}


async def create_provider(client):
    response = await client.post(
        "/api/v1/notifications/", json={"name": "Notify", "provider_type": "notify", "config": CONFIG}
    )
    assert response.status_code == 200
    return response.json()["id"]


async def test_notify_settings_round_trip_and_push_flags_remain_independent(async_client):
    provider_id = await create_provider(async_client)
    response = await async_client.get(f"/api/v1/notifications/{provider_id}")
    assert response.json()["config"] == SHOWN
    assert response.json()["secrets_configured"] == {"token": True}
    assert response.json()["on_print_start"] is False
    assert response.json()["on_print_progress"] is False


async def test_notify_crud_refreshes_worker_configuration_without_remote_work(async_client):
    with (
        patch("backend.app.api.routes.notifications.notify_live_activities.providers_changed") as live_changed,
        patch("backend.app.api.routes.notifications.notify_widgets.providers_changed") as widgets_changed,
        patch("backend.app.api.routes.notifications.notify_live_activities.schedule_cleanup", new_callable=AsyncMock),
        patch("backend.app.api.routes.notifications.notify_widgets.schedule_cleanup", new_callable=AsyncMock),
        patch(
            "backend.app.api.routes.notifications.notify_live_activities.cleanup_provider", new_callable=AsyncMock
        ) as live_http,
        patch(
            "backend.app.api.routes.notifications.notify_widgets.cleanup_provider", new_callable=AsyncMock
        ) as widget_http,
    ):
        provider_id = await create_provider(async_client)
        assert live_changed.call_count == widgets_changed.call_count == 1
        response = await async_client.patch(f"/api/v1/notifications/{provider_id}", json={"enabled": False})
        assert response.status_code == 200
        assert live_changed.call_count == widgets_changed.call_count == 2
        response = await async_client.delete(f"/api/v1/notifications/{provider_id}")
        assert response.status_code == 200
        assert live_changed.call_count == widgets_changed.call_count == 3
    live_http.assert_not_awaited()
    widget_http.assert_not_awaited()


@pytest.mark.parametrize(
    "config",
    [
        {},
        {**CONFIG, "device_id": "GRP12345"},
        {**CONFIG, "device_id": "WB12345678901234"},
        {**CONFIG, "live_activities": "false"},
        {**CONFIG, "live_activities": False, "lock_screen_widgets": "false"},
        {**CONFIG, "device_id": "GRP12345", "live_activities": False},
        {**CONFIG, "device_id": "WB12345678901234", "live_activities": False},
    ],
)
async def test_invalid_notify_configuration_cannot_be_created(async_client, config):
    response = await async_client.post(
        "/api/v1/notifications/", json={"name": "Notify", "provider_type": "notify", "config": config}
    )
    assert response.status_code == 422


async def test_empty_config_update_is_rejected_and_keeps_previous_credentials(async_client):
    provider_id = await create_provider(async_client)
    response = await async_client.patch(f"/api/v1/notifications/{provider_id}", json={"config": {}, **KEEP})
    # Ours (G01): keeping the token while its device changes is refused first, with 400
    # (upstream answers 422 from notify_credentials); either way nothing is saved.
    assert response.status_code == 400
    saved = await async_client.get(f"/api/v1/notifications/{provider_id}")
    assert saved.json()["config"] == SHOWN


async def test_credential_change_queues_old_device_cleanup_with_old_credentials(async_client):
    provider_id = await create_provider(async_client)
    with (
        patch(
            "backend.app.api.routes.notifications.notify_live_activities.schedule_cleanup", new_callable=AsyncMock
        ) as cleanup,
        patch(
            "backend.app.api.routes.notifications.notify_widgets.schedule_cleanup", new_callable=AsyncMock
        ) as widget_cleanup,
    ):
        response = await async_client.patch(
            f"/api/v1/notifications/{provider_id}",
            json={"config": {**SHOWN, "token": "replacement-token"}, "secret_actions": {"token": "replace"}},
        )
    assert response.status_code == 200
    cleanup.assert_awaited_once_with(provider_id, CONFIG)
    widget_cleanup.assert_awaited_once_with(provider_id, CONFIG)
    assert "token" not in response.json()["config"]


async def test_disabling_or_changing_push_thread_leaves_cleanup_to_durable_worker(async_client):
    provider_id = await create_provider(async_client)
    with (
        patch(
            "backend.app.api.routes.notifications.notify_live_activities.schedule_cleanup", new_callable=AsyncMock
        ) as cleanup,
        patch(
            "backend.app.api.routes.notifications.notify_widgets.schedule_cleanup", new_callable=AsyncMock
        ) as widget_cleanup,
    ):
        response = await async_client.patch(
            f"/api/v1/notifications/{provider_id}",
            json={"enabled": False, "config": {**SHOWN, "group_type": "workshop"}, **KEEP},
        )
    assert response.status_code == 200
    cleanup.assert_not_awaited()
    widget_cleanup.assert_not_awaited()


async def test_delete_queues_cleanup_before_forgetting_credentials(async_client):
    provider_id = await create_provider(async_client)
    with (
        patch(
            "backend.app.api.routes.notifications.notify_live_activities.schedule_cleanup", new_callable=AsyncMock
        ) as cleanup,
        patch(
            "backend.app.api.routes.notifications.notify_widgets.schedule_cleanup", new_callable=AsyncMock
        ) as widget_cleanup,
    ):
        response = await async_client.delete(f"/api/v1/notifications/{provider_id}")
    assert response.status_code == 200
    cleanup.assert_awaited_once_with(provider_id, CONFIG)
    widget_cleanup.assert_awaited_once_with(provider_id, CONFIG)
    assert (await async_client.get(f"/api/v1/notifications/{provider_id}")).status_code == 404


async def test_lock_screen_widgets_can_be_enabled_without_live_activities_or_push_events(async_client):
    config = {**CONFIG, "live_activities": False}
    response = await async_client.post(
        "/api/v1/notifications/",
        json={
            "name": "Printer widget",
            "provider_type": "notify",
            "config": config,
            "on_print_start": False,
            "on_print_complete": False,
            "on_print_failed": False,
        },
    )
    assert response.status_code == 200
    assert response.json()["config"] == {key: value for key, value in config.items() if key != "token"}
    assert response.json()["on_print_start"] is False
    assert response.json()["on_print_complete"] is False
    assert response.json()["on_print_failed"] is False


async def test_switching_provider_type_cleans_up_both_owned_notify_resources(async_client):
    provider_id = await create_provider(async_client)
    with (
        patch(
            "backend.app.api.routes.notifications.notify_live_activities.schedule_cleanup", new_callable=AsyncMock
        ) as cleanup,
        patch(
            "backend.app.api.routes.notifications.notify_widgets.schedule_cleanup", new_callable=AsyncMock
        ) as widget_cleanup,
    ):
        response = await async_client.patch(
            f"/api/v1/notifications/{provider_id}",
            json={"provider_type": "ntfy", "config": {"topic": "prints"}},
        )
    assert response.status_code == 200
    cleanup.assert_awaited_once_with(provider_id, CONFIG)
    widget_cleanup.assert_awaited_once_with(provider_id, CONFIG)


@pytest.mark.parametrize("disable", [{"config": {**SHOWN, "lock_screen_widgets": False}, **KEEP}, {"enabled": False}])
async def test_quick_widget_disable_and_reenable_preserves_cleanup_intent(async_client, db_session, disable):
    provider_id = await create_provider(async_client)
    row = NotificationLockScreenWidget(
        provider_id=provider_id,
        printer_id=42,
        credential_key=_credential_key(CONFIG),
        widget_id="WG123456",
        state="active",
        content="{}",
    )
    db_session.add(row)
    await db_session.commit()
    response = await async_client.patch(f"/api/v1/notifications/{provider_id}", json=disable)
    assert response.status_code == 200
    await db_session.refresh(row)
    assert row.state == "deleting"
    response = await async_client.patch(
        f"/api/v1/notifications/{provider_id}", json={"config": SHOWN, "enabled": True, **KEEP}
    )
    assert response.status_code == 200
    await db_session.refresh(row)
    assert row.state == "deleting"
    assert row.widget_id == "WG123456"


async def test_delete_is_one_transaction_under_the_anchor(async_client, db_session):
    """Ours (A7): cleanup is queued, the rows of both tables and the provider go, one commit.

    Upstream first commits ``enabled = False``; our route holds the credential anchor, which a
    commit would release, so the provider is still as it was while cleanup is queued.
    """
    from backend.app.core import database

    provider_id = await create_provider(async_client)
    db_session.add_all(
        [
            NotificationLiveActivity(
                provider_id=provider_id,
                printer_id=42,
                print_key="job:1",
                print_name="Benchy",
                credential_key=_credential_key(CONFIG),
                activity_id="LA123456",
                state="active",
                content="{}",
                failures=0,
            ),
            NotificationLockScreenWidget(
                provider_id=provider_id,
                printer_id=42,
                credential_key=_credential_key(CONFIG),
                widget_id="WG123456",
                state="active",
                content="{}",
                failures=0,
            ),
        ]
    )
    await db_session.commit()
    seen = []

    async def queued(provider, config):
        async with database.async_session() as db:
            row = await db.get(NotificationProvider, provider)
            seen.append(row.enabled if row is not None else None)

    with (
        patch("backend.app.api.routes.notifications.notify_live_activities.schedule_cleanup", side_effect=queued),
        patch("backend.app.api.routes.notifications.notify_widgets.schedule_cleanup", side_effect=queued),
    ):
        response = await async_client.delete(f"/api/v1/notifications/{provider_id}")

    assert response.status_code == 200
    assert seen == [True, True]
    db_session.expire_all()
    assert (await db_session.scalars(select(NotificationLiveActivity))).all() == []
    assert (await db_session.scalars(select(NotificationLockScreenWidget))).all() == []
    assert await db_session.get(NotificationProvider, provider_id) is None
