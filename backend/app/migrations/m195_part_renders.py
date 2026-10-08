"""Part thumbnails: plate_renders and plate_render_objects (spec §8.1, §8.2; plan E3, task 19).

plate_renders is a plate's queue row and the header of its last published result in one: the result
exists when result_dir is set, and status says where processing stands. Rows are keyed by the file's
CONTENT (sha256), the plate and the renderer version. Identical files share renders, and a re-slice is a
new row: there is no invalidation of its own. phase / reason carry the retry contract across a restart.
orphaned_at starts the GC grace when the hash loses its last linked file (plan E3, R7).

AUTOINCREMENT on SQLite, because render_id goes into logs, media URLs and the publication's CAS and
must never be reissued. identify_id is BIGINT, because a slicer id is u32. SQLite runs no FK actions:
the writer deletes instance rows in code.
"""

from backend.app.migrations.helpers import table_exists

version = 195
name = "part_renders"


async def upgrade(conn):
    sqlite = conn.dialect.name == "sqlite"
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    plain_pk = "INTEGER PRIMARY KEY" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"
    json_type = "TEXT" if sqlite else "JSON"
    big = "INTEGER" if sqlite else "BIGINT"

    if not await table_exists(conn, "plate_renders"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE plate_renders (
                id {pk},
                file_sha256 VARCHAR(64) NOT NULL,
                plate_index INTEGER NOT NULL,
                renderer_version INTEGER NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'pending',
                phase VARCHAR(16) NOT NULL DEFAULT 'render',
                reason VARCHAR(32),
                result_dir VARCHAR(36),
                manifest_sha256 VARCHAR(64),
                runtime_version VARCHAR(32),
                priority INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at {ts} NOT NULL,
                last_error VARCHAR(64),
                requested_at {ts} NOT NULL,
                finished_at {ts},
                elapsed_ms INTEGER,
                orphaned_at {ts},
                CONSTRAINT uq_plate_renders_key UNIQUE (file_sha256, plate_index, renderer_version),
                CONSTRAINT ck_plate_renders_sha256 CHECK (length(file_sha256) = 64),
                CONSTRAINT ck_plate_renders_status CHECK (status IN ('pending', 'ready', 'failed', 'unavailable')),
                CONSTRAINT ck_plate_renders_phase CHECK (phase IN ('render', 'fallback'))
            )
            """
        )
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_plate_renders_file_sha256 ON plate_renders (file_sha256)")
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_plate_renders_queue ON plate_renders (status, renderer_version, next_attempt_at)"
    )

    if not await table_exists(conn, "plate_render_objects"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE plate_render_objects (
                id {plain_pk},
                render_id INTEGER NOT NULL REFERENCES plate_renders(id) ON DELETE CASCADE,
                identify_id {big} NOT NULL,
                method VARCHAR(16) NOT NULL,
                reason VARCHAR(32),
                width INTEGER,
                height INTEGER,
                tools {json_type} NOT NULL,
                CONSTRAINT uq_plate_render_objects_instance UNIQUE (render_id, identify_id),
                CONSTRAINT ck_plate_render_objects_method
                    CHECK (method IN ('toolpath', 'model', 'top_mask', 'missing', 'skipped'))
            )
            """
        )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_plate_render_objects_render_id ON plate_render_objects (render_id)"
    )
