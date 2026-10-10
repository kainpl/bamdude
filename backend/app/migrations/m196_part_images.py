"""Part thumbnails: a part's picture choice on product_parts (spec §8.6; plan E4, task 26).

image_source is auto / instance / photo; only the chosen source's data is kept: the pin
(image_file_id, image_plate_index, image_identify_id) for instance, the stored file name
(image_photo) for photo. services/part_images.py is the only writer. No FK on image_file_id --
a pin whose file went explains itself instead of silently becoming auto. No CHECK either: one
writer, and an inline CHECK would drift from a named one in conform_imported_schema (plan E4,
D9, D19). identify_id is BIGINT, because a slicer id is u32.
"""

from backend.app.migrations.helpers import add_column

version = 196
name = "part_images"


async def upgrade(conn):
    big = "INTEGER" if conn.dialect.name == "sqlite" else "BIGINT"
    await add_column(conn, "product_parts", "image_source VARCHAR(16) NOT NULL DEFAULT 'auto'")
    await add_column(conn, "product_parts", "image_file_id INTEGER")
    await add_column(conn, "product_parts", "image_plate_index INTEGER")
    await add_column(conn, "product_parts", f"image_identify_id {big}")
    await add_column(conn, "product_parts", "image_photo VARCHAR(64)")
