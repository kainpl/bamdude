"""Macro types beyond gcode: add ``action_type`` + ``mqtt_action`` + ``delay_seconds``.

Until now ``macros`` only modelled gcode snippets injected on swap-mode
events. We're extending the model so a macro can also invoke an MQTT-level
command (e.g. ``chamber_light_off`` on ``print_started``) — things the
printer doesn't expose through gcode.

All three new columns default to values compatible with the pre-existing
gcode macros (``action_type='gcode'``, ``mqtt_action=NULL``, ``delay=0``),
so the rows the user already has keep working unchanged after upgrade.
"""

from backend.app.migrations.helpers import add_column

version = 17
name = "macro_action_type"


async def upgrade(conn):
    await add_column(conn, "macros", "action_type VARCHAR(20) NOT NULL DEFAULT 'gcode'")
    await add_column(conn, "macros", "mqtt_action VARCHAR(50)")
    await add_column(conn, "macros", "delay_seconds INTEGER NOT NULL DEFAULT 0")


async def seed(session_factory):  # pragma: no cover — no-op, columns are self-defaulting
    async with session_factory() as db:
        _ = db  # noqa: ARG001
