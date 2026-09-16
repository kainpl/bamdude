"""The in-app inbox is a channel in the providers list, delivered first and never blocking.

Spec: vault 60-specs/notification-center-spec §5.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.user import User
from backend.app.models.user_notification import UserNotification
from backend.app.services import notification_inbox
from backend.app.services.notification_inbox import (
    INBOX_CHANNEL,
    deliver,
    has_subscriber,
    prune_older_than,
    unread_count,
)
from backend.app.services.notification_service import NotificationService


async def _user(db, username, *, inbox_events=None, is_active=True) -> User:
    user = User(username=username, password_hash="x", role="user", is_active=is_active, inbox_events=inbox_events)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.mark.asyncio
async def test_fan_out_writes_one_row_per_subscribed_active_user(db_session):
    defaults = await _user(db_session, "defaults")
    everything = await _user(db_session, "everything", inbox_events=["print_complete", "print_failed"])
    nothing = await _user(db_session, "nothing", inbox_events=[])
    inactive = await _user(db_session, "inactive", is_active=False)

    with patch.object(notification_inbox.ws_manager, "broadcast_to_user", new_callable=AsyncMock) as ws:
        got = await deliver(
            db_session, event_type="print_failed", title="T", message="M", printer_id=7, printer_name="P1"
        )
        rows = (await db_session.execute(select(UserNotification))).scalars().all()
        assert sorted(got) == sorted([defaults.id, everything.id])
        assert {r.user_id for r in rows} == {defaults.id, everything.id}
        assert all(r.severity == "error" and r.printer_name == "P1" and r.read_at is None for r in rows)
        assert nothing.id not in {r.user_id for r in rows} and inactive.id not in {r.user_id for r in rows}
        assert ws.await_count == 2
        payload = ws.await_args_list[0].args[1]
        assert payload["type"] == "inbox_item"
        assert payload["data"]["unread_count"] == 1
        assert payload["data"]["item"]["title"] == "T" and payload["data"]["item"]["group"] == "print"

    # info events reach only explicit subscribers
    with patch.object(notification_inbox.ws_manager, "broadcast_to_user", new_callable=AsyncMock):
        got = await deliver(db_session, event_type="print_complete", title="T2", message="M2")
    assert got == [everything.id]
    assert await unread_count(db_session, everything.id) == 2
    assert await unread_count(db_session, defaults.id) == 1


@pytest.mark.asyncio
async def test_no_recipients_writes_nothing(db_session):
    await _user(db_session, "nothing", inbox_events=[])
    with patch.object(notification_inbox.ws_manager, "broadcast_to_user", new_callable=AsyncMock) as ws:
        assert await deliver(db_session, event_type="print_failed", title="T", message="M") == []
    assert ws.await_count == 0
    assert (await db_session.execute(select(UserNotification))).scalars().all() == []


@pytest.mark.asyncio
async def test_the_channel_rides_every_provider_lookup(db_session):
    service = NotificationService()
    providers = await service._get_providers_for_event(db_session, "on_print_start", None)
    assert providers == [INBOX_CHANNEL]
    providers = await service._get_providers_for_event(db_session, "on_sensor_threshold", None, unscoped_only=True)
    assert providers == [INBOX_CHANNEL]


@pytest.mark.asyncio
async def test_a_progress_subscriber_is_not_muted_by_somebody_elses_floor(db_session):
    """print_progress is `info`, so nobody holds it by default — but somebody who ticks it means it.

    The duration floor belongs to each provider, not to the inbox, and an
    operator who asked for milestones should not lose them because an unrelated
    ntfy provider is set to ignore short prints.
    """
    service = NotificationService()
    await _user(db_session, "wants-progress", inbox_events=["print_progress"])
    muted = AsyncMock()
    muted.provider_type = "ntfy"
    muted.name = "ntfy"
    muted.id = 5
    muted.progress_min_duration_minutes = 60

    with (
        patch.object(service, "_get_providers_for_event", new_callable=AsyncMock, return_value=[muted, INBOX_CHANNEL]),
        patch.object(service, "_build_message_from_template", new_callable=AsyncMock, return_value=("T", "M")),
        patch.object(service, "_send_to_providers", new_callable=AsyncMock) as send,
    ):
        await service.on_print_progress(1, "P", "f.3mf", 50, db_session, estimated_minutes=5)

    send.assert_awaited_once()
    # The muted provider is still excluded — only the channel rides through.
    assert [p.provider_type for p in send.await_args.args[0]] == ["inbox"]


@pytest.mark.asyncio
async def test_the_channel_never_defeats_a_progress_duration_floor(db_session):
    """The camera grab stays lazy: an inbox nobody subscribed to must not pay for it."""
    service = NotificationService()
    muted = AsyncMock()
    muted.provider_type = "ntfy"
    muted.name = "ntfy"
    muted.id = 5
    muted.progress_min_duration_minutes = 60
    supplier = AsyncMock(return_value=b"jpeg")

    with (
        patch.object(service, "_get_providers_for_event", new_callable=AsyncMock, return_value=[muted, INBOX_CHANNEL]),
        patch.object(service, "_send_to_providers", new_callable=AsyncMock) as send,
    ):
        await service.on_print_progress(1, "P", "f.3mf", 50, db_session, estimated_minutes=5, image_supplier=supplier)
    send.assert_not_awaited()
    supplier.assert_not_awaited()

    # A provider that clears its floor pulls the channel along with it.
    passing = AsyncMock()
    passing.provider_type = "ntfy"
    passing.name = "ntfy"
    passing.id = 6
    passing.progress_min_duration_minutes = 0
    with (
        patch.object(
            service, "_get_providers_for_event", new_callable=AsyncMock, return_value=[passing, INBOX_CHANNEL]
        ),
        patch.object(service, "_build_message_from_template", new_callable=AsyncMock, return_value=("T", "M")),
        patch.object(service, "_send_to_providers", new_callable=AsyncMock) as send,
    ):
        await service.on_print_progress(1, "P", "f.3mf", 50, db_session, estimated_minutes=5)
    assert INBOX_CHANNEL in send.await_args.args[0]


@pytest.mark.asyncio
async def test_send_to_providers_delivers_the_channel_first_and_never_logs_it(
    db_session, notification_provider_factory
):
    """The list arrives with the channel LAST; delivery must still happen FIRST.

    The providers come from the real lookup on purpose. A hand-built
    ``[INBOX_CHANNEL, provider]`` would only assert the order of its own
    literal and would pass whatever the dispatcher did — while the order the
    dispatcher actually receives is provider-then-channel, because
    ``_get_providers_for_event`` appends the channel at the end. A dead SMTP
    host ahead of the inbox in the loop is the delay this test exists to catch.
    """
    service = NotificationService()
    await notification_provider_factory(name="ntfy", on_print_start=True)
    providers = await service._get_providers_for_event(db_session, "on_print_start", None)
    assert [p.provider_type for p in providers] == ["ntfy", "inbox"]

    order: list[str] = []

    async def fake_deliver(db, **kwargs):
        order.append("inbox")
        return []

    async def fake_send(*args, **kwargs):
        order.append("provider")
        return True, None

    with (
        patch.object(notification_inbox, "deliver", side_effect=fake_deliver) as deliver_mock,
        patch.object(service, "_send_to_provider", side_effect=fake_send),
        patch.object(service, "_update_provider_status", new_callable=AsyncMock),
        patch.object(service, "_log_notification", new_callable=AsyncMock) as log_mock,
    ):
        await service._send_to_providers(providers, "T", "M", db_session, "print_start", 1, "P")
    assert order == ["inbox", "provider"]
    deliver_mock.assert_awaited_once()
    assert deliver_mock.await_args.kwargs["event_type"] == "print_start"
    assert log_mock.await_count == 1  # the provider, never the channel


@pytest.mark.asyncio
async def test_an_inbox_failure_does_not_stop_the_providers(db_session):
    service = NotificationService()
    provider = AsyncMock()
    provider.provider_type = "ntfy"
    provider.name = "ntfy"
    provider.id = 5
    provider.daily_digest_enabled = False
    with (
        patch.object(notification_inbox, "deliver", side_effect=RuntimeError("boom")),
        patch.object(service, "_send_to_provider", new_callable=AsyncMock, return_value=(True, None)) as send,
        patch.object(service, "_update_provider_status", new_callable=AsyncMock),
        patch.object(service, "_log_notification", new_callable=AsyncMock),
    ):
        await service._send_to_providers([INBOX_CHANNEL, provider], "T", "M", db_session, "print_start")
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_in_app_renders_the_template_and_delivers_without_providers(db_session):
    service = NotificationService()
    await _user(db_session, "u", inbox_events=["print_start"])
    with (
        patch.object(service, "_build_message_from_template", new_callable=AsyncMock, return_value=("Ti", "Bo")),
        patch.object(notification_inbox.ws_manager, "broadcast_to_user", new_callable=AsyncMock),
    ):
        await service.notify_in_app(db_session, "print_start", {"printer": "P"}, printer_id=1, printer_name="P")
    rows = (await db_session.execute(select(UserNotification))).scalars().all()
    assert [(r.title, r.message, r.event_type) for r in rows] == [("Ti", "Bo", "print_start")]


@pytest.mark.asyncio
async def test_has_subscriber_answers_for_the_work_behind_an_event(db_session):
    """What ``main.py``'s bed-cooldown monitor asks before polling a printer for
    half an hour: is there anybody at the other end at all?"""
    # bed_cooled is info — nobody receives it under the default subscription.
    await _user(db_session, "defaults")
    assert await has_subscriber(db_session, "bed_cooled") is False
    assert await has_subscriber(db_session, "print_failed") is True  # error, in the defaults

    await _user(db_session, "muted", inbox_events=[])
    assert await has_subscriber(db_session, "bed_cooled") is False

    inactive = await _user(db_session, "inactive", inbox_events=["bed_cooled"], is_active=False)
    assert inactive.id is not None
    assert await has_subscriber(db_session, "bed_cooled") is False

    await _user(db_session, "asked_for_it", inbox_events=["bed_cooled"])
    assert await has_subscriber(db_session, "bed_cooled") is True


@pytest.mark.asyncio
async def test_prune_removes_old_rows_read_or_not(db_session):
    from datetime import datetime, timedelta

    user = await _user(db_session, "u")
    old = datetime.utcnow() - timedelta(days=40)
    db_session.add_all(
        [
            UserNotification(
                user_id=user.id,
                event_type="print_failed",
                severity="error",
                title="a",
                message="m",
                created_at=old,
                read_at=old,
            ),
            UserNotification(
                user_id=user.id, event_type="print_failed", severity="error", title="b", message="m", created_at=old
            ),
            UserNotification(user_id=user.id, event_type="print_failed", severity="error", title="c", message="m"),
        ]
    )
    await db_session.commit()
    assert await prune_older_than(db_session, 30) == 2
    left = (await db_session.execute(select(UserNotification.title))).scalars().all()
    assert left == ["c"]
