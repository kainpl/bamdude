"""Persist which printers are recording their MQTT traffic.

Recording exists so a capture outlives the window that started it — the whole
complaint was that watching a printer meant leaving a terminal open. A backend
restart is only a longer version of closing that window, so the intent has to
live in the database rather than in process memory, and the lifespan restarts
whatever it finds here.

Two columns rather than one: ``mqtt_recording_started_at`` is what turns "this
is recording" into "this has been recording since Tuesday", which is the
question asked about a file nothing caps the size of.
"""

from backend.app.migrations.helpers import add_column

version = 139
name = "mqtt_recording_state"


async def upgrade(conn):
    await add_column(conn, "printers", "mqtt_recording BOOLEAN NOT NULL DEFAULT 0")
    await add_column(conn, "printers", "mqtt_recording_started_at TIMESTAMP")
