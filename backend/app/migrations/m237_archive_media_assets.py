"""A registry of the videos that belong to print archives (G10 C, upstream b342124f0 / c01849eb8 lineage).

``print_archives.timelapse_path`` names the video an archive shows and nothing
else. ``archive_media_assets`` adds what the pointer cannot hold: the video's
own identity and revision, where it came from, and — for a recording read off a
printer — which recording it was, so that one recording is never attached to
two prints. ``timelapse_path`` stays as the projection of the ``primary`` row.

Two unique indexes carry the rules:

* ``uq_archive_media_assets_primary`` — one ``primary`` row per archive (a
  partial index; both engines support it);
* ``uq_archive_media_assets_remote_claim`` — a recording on a printer (the
  printer's ``connector_identity``, the medium, the path, the SHA-256 of the
  downloaded bytes) belongs to one archive. NULLs are distinct on both engines,
  so rows with no remote source never collide.

The seed writes one ``primary`` / ``legacy`` row for every archive that already
has a ``timelapse_path``. ⚠️ It reads no file, guesses no source and makes no
claim: nothing is known about where a video attached before this table came
from, and a wrong guess would refuse a later, correct attach. An archive whose
``recovery_identity`` is missing gets no row — its video keeps working through
``timelapse_path`` and is described as legacy when it is next touched.

``AUTOINCREMENT`` on SQLite keeps a deleted row's id from passing to the next
one. The foreign key is declared for PostgreSQL; SQLite runs no FK actions, so
the rows are removed in code wherever an archive is hard-deleted
(``services/archive_media.forget_archives``).

Depends on ``print_archives.id``, ``recovery_identity`` (m204) and ``timelapse_path``.
"""

from datetime import datetime, timezone
from uuid import uuid4

from backend.app.migrations.helpers import table_exists

version = 237
name = "archive_media_assets"


async def upgrade(conn):
    sqlite = conn.dialect.name == "sqlite"
    if not await table_exists(conn, "archive_media_assets"):
        pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
        ts = "DATETIME" if sqlite else "TIMESTAMP"
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE archive_media_assets (
                id {pk},
                asset_uuid VARCHAR(36) NOT NULL,
                archive_id INTEGER NOT NULL REFERENCES print_archives(id) ON DELETE CASCADE,
                archive_identity VARCHAR(36) NOT NULL,
                role VARCHAR(16) NOT NULL,
                revision INTEGER NOT NULL,
                relative_path VARCHAR(500) NOT NULL,
                size_bytes BIGINT,
                content_sha256 VARCHAR(64),
                origin VARCHAR(16) NOT NULL,
                remote_printer_identity VARCHAR(36),
                remote_storage VARCHAR(16),
                remote_path VARCHAR(500),
                remote_size BIGINT,
                remote_mtime_raw VARCHAR(64),
                created_at {ts} NOT NULL,
                CONSTRAINT uq_archive_media_assets_asset_uuid UNIQUE (asset_uuid)
            )
            """
        )
    await conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_archive_media_assets_primary "
        "ON archive_media_assets (archive_id) WHERE role = 'primary'"
    )
    await conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_archive_media_assets_remote_claim "
        "ON archive_media_assets (remote_printer_identity, remote_storage, remote_path, content_sha256)"
    )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_archive_media_assets_archive ON archive_media_assets (archive_id, role)"
    )


async def seed(session_factory):
    from sqlalchemy import BigInteger, DateTime, Integer, String, column, insert, select, table

    archives = table(
        "print_archives",
        column("id", Integer),
        column("recovery_identity", String),
        column("timelapse_path", String),
    )
    assets = table(
        "archive_media_assets",
        column("asset_uuid", String),
        column("archive_id", Integer),
        column("archive_identity", String),
        column("role", String),
        column("revision", Integer),
        column("relative_path", String),
        column("size_bytes", BigInteger),
        column("content_sha256", String),
        column("origin", String),
        column("created_at", DateTime),
    )

    async with session_factory() as db:
        have = set((await db.execute(select(assets.c.archive_id).where(assets.c.role == "primary"))).scalars().all())
        rows = (
            await db.execute(
                select(archives.c.id, archives.c.recovery_identity, archives.c.timelapse_path)
                .where(archives.c.timelapse_path.is_not(None))
                .order_by(archives.c.id)
            )
        ).all()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        wrote = False
        for archive_id, identity, path in rows:
            # Idempotent (DEBUG re-runs the newest migration), and nothing for a
            # blank path or an archive with no identity to bind the row to.
            if archive_id in have or not (path or "").strip() or not (identity or "").strip():
                continue
            await db.execute(
                insert(assets).values(
                    asset_uuid=str(uuid4()),
                    archive_id=archive_id,
                    archive_identity=identity,
                    role="primary",
                    revision=1,
                    relative_path=path,
                    size_bytes=None,
                    content_sha256=None,
                    origin="legacy",
                    created_at=now,
                )
            )
            wrote = True
        if wrote:
            await db.commit()
