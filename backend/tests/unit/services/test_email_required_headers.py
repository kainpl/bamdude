"""Every outgoing mail carries ``Date`` and ``Message-ID`` (upstream #3376)."""

import email
import smtplib
from email.mime.multipart import MIMEMultipart
from email.utils import parseaddr, parsedate_to_datetime
from types import SimpleNamespace

import pytest

from backend.app.services import email_service
from backend.app.services.email_service import add_required_headers
from backend.app.services.notification_service import NotificationService


def test_both_headers_are_added():
    msg = MIMEMultipart()
    add_required_headers(msg, "farm@example.org")

    assert parsedate_to_datetime(msg["Date"]) is not None
    assert msg["Message-ID"].startswith("<") and msg["Message-ID"].endswith("@example.org>")


def test_the_domain_comes_from_a_display_name_address_too():
    msg = MIMEMultipart()
    add_required_headers(msg, "Farm <farm@prints.example>")

    assert msg["Message-ID"].endswith("@prints.example>")


@pytest.mark.parametrize("sender", ["", None, "no-at-sign"])
def test_a_sender_without_a_domain_gets_the_fallback(sender):
    msg = MIMEMultipart()
    add_required_headers(msg, sender)

    assert msg["Message-ID"].endswith("@bamdude.local>")


def test_headers_already_present_are_kept():
    msg = MIMEMultipart()
    msg["Date"] = "Mon, 01 Jan 2024 00:00:00 +0000"
    msg["Message-ID"] = "<kept@example.org>"
    add_required_headers(msg, "farm@example.org")

    assert msg.get_all("Date") == ["Mon, 01 Jan 2024 00:00:00 +0000"]
    assert msg.get_all("Message-ID") == ["<kept@example.org>"]


def test_two_messages_get_two_ids():
    first, second = MIMEMultipart(), MIMEMultipart()
    add_required_headers(first, "farm@example.org")
    add_required_headers(second, "farm@example.org")

    assert first["Message-ID"] != second["Message-ID"]


class _Recorder:
    sent: list = []

    def __init__(self, *args, **kwargs):
        pass

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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def smtp(monkeypatch):
    _Recorder.sent = []
    monkeypatch.setattr(smtplib, "SMTP", _Recorder)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _Recorder)
    return _Recorder.sent


def test_account_mail_carries_the_headers_and_a_well_formed_sender(smtp):
    settings = SimpleNamespace(
        smtp_host="mail.example.org",
        smtp_port=25,
        smtp_security="none",
        smtp_auth_enabled=False,
        smtp_username=None,
        smtp_password=None,
        smtp_from_name="Ferma, Друк",
        smtp_from_email="farm@example.org",
    )

    email_service.send_email(settings, "user@example.org", "Subject", "Body")

    parsed = email.message_from_string(smtp[-1])
    assert parsed["Date"] and parsed["Message-ID"].endswith("@example.org>")
    assert parseaddr(parsed["From"])[1] == "farm@example.org"


@pytest.mark.asyncio
async def test_the_email_notification_channel_carries_the_headers(smtp):
    config = {
        "smtp_server": "mail.example.org",
        "smtp_port": 25,
        "security": "none",
        "auth_enabled": False,
        "from_email": "farm@example.org",
        "to_email": "user@example.org",
    }

    ok, detail = await NotificationService()._send_email(config, "Title", "Body")

    assert ok, detail
    parsed = email.message_from_string(smtp[-1])
    assert parsed["Date"] and parsed["Message-ID"].endswith("@example.org>")
