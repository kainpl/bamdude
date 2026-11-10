"""AA4: refresh exact historical mail defaults without rewriting operator copy."""

import json
import re
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.migrations import m218_account_mail_default_bodies as migration
from backend.app.models.notification_template import DEFAULT_TEMPLATES, NotificationTemplate
from backend.app.services import email_service

_EVENTS = ("user_created", "password_reset", "password_reset_link")
_FROZEN = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures/account_mail_defaults_pre_g01.json").read_text(encoding="utf-8")
)
_DATA = Path(__file__).resolve().parents[2] / "app/data"
_WHEN = datetime(2020, 1, 2, 3, 4, 5)


def _current(variant):
    if variant == "en_seed":
        return {row["event_type"]: row for row in DEFAULT_TEMPLATES if row["event_type"] in _EVENTS}
    return json.loads((_DATA / f"notification_templates_{variant}.json").read_text(encoding="utf-8"))


async def _upgrade(db):
    await migration.upgrade(await db.connection())
    await db.commit()


async def _rows(db):
    return (
        await db.execute(
            select(
                NotificationTemplate.event_type,
                NotificationTemplate.name,
                NotificationTemplate.title_template,
                NotificationTemplate.body_template,
                NotificationTemplate.is_default,
                NotificationTemplate.created_at,
                NotificationTemplate.updated_at,
            ).order_by(NotificationTemplate.event_type)
        )
    ).all()


@pytest.mark.parametrize("variant", ["en_seed", "en", "uk"])
@pytest.mark.parametrize("is_default", [True, False])
async def test_old_defaults_upgrade_once_and_render_without_empty_credential_labels(
    db_session, monkeypatch, variant, is_default
):
    assert _FROZEN["source_revision"] == "ecf1c4bcc8824acb656b0c15dad52b5fb0a1e176"
    for event in _EVENTS:
        old = _FROZEN[variant][event]
        db_session.add(
            NotificationTemplate(
                event_type=event,
                name="Kept name",
                title_template="Kept title {username}",
                body_template=old["body_template"],
                is_default=is_default,
                created_at=_WHEN,
                updated_at=_WHEN,
            )
        )
    await db_session.commit()
    await _upgrade(db_session)
    first = await _rows(db_session)
    current = _current(variant)
    for event, name, title, body, flag, created, updated in first:
        assert body == current[event]["body_template"]
        assert (name, title, flag, created, updated) == (
            "Kept name",
            "Kept title {username}",
            is_default,
            _WHEN,
            _WHEN,
        )
        assert not any(placeholder in body for placeholder in ("{password}", "{login_url}", "{reset_url}"))
    await _upgrade(db_session)
    assert await _rows(db_session) == first

    monkeypatch.setattr("backend.app.i18n.current_language", lambda: "uk" if variant == "uk" else "en")
    url = "https://mail.invalid/sign-in"
    password = "Synthetic-AA4-Password!"
    messages = [
        await email_service.create_welcome_email_from_template(db_session, "alice", password, url),
        await email_service.create_password_reset_email_from_template(db_session, "alice", password, url),
        await email_service.create_password_reset_link_email_from_template(db_session, "alice", url, 3),
    ]
    for index, (subject, body, html) in enumerate(messages):
        assert subject == "Kept title alice"
        assert body.count(url) == 1
        assert f'href="{url}"' in html
        assert not re.search(r"(?m)^(?:Password|New Password|Login at|Пароль|Новий пароль|Увійти):[ \t]*$", body)
        assert not any(token in body for token in ("{password}", "{login_url}", "{reset_url}"))
        if index < 2:
            assert body.count(password) == 1
        else:
            assert password not in body and password not in html
            assert "Open the link below to choose a new one:" not in body
            assert "Відкрийте посилання нижче, щоб задати новий:" not in body
            expected = (
                "скористайтеся захищеним посиланням наприкінці цього листа"
                if variant == "uk"
                else "Use the protected reset link at the end of this email"
            )
            assert expected in body


@pytest.mark.parametrize("is_default", [True, False])
@pytest.mark.parametrize("customization", ["suffix", "prefix", "custom"])
async def test_custom_bodies_are_preserved_byte_exact_even_with_default_flag(db_session, is_default, customization):
    for event in _EVENTS:
        old = _FROZEN["uk"][event]["body_template"]
        body = {
            "suffix": old + "\n",
            "prefix": "Team copy\n" + old,
            "custom": "Custom\r\n{password}\n{login_url}\n{reset_url}\t",
        }[customization]
        db_session.add(
            NotificationTemplate(
                event_type=event,
                name="Custom name",
                title_template="Custom subject",
                body_template=body,
                is_default=is_default,
                created_at=_WHEN,
                updated_at=_WHEN,
            )
        )
    # A different event is never rewritten merely because it reuses old copy.
    db_session.add(
        NotificationTemplate(
            event_type="print_start",
            name="Other event",
            title_template="Other title",
            body_template=_FROZEN["en"]["user_created"]["body_template"],
            is_default=True,
            created_at=_WHEN,
            updated_at=_WHEN,
        )
    )
    await db_session.commit()
    before = await _rows(db_session)
    await _upgrade(db_session)
    await _upgrade(db_session)
    assert await _rows(db_session) == before
    assert [row.body_template.encode("utf-8") for row in await _rows(db_session)] == [
        row.body_template.encode("utf-8") for row in before
    ]


@pytest.mark.parametrize("variant", ["en_seed", "en", "uk"])
async def test_current_fresh_install_defaults_are_already_correct_and_unchanged(db_session, variant):
    current = _current(variant)
    for event in _EVENTS:
        db_session.add(
            NotificationTemplate(
                event_type=event,
                name=current[event]["name"],
                title_template=current[event]["title_template"],
                body_template=current[event]["body_template"],
                is_default=True,
                created_at=_WHEN,
                updated_at=_WHEN,
            )
        )
    await db_session.commit()
    before = await _rows(db_session)
    await _upgrade(db_session)
    await _upgrade(db_session)
    assert await _rows(db_session) == before


async def test_fresh_m001_seed_remains_equal_to_current_model_defaults(test_engine, db_session):
    from backend.app.migrations.m001_bamdude_baseline import _seed_notification_templates

    await _seed_notification_templates(async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False))
    before = await _rows(db_session)
    await _upgrade(db_session)
    assert await _rows(db_session) == before
    by_event = {row.event_type: row.body_template for row in before}
    for event in _EVENTS:
        assert by_event[event] == _current("en_seed")[event]["body_template"]


async def test_upgrade_uses_only_historical_columns_without_orm_defaults(tmp_path):
    # Later ORM columns/defaults cannot enter this frozen UPDATE.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'minimal.db'}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE notification_templates (event_type TEXT, body_template TEXT)"))
            await conn.execute(
                text("INSERT INTO notification_templates VALUES (:event, :body)"),
                {
                    "event": "user_created",
                    "body": _FROZEN["en"]["user_created"]["body_template"],
                },
            )
            await migration.upgrade(conn)
            await migration.upgrade(conn)
            assert (await conn.execute(text("SELECT body_template FROM notification_templates"))).scalar_one() == (
                _current("en")["user_created"]["body_template"]
            )
    finally:
        await engine.dispose()
