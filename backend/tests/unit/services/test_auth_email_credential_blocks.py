"""Editable authentication copy cannot redirect a password or recovery link."""

import html
import json
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app.services import email_service as mail


class Links(HTMLParser):
    def __init__(self, value):
        super().__init__()
        self.hrefs = []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.hrefs.extend(value for name, value in attrs if name in {"href", "src"})


@pytest.mark.parametrize("kind", ["welcome", "admin-reset", "reset-link"])
@pytest.mark.parametrize("fallback", [False, True])
async def test_secret_is_only_in_code_owned_block(monkeypatch, kind, fallback):
    secret = "CANARY_пароль<&\"'"
    url = "https://farm.example/reset#reset_token=CANARY_LINK&next=home"
    template = SimpleNamespace(
        title_template="Notice {password} {reset_url} {login_url}",
        body_template=(
            "Hello {username}\nhttps://attacker.invalid/?p={password}&r={reset_url}&u={login_url}\n"
            '<a href="https://attacker.invalid/{password}">trap</a>\n'
            "https://attacker.invalid/%7Bpassword%7D\nKeep my custom copy"
        ),
    )
    getter = AsyncMock(return_value=None if fallback else template)
    monkeypatch.setattr(mail, "get_notification_template", getter)
    monkeypatch.setattr("backend.app.i18n.current_language", lambda: "en")
    if kind == "welcome":
        result = await mail.create_welcome_email_from_template(None, '<img src="evil">', secret, url)
        expected_event = "user_created"
    elif kind == "admin-reset":
        result = await mail.create_password_reset_email_from_template(None, '<img src="evil">', secret, url)
        expected_event = "password_reset"
    else:
        result = await mail.create_password_reset_link_email_from_template(None, '<img src="evil">', url, 2)
        expected_event = "password_reset_link"
    subject, text, markup = result
    getter.assert_awaited_once_with(None, expected_event)
    assert secret not in subject and url not in subject
    assert '<img src="evil">' not in markup
    assert Links(markup).hrefs == [url]
    assert text.count(url) == 1
    if kind != "reset-link":
        assert text.count(secret) == 1 and markup.count(html.escape(secret)) == 1
    else:
        assert secret not in text
        assert "Link expires in hours: 2" in text
    if not fallback:
        assert "Keep my custom copy" in text
        editable_markup, owned = markup.split('<div data-auth-credentials="true">')
        assert url not in editable_markup and secret not in editable_markup
        assert html.escape(url, quote=True) in owned


@pytest.mark.parametrize("builder", [mail.create_welcome_email, mail.create_password_reset_email])
def test_builtin_password_html_and_text_control_safety(builder):
    subject, text, markup = builder("name\r\nForged: value", '<"secret&>', 'https://farm.example/?a="x"&b=2')
    assert "\r" not in text and "name\\u000d\\u000aForged" in text
    assert '<"secret&>' not in markup
    assert "&lt;&quot;secret&amp;&gt;" in markup
    assert Links(markup).hrefs == ['https://farm.example/?a="x"&b=2']
    assert "\n" not in subject


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://:password@host/", "https://host/\r\nInjected: yes"])
def test_server_link_still_rejects_active_scheme_or_controls(url):
    with pytest.raises(ValueError, match="Invalid authentication email URL"):
        mail.create_welcome_email("name", "secret", url)


async def test_protected_block_uses_system_language(monkeypatch):
    monkeypatch.setattr("backend.app.i18n.current_language", lambda: "uk")
    monkeypatch.setattr(mail, "get_notification_template", AsyncMock(return_value=None))
    _, text, markup = await mail.create_password_reset_link_email_from_template(None, "імʼя", "https://farm/reset", 1)
    assert "Скинути пароль: https://farm/reset" in text
    assert "Посилання дійсне, год: 1" in markup


def test_shipped_defaults_and_preview_never_advertise_secret_placeholders():
    from backend.app.models.notification_template import DEFAULT_TEMPLATES
    from backend.app.schemas.notification_template import EVENT_VARIABLES, SAMPLE_DATA

    events = {"user_created", "password_reset", "password_reset_link"}
    secret_fields = {"password", "login_url", "reset_url"}
    for event in events:
        assert secret_fields.isdisjoint(EVENT_VARIABLES[event])
        assert secret_fields.isdisjoint(SAMPLE_DATA[event])
    sources = [{row["event_type"]: row for row in DEFAULT_TEMPLATES}]
    data_dir = Path(mail.__file__).resolve().parents[1] / "data"
    sources += [
        json.loads((data_dir / f"notification_templates_{lang}.json").read_text(encoding="utf-8"))
        for lang in ("en", "uk")
    ]
    for source in sources:
        for event in events:
            for field in secret_fields:
                assert "{" + field + "}" not in source[event]["body_template"]
