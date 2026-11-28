"""The ``ams_drying_not_heating`` notification template: auto-drying says a unit did not heat (C08).

Upstream Bambuddy #3478 (``5be58d7f1``). A printer can take a drying command and leave the heater off, so the
timer stands still. Two such cycles in a row suspend auto-drying on the unit; the notification used to be the
unproductive-humidity one («… this threshold cannot be reached here»), which sends the operator to the threshold
while the unit never heated. The new template rides the same provider switch (``on_ams_drying_suspended``) and the
same delivery event, so there is no column, no catalog row and no Telegram change here.

The seed adds the row for databases that predate it, in the system language, by named columns, never twice; a fresh
install gets it from m001's seed of ``DEFAULT_TEMPLATES``. An existing row is the operator's and stays as it is.
No DDL.
"""

import json
import logging
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger(__name__)

version = 275
name = "auto_drying_not_heating_template"

_EVENT = "ams_drying_not_heating"


async def upgrade(conn):
    """No DDL."""


async def seed(session_factory):
    from backend.app.models.notification_template import DEFAULT_TEMPLATES

    async with session_factory() as db:
        present = (
            await db.execute(text("SELECT 1 FROM notification_templates WHERE event_type = :e"), {"e": _EVENT})
        ).first()
        if present is not None:
            return
        lang = (await db.execute(text("SELECT value FROM settings WHERE key = 'language'"))).scalar()
        data = next(t for t in DEFAULT_TEMPLATES if t["event_type"] == _EVENT)
        if lang == "uk":
            path = Path(__file__).parent.parent / "data" / "notification_templates_uk.json"
            data = json.loads(path.read_text(encoding="utf-8")).get(_EVENT, data)
        await db.execute(
            text(
                "INSERT INTO notification_templates (event_type, name, title_template, body_template, is_default) "
                "VALUES (:e, :n, :t, :b, :d)"
            ),
            {"e": _EVENT, "n": data["name"], "t": data["title_template"], "b": data["body_template"], "d": True},
        )
        await db.commit()
        logger.info("m275: seeded the %s notification template (%s)", _EVENT, lang or "en")
