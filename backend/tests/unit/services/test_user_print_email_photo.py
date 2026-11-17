"""The personal "your print finished" e-mail shows the finish photo."""

import email
import smtplib
from types import SimpleNamespace

import pytest

from backend.app.services import email_service
from backend.app.services.email_service import INLINE_IMAGE_CID

FRAME = b"\xff\xd8\xff\xe0 finish"


class _Recorder:
    sent: list = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, *args, **kwargs):
        pass

    def login(self, *args, **kwargs):
        pass

    def send_message(self, msg, *args, **kwargs):
        type(self).sent.append(msg.as_string())

    def sendmail(self, sender, to, text):
        type(self).sent.append(text)

    def quit(self):
        pass


SMTP = SimpleNamespace(
    smtp_host="mail.example.org",
    smtp_port=25,
    smtp_security="none",
    smtp_auth_enabled=False,
    smtp_username=None,
    smtp_password=None,
    smtp_from_name="Farm",
    smtp_from_email="farm@example.org",
)


@pytest.fixture
def sent(monkeypatch):
    _Recorder.sent = []
    monkeypatch.setattr(smtplib, "SMTP", _Recorder)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _Recorder)

    async def smtp(db):
        return SMTP

    monkeypatch.setattr(email_service, "get_smtp_settings", smtp)
    return _Recorder.sent


def template(monkeypatch, body):
    async def get(db, event_type):
        return SimpleNamespace(title_template="Done: {filename}", body_template=body)

    monkeypatch.setattr(email_service, "get_notification_template", get)


def parts(raw):
    message = email.message_from_string(raw)
    html = next(p.get_payload(decode=True).decode() for p in message.walk() if p.get_content_type() == "text/html")
    images = [p for p in message.walk() if p.get_content_maintype() == "image"]
    return message, html, images


async def send(**kwargs):
    await email_service.send_user_print_notification(
        db=None, event_type="user_print_complete", user_email="u@example.org", username="u", **kwargs
    )


@pytest.mark.asyncio
async def test_the_photo_follows_the_text_when_the_template_does_not_place_it(sent, monkeypatch):
    template(monkeypatch, "{printer} finished {filename}.")

    await send(variables={"printer": "P1", "filename": "benchy"}, image_data=FRAME)

    message, html, images = parts(sent[-1])
    assert message.get_content_type() == "multipart/related"
    assert f"cid:{INLINE_IMAGE_CID}" in html
    assert images[0]["Content-ID"] == f"<{INLINE_IMAGE_CID}>"
    assert images[0].get_payload(decode=True) == FRAME


@pytest.mark.asyncio
async def test_the_photo_takes_the_place_of_its_link(sent, monkeypatch):
    template(monkeypatch, "Photo: {finish_photo_url}\nBye")
    link = "https://farm.example/api/v1/notifications/photos/x.jpg"

    await send(variables={"printer": "P1", "filename": "benchy", "finish_photo_url": link}, image_data=FRAME)

    _message, html, _images = parts(sent[-1])
    assert link not in html
    assert html.count(f"cid:{INLINE_IMAGE_CID}") == 1


@pytest.mark.asyncio
async def test_a_letter_without_a_photo_is_what_it_was(sent, monkeypatch):
    template(monkeypatch, "{printer} finished {filename}. {finish_photo_url}")

    await send(variables={"printer": "P1", "filename": "benchy"})

    message, html, images = parts(sent[-1])
    assert message.get_content_type() == "multipart/alternative"
    assert images == []
    assert "cid:" not in html and "{finish_photo_url}" not in html
