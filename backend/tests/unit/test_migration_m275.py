"""m275 seeds the not-heating auto-drying template for databases that predate it (C08, upstream #3478)."""

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.migrations import m275_auto_drying_not_heating_template as migration
from backend.app.models.notification_template import DEFAULT_TEMPLATES

_EVENT = "ams_drying_not_heating"
_DATA = Path(__file__).resolve().parents[2] / "app/data"


async def _rows(sessions):
    async with sessions() as db:
        return (
            await db.execute(
                text(
                    "SELECT name, title_template, body_template, is_default FROM notification_templates "
                    "WHERE event_type = :e"
                ),
                {"e": _EVENT},
            )
        ).all()


async def _language(sessions, value):
    async with sessions() as db:
        await db.execute(text("INSERT INTO settings (key, value) VALUES ('language', :v)"), {"v": value})
        await db.commit()


@pytest.mark.parametrize("language", [None, "en", "uk"])
async def test_the_template_is_seeded_once_in_the_system_language(test_engine, language):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    if language is not None:
        await _language(sessions, language)

    await migration.upgrade(None)
    await migration.seed(sessions)
    await migration.seed(sessions)  # a rerun adds no second row

    rows = await _rows(sessions)
    assert len(rows) == 1
    if language == "uk":
        expected = json.loads((_DATA / "notification_templates_uk.json").read_text(encoding="utf-8"))[_EVENT]
    else:
        expected = next(t for t in DEFAULT_TEMPLATES if t["event_type"] == _EVENT)
    assert tuple(rows[0]) == (expected["name"], expected["title_template"], expected["body_template"], True)


async def test_an_existing_row_is_left_as_the_operator_wrote_it(test_engine):
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as db:
        await db.execute(
            text(
                "INSERT INTO notification_templates (event_type, name, title_template, body_template, is_default) "
                "VALUES (:e, 'Mine', 'My title', 'My body', FALSE)"
            ),
            {"e": _EVENT},
        )
        await db.commit()

    await migration.seed(sessions)

    assert [tuple(r) for r in await _rows(sessions)] == [("Mine", "My title", "My body", False)]


def test_both_locale_files_carry_the_template():
    for lang in ("en", "uk"):
        data = json.loads((_DATA / f"notification_templates_{lang}.json").read_text(encoding="utf-8"))
        assert {"name", "title_template", "body_template"} <= set(data[_EVENT]), lang
