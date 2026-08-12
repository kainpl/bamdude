"""Add ``smart_plugs.zigbee_ieee`` for plugs driven over BamDude's own radio.

Addressed by IEEE rather than the short NWK address, which the network
reassigns on rejoin: a stored NWK would quietly refer to a different device — or
to none — after a power cut, and the failure would look like a broken plug
rather than a wrong address.

No seed. Existing plugs are Tasmota / Home Assistant / MQTT / REST and leave the
column NULL; nothing about them changes.

No new permission either — ``SMART_PLUGS_*`` already covers create / read /
update / delete / control for every plug type, so there is nothing to grant to
Administrators here. That matters because our migrations are frozen and
Administrators are not self-healed at startup, so a permission introduced
without a seed would leave existing installs unable to use it.
"""

from __future__ import annotations

from backend.app.migrations.helpers import add_column

version = 115
name = "zigbee_plug"


async def upgrade(conn):
    await add_column(conn, "smart_plugs", "zigbee_ieee VARCHAR(23)")
