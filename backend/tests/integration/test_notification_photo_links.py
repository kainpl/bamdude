"""Camera frames in notifications: who gets bytes, who gets a link, and what the link opens.

A frame goes as bytes to the channels that take bytes. Home Assistant, Bark and
a Slack-format webhook fetch a URL themselves, and ``{finish_photo_url}`` is
opened from a chat: those lead to a short-lived copy with an unguessable name,
never to the archive's own photo, which is behind the media token.
"""

from __future__ import annotations

import json
import re

import pytest

from backend.app.models.notification import NotificationProvider
from backend.app.models.settings import Settings
from backend.app.services.notification_service import NotificationService
from backend.app.utils import notification_photos

pytestmark = pytest.mark.asyncio

FRAME = b"\xff\xd8\xff\xe0 a camera frame"
LINK = re.compile(r"^https://farm\.example/api/v1/notifications/photos/[a-z0-9_]+_\d{8}_\d{6}_[A-Za-z0-9_-]{32}\.jpg$")


@pytest.fixture(autouse=True)
def _photo_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_photos, "notification_photos_dir", lambda: tmp_path / "notification-photos")


async def external_url(db, value="https://farm.example/"):
    db.add(Settings(key="external_url", value=value))
    await db.commit()


async def provider(db, kind, config, **fields) -> NotificationProvider:
    row = NotificationProvider(name=kind, provider_type=kind, enabled=True, config=json.dumps(config), **fields)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def capture(monkeypatch, method):
    seen = {}

    async def fake(self, *args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return True, "ok"

    monkeypatch.setattr(NotificationService, method, fake)
    return seen


async def test_the_link_opens_a_copy_of_the_frame(async_client, db_session):
    await external_url(db_session)

    link = await NotificationService().photo_link(db_session, FRAME, "first_layer_complete")

    assert LINK.match(link)
    response = await async_client.get(link.removeprefix("https://farm.example"))
    assert response.status_code == 200
    assert response.content == FRAME
    assert response.headers["cache-control"] == "private, no-store"


async def test_one_frame_is_copied_once_for_every_channel(async_client, db_session):
    await external_url(db_session)
    service = NotificationService()

    first = await service.photo_link(db_session, FRAME, "print_complete")
    second = await service.photo_link(db_session, FRAME, "print_complete")

    assert first == second
    assert len(list(notification_photos.notification_photos_dir().iterdir())) == 1


async def test_without_an_external_address_a_service_gets_no_link(async_client, db_session):
    service = NotificationService()

    assert await service.photo_link(db_session, FRAME, "print_complete") is None
    relative = await service.photo_link(db_session, FRAME, "print_complete", absolute_only=False)
    assert relative.startswith("/api/v1/notifications/photos/")


async def test_no_frame_no_link(async_client, db_session):
    await external_url(db_session)

    assert await NotificationService().photo_link(db_session, None, "print_complete") is None


async def test_a_name_that_was_never_written_is_not_found(async_client, db_session):
    response = await async_client.get("/api/v1/notifications/photos/print_20260101_000000_" + "a" * 32 + ".jpg")

    assert response.status_code == 404


async def test_home_assistant_is_given_the_link(async_client, db_session, monkeypatch):
    await external_url(db_session)
    seen = capture(monkeypatch, "_send_homeassistant")
    row = await provider(db_session, "homeassistant", {"service": "notify.mobile_app_phone"})

    await NotificationService()._send_to_provider(
        row, "t", "m", db_session, image_data=FRAME, event_type="printer_error"
    )

    assert LINK.match(seen["kwargs"]["image_url"])


async def test_bark_is_given_the_link(async_client, db_session, monkeypatch):
    await external_url(db_session)
    seen = capture(monkeypatch, "_send_bark")
    row = await provider(db_session, "bark", {"device_key": "k"})

    await NotificationService()._send_to_provider(
        row, "t", "m", db_session, image_data=FRAME, event_type="printer_error"
    )

    assert LINK.match(seen["kwargs"]["image_url"])


@pytest.mark.parametrize(("payload_format", "linked"), [("slack", True), ("generic", False)])
async def test_only_a_slack_format_webhook_is_given_the_link(
    async_client, db_session, monkeypatch, payload_format, linked
):
    await external_url(db_session)
    seen = capture(monkeypatch, "_send_webhook")
    row = await provider(
        db_session, "webhook", {"webhook_url": "https://hooks.example/x", "payload_format": payload_format}
    )

    await NotificationService()._send_to_provider(
        row, "t", "m", db_session, image_data=FRAME, event_type="printer_error"
    )

    assert bool(seen["kwargs"]["image_url"]) is linked


async def test_a_channel_that_opted_out_gets_neither_bytes_nor_a_link(async_client, db_session, monkeypatch):
    await external_url(db_session)
    seen = capture(monkeypatch, "_send_homeassistant")
    ntfy = capture(monkeypatch, "_send_ntfy")
    ha = await provider(db_session, "homeassistant", {"service": "notify.mobile_app_phone"}, attach_photo=False)
    push = await provider(db_session, "ntfy", {"server": "https://ntfy.invalid", "topic": "x"}, attach_photo=False)
    service = NotificationService()

    await service._send_to_provider(ha, "t", "m", db_session, image_data=FRAME, event_type="printer_error")
    await service._send_to_provider(push, "t", "m", db_session, image_data=FRAME, event_type="printer_error")

    assert seen["kwargs"]["image_url"] is None
    assert ntfy["kwargs"]["image_data"] is None
    assert not notification_photos.notification_photos_dir().exists()


async def test_a_row_from_before_the_option_still_gets_photos(async_client, db_session, monkeypatch):
    seen = capture(monkeypatch, "_send_ntfy")
    row = await provider(db_session, "ntfy", {"server": "https://ntfy.invalid", "topic": "x"})
    row.attach_photo = None

    await NotificationService()._send_to_provider(
        row, "t", "m", db_session, image_data=FRAME, event_type="printer_error"
    )

    assert seen["kwargs"]["image_data"] == FRAME


async def test_bark_puts_the_link_into_its_payload(monkeypatch):
    posted = {}

    class Response:
        status_code = 200

        def json(self):
            return {"code": 200}

    class Client:
        async def post(self, url, json=None, **kwargs):
            posted.update(json)
            return Response()

    service = NotificationService()

    async def client():
        return Client()

    monkeypatch.setattr(service, "_get_client", client)

    ok, _ = await service._send_bark({"device_key": "k"}, "t", "m", image_url="https://farm.example/p.jpg")

    assert ok and posted["icon"] == "https://farm.example/p.jpg"


async def test_the_plate_check_notification_carries_the_judged_frame(async_client, db_session, monkeypatch):
    sent = {}

    async def providers(self, db, field, printer_id=None, **kwargs):
        return ["one"]

    async def template(self, db, event, variables):
        return "title", "body"

    async def send(self, providers, title, message, db, event, *args, **kwargs):
        sent.update(kwargs)

    monkeypatch.setattr(NotificationService, "_get_providers_for_event", providers)
    monkeypatch.setattr(NotificationService, "_build_message_from_template", template)
    monkeypatch.setattr(NotificationService, "_send_to_providers", send)

    await NotificationService().on_plate_not_empty(1, "P1", db_session, difference_percent=4.2, image_data=FRAME)

    assert sent["image_data"] == FRAME


async def test_the_obico_notice_carries_the_flagged_frame(monkeypatch):
    from backend.app.services import obico_actions

    seen = {}

    async def notify(printer_id, printer_name, task_name, score, action, frame=None):
        seen["frame"] = frame

    async def name(printer_id):
        return "P1"

    monkeypatch.setattr(obico_actions, "_notify", notify)
    monkeypatch.setattr(obico_actions, "_get_printer_name", name, raising=False)

    await obico_actions.execute_action(1, "notify", "benchy", 0.9, FRAME)

    assert seen["frame"] == FRAME
