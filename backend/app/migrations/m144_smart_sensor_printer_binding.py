"""Let a sensor be bound to a printer instead of to a place.

A sensor has always pointed at ``printer_locations`` — a room or a shelf. That
is the right answer for a thermometer measuring the workshop, and the wrong one
for an enclosure probe or a door contact, which belong to one machine. Upstream
solves this with a separate Home-Assistant-only table bound to the printer; we
already own our sensors over Zigbee directly, so the binding is a property of
the sensor we have rather than a reason for a second kind.

⚠️ **The two bindings are exclusive**, and the routes enforce it. They answer
the same question — where this reading belongs — and a printer already has a
location, so holding both would let a sensor claim a place its printer is not
in, and appear in two lists at once.

⚠️ ``ON DELETE SET NULL``, deliberately unlike the location's ``RESTRICT``. A
sensor is physical hardware that outlives the printer it was taped to: deleting
the printer must not delete an adopted device, and refusing to delete a printer
because a thermometer points at it would be worse. The sensor becomes unbound.

Existing rows are untouched — every one of them keeps whatever location it had,
and ``printer_id`` starts NULL, so nothing moves until somebody re-points it.
"""

import logging

from backend.app.migrations.helpers import add_column

logger = logging.getLogger(__name__)

version = 144
name = "smart_sensor_printer_binding"


async def upgrade(conn):
    # ⚠️ No FK clause here on purpose. SQLite cannot add a constraint to an
    # existing table, and ``add_column`` is shared by both backends — the
    # relationship is declared on the model, which is what ``create_all`` uses
    # for a fresh install. An existing SQLite database therefore carries the
    # column without the database-level cascade; the delete path nulls it in
    # application code either way, and rewriting the table to gain the clause
    # would risk the data for a guarantee we do not rely on.
    await add_column(conn, "smart_sensors", "printer_id INTEGER")

    from sqlalchemy import text

    # Indexed because the printer card asks "which sensors are this printer's"
    # on every render, once per card.
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_smart_sensors_printer_id ON smart_sensors (printer_id)"))
