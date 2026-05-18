"""Add ``tower_result`` to ``filament_calibration``.

PA / Flow calibrations capture a numeric result (``pa_k_value``,
``flow_ratio``) and — for PA — push it to the printer's K-profile. The
**tower** modes (VFA, Vol Speed; later Temp, Retraction) are
print-and-eyeball tests: the operator prints the tower, reads a failure
height off it, and the finish-page calculator back-computes a value the
operator types into their slicer. That value has no printer runtime knob,
so it can't be auto-applied — but it is worth keeping as a farm record
("this filament, on this printer + nozzle, calibrated to X").

``tower_result`` holds that value. The unit is implied by ``cali_mode``
(VFA → mm/s, Vol Speed → mm³/s, Temp → °C, Retraction → mm). PA / Flow
rows leave it NULL; tower rows leave ``pa_k_value`` / ``flow_ratio`` NULL.

Idempotent — ``add_column`` is a no-op when the column already exists.
"""

from backend.app.migrations.helpers import add_column

version = 69
name = "filament_calibration_tower_result"


async def upgrade(conn):
    await add_column(conn, "filament_calibration", "tower_result FLOAT")
