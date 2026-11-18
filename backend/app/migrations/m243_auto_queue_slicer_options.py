"""An AutoQueue job remembers what the slicer itself chose for it.

A job sent through a Virtual Printer in AutoQueue mode has no printer yet, so its print options are
decided when the distributor hands it to one -- from the creator's saved profile for that printer's
model (m199 added the order's own choices beside it). A Virtual Printer job has no creator, and the
choices its operator made in the slicer (timelapse, bed levelling, flow calibration, first-layer
inspection, nozzle offset calibration) were stamped on the row's own columns, which that decision
does not read: the job printed with the system defaults. ``auto_queue_items.slicer_options`` holds
only what the slicer explicitly sent, under the queue row's option names; the assignment lays it
over the profile. NULL -- every existing row, and every job not sent by a slicer -- changes nothing.
"""

from backend.app.migrations.helpers import add_column, json_column_type

version = 243
name = "auto_queue_slicer_options"


async def upgrade(conn):
    await add_column(conn, "auto_queue_items", f"slicer_options {json_column_type()}")
