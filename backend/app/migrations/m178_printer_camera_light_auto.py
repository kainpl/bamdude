"""Per-printer chamber light for the camera.

``printers.camera_light_auto`` - ``inherit`` (the farm's ``camera_light_auto``
setting decides), ``on`` or ``off`` for this printer alone. Read by
services/camera_light on every use of the camera; the farm toggle and the Obico
sub-toggle live in ``settings`` like every other farm toggle and need no DDL.
Not per model on purpose: the model only says whether there is a light, and
whether a machine may glow towards the window is a question about the machine.
Vault: 60-specs/camera-light-lease-spec.
"""

from backend.app.migrations.helpers import add_column

version = 178
name = "printer_camera_light_auto"


async def upgrade(conn):
    await add_column(conn, "printers", "camera_light_auto VARCHAR(8) NOT NULL DEFAULT 'inherit'")
