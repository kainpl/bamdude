"""Optional polygon mask. NULL retains the existing rectangular ROI unchanged.

Vertices are normalized source-image coordinates. Reference images remain full
original frames, so changing the contour never rewrites calibration photos.
"""

from backend.app.migrations.helpers import add_column, json_column_type

version = 196
name = "plate_detection_polygon"


async def upgrade(conn):
    await add_column(conn, "printers", f"plate_detection_polygon {json_column_type()}")
