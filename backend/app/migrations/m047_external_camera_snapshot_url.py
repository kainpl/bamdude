"""Add ``printers.external_camera_snapshot_url`` (upstream Bambuddy #1177 follow-up).

go2rtc and several IP cameras still emit a warm-up / often-black frame on a
fresh MJPEG connection — even after the v0.2.4b2 warm-up-frame skip
(see ``services/external_camera.py::_capture_mjpeg_frame`` and audit-MD A.11).
The reporter's bisect named the cleanest way out: go2rtc exposes
``/api/frame.jpeg`` as a dedicated single-frame endpoint that never returns
the encoder's stale keyframe, while ``/api/stream.mjpeg`` always does on a
fresh connection.

This migration adds an optional column where operators can pin that override
URL on a per-printer basis. When set, every single-frame capture path
(``/printers/{id}/camera/snapshot``, ``[SNAPSHOT]`` notification thumbnails,
``[PHOTO-BG]`` finish photo, layer timelapse, Obico ML, plate-detect /
calibrate-plate) routes through ``services/external_camera.py::_capture_snapshot``
on the override URL via plain HTTP GET — bypassing the warm-up dance
entirely. Live-view streaming is **not** affected; the in-app viewer keeps
using the configured stream URL because a 1 fps poll-the-snapshot-endpoint
live view would regress everyone who doesn't have the warm-up problem.

Idempotent: ``add_column`` is a no-op when the column already exists, so
re-running on an upgraded DB is safe.
"""

from backend.app.migrations.helpers import add_column

version = 47
name = "external_camera_snapshot_url"


async def upgrade(conn):
    await add_column(conn, "printers", "external_camera_snapshot_url VARCHAR(500)")
