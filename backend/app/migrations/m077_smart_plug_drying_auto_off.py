"""Add per-plug AMS-drying auto-off columns to ``smart_plugs``.

Two new fields on the ``SmartPlug`` row, independent of the existing
``auto_off`` (which fires only after a print finishes):

* ``auto_off_after_drying`` (Boolean, default False) — gate flag.
* ``off_delay_after_drying_minutes`` (Integer, default 10) — cooldown
  delay after the AMS reports a drying cycle complete (dry_time > 0 →
  0 falling edge). Defaults to 10 min instead of the print-finish
  default of 5 because the AMS chamber is hot post-cycle and users may
  want longer cooldown.

The trigger lives in ``BambuMQTTClient`` and observes firmware state,
not scheduler intent — so it catches queue, ambient and manual drying
cycles identically. BamDude doesn't model per-AMS plug routing, so the
trigger is plug-vs-printer-level: any AMS on a plug's linked printer
finishing a dry cycle fires the auto-off on every plug whose toggle is
set.

Upstream Bambuddy #1349 / commit ``6f2cec5e``.
"""

from backend.app.migrations.helpers import add_column

version = 77
name = "smart_plug_drying_auto_off"


async def upgrade(conn):
    await add_column(conn, "smart_plugs", "auto_off_after_drying BOOLEAN DEFAULT 0 NOT NULL")
    await add_column(conn, "smart_plugs", "off_delay_after_drying_minutes INTEGER DEFAULT 10 NOT NULL")
