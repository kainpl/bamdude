"""The locale sync must CREATE a shipped template, not only refresh one.

It only ever issued an UPDATE, so a template added to the JSON after the DB was
first seeded never materialised: ``rowcount`` came back 0 and the loop moved on.
Four shipped templates were dead this way — ``filament_runout``,
``filament_runout_backup``, ``stock_break_alert``, ``stock_reorder_alert`` — and
a farm lost every runout notification without a word, because the count the
startup logs ("39 notification templates") is rows UPDATED, in which a missing
template is indistinguishable from an unchanged one.

⚠️ ``event_type`` is UNIQUE, and a template the user has edited is marked
``is_default=False`` and lives in that same single row. So "insert when the
update touched nothing" must still check the row does not exist at all —
otherwise the sync either violates the constraint or overwrites the edit it is
supposed to leave alone.
"""

import json

import pytest
from sqlalchemy import select

from backend.app.models.notification_template import NotificationTemplate
from backend.app.services import locale_updater


@pytest.fixture
def shipped(tmp_path, monkeypatch):
    """A locale directory holding exactly the templates a test names."""

    def _write(templates: dict):
        (tmp_path / "notification_templates_en.json").write_text(json.dumps(templates), encoding="utf-8")
        monkeypatch.setattr(locale_updater, "DATA_DIR", tmp_path)

    return _write


def _tpl(name):
    return {"name": name, "title_template": f"{name} title", "body_template": f"{name} body"}


@pytest.mark.asyncio
async def test_a_shipped_template_with_no_row_is_created(db_session, shipped):
    shipped({"filament_runout": _tpl("Filament runout")})

    touched = await locale_updater._update_notification_templates(db_session, "en")
    await db_session.commit()

    row = (
        await db_session.execute(
            select(NotificationTemplate).where(NotificationTemplate.event_type == "filament_runout")
        )
    ).scalar_one()
    assert touched == 1
    assert row.name == "Filament runout"
    assert row.is_default is True


@pytest.mark.asyncio
async def test_an_existing_default_is_refreshed_not_duplicated(db_session, shipped):
    db_session.add(
        NotificationTemplate(
            event_type="print_start",
            name="stale",
            title_template="stale",
            body_template="stale",
            is_default=True,
        )
    )
    await db_session.commit()
    shipped({"print_start": _tpl("Print Started")})

    await locale_updater._update_notification_templates(db_session, "en")
    await db_session.commit()

    rows = (
        (await db_session.execute(select(NotificationTemplate).where(NotificationTemplate.event_type == "print_start")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].name == "Print Started"


@pytest.mark.asyncio
async def test_a_template_the_user_edited_is_left_alone(db_session, shipped):
    # ⚠️ Not merely "not updated" — not INSERTED over either. One row per
    # event_type, so a careless insert would either blow up on the unique
    # constraint or replace the operator's own wording.
    db_session.add(
        NotificationTemplate(
            event_type="print_complete",
            name="mine",
            title_template="mine",
            body_template="mine",
            is_default=False,
        )
    )
    await db_session.commit()
    shipped({"print_complete": _tpl("Print Complete")})

    await locale_updater._update_notification_templates(db_session, "en")
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(NotificationTemplate).where(NotificationTemplate.event_type == "print_complete")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].name == "mine"


@pytest.mark.asyncio
async def test_every_shipped_template_reaches_the_database(db_session):
    # The drift itself: whatever ships must be creatable in one sync.
    await locale_updater._update_notification_templates(db_session, "en")
    await db_session.commit()

    with open(locale_updater.DATA_DIR / "notification_templates_en.json", encoding="utf-8") as f:
        shipped_keys = set(json.load(f))
    in_db = {r for (r,) in (await db_session.execute(select(NotificationTemplate.event_type))).all()}
    assert shipped_keys - in_db == set()
