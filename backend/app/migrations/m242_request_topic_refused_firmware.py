"""A printer remembers that it refuses the request-topic subscription, per firmware.

Some printers drop the MQTT connection when BamDude subscribes to their request topic (it is how
a print started from a slicer is seen with its tray mapping). The verdict was kept in process
memory only, so every start probed every such printer again: two dropped connections each.
``printers.request_topic_refused_firmware`` holds the firmware version the refusal was met on;
NULL means "not known to refuse" and is what every existing row gets, so the first start after
this migration probes once more and then remembers. A printer that reports another firmware is
probed again -- the value is never trusted across a firmware change.
"""

from backend.app.migrations.helpers import add_column

version = 242
name = "request_topic_refused_firmware"


async def upgrade(conn):
    await add_column(conn, "printers", "request_topic_refused_firmware VARCHAR(50)")
