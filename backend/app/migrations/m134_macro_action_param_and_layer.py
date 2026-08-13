"""Give macros an action parameter and a target layer.

Two nullable columns on ``macros``:

    * ``mqtt_action_param TEXT NULL`` — the single argument an
      ``mqtt_action`` takes. A string holds both a choice id ("on", "3")
      and a future number without a second column; the catalog in
      ``core/mqtt_macro_actions.py`` owns what it means.
    * ``trigger_layer INTEGER NULL`` — the layer the ``layer_reached``
      event fires on. Null for every other event.

The seed rewrites the two pre-existing chamber-light actions, whose ids used
to carry their value, into the new grammar. Idempotent by construction: the
second run matches no rows, which matters because ``DEBUG=true`` re-runs the
latest migration on every startup.
"""

import logging

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

logger = logging.getLogger(__name__)

version = 134
name = "macro_action_param_and_layer"

_LEGACY_LIGHT_ROWS = (
    ("chamber_light_on", "on"),
    ("chamber_light_off", "off"),
)


async def upgrade(conn):
    await add_column(conn, "macros", "mqtt_action_param VARCHAR(50)")
    await add_column(conn, "macros", "trigger_layer INTEGER")


async def seed(session_factory):
    async with session_factory() as db:
        moved = 0
        for legacy_id, param in _LEGACY_LIGHT_ROWS:
            result = await db.execute(
                text(
                    "UPDATE macros SET mqtt_action = 'chamber_light', mqtt_action_param = :param "
                    "WHERE mqtt_action = :legacy_id"
                ),
                {"param": param, "legacy_id": legacy_id},
            )
            moved += result.rowcount or 0
        if moved:
            await db.commit()
            logger.info("m134: moved %d chamber-light macro(s) to the parameterized action", moved)
