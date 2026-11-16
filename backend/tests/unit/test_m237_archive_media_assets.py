"""m237 creates ``archive_media_assets``, the model declares the same table, the seed describes what exists (G10 C, M14)."""

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.migrations import m237_archive_media_assets as m237

COLUMNS = {
    "id": ("INTEGER", False),
    "asset_uuid": ("VARCHAR(36)", True),
    "archive_id": ("INTEGER", True),
    "archive_identity": ("VARCHAR(36)", True),
    "role": ("VARCHAR(16)", True),
    "revision": ("INTEGER", True),
    "relative_path": ("VARCHAR(500)", True),
    "size_bytes": ("BIGINT", False),
    "content_sha256": ("VARCHAR(64)", False),
    "origin": ("VARCHAR(16)", True),
    "remote_printer_identity": ("VARCHAR(36)", False),
    "remote_storage": ("VARCHAR(16)", False),
    "remote_path": ("VARCHAR(500)", False),
    "remote_size": ("BIGINT", False),
    "remote_mtime_raw": ("VARCHAR(64)", False),
    "created_at": ("DATETIME", True),
}

INSERT = (
    "INSERT INTO archive_media_assets (asset_uuid, archive_id, archive_identity, role, revision, relative_path, "
    "content_sha256, origin, remote_printer_identity, remote_storage, remote_path, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01 00:00:00')"
)


async def _engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm237.db'}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE print_archives (id INTEGER PRIMARY KEY, recovery_identity VARCHAR(36), "
            "timelapse_path VARCHAR(500))"
        )
        await m237.upgrade(conn)
    return engine


@pytest.mark.asyncio
async def test_m237_creates_the_table_and_its_indexes(tmp_path):
    engine = await _engine(tmp_path)
    async with engine.begin() as conn:
        columns = {
            row[1]: (row[2], bool(row[3]))
            for row in (await conn.exec_driver_sql("PRAGMA table_info(archive_media_assets)")).fetchall()
        }
        assert columns == COLUMNS
        indexes = {
            row[1]: bool(row[2])
            for row in (await conn.exec_driver_sql("PRAGMA index_list(archive_media_assets)")).fetchall()
        }
        assert indexes["uq_archive_media_assets_primary"] is True
        assert indexes["uq_archive_media_assets_remote_claim"] is True
        assert indexes["ix_archive_media_assets_archive"] is False
        # idempotent (DEBUG=true re-runs the newest migration)
        await m237.upgrade(conn)
    await engine.dispose()


@pytest.mark.asyncio
async def test_an_archive_has_one_primary_video_and_any_number_of_others(tmp_path):
    engine = await _engine(tmp_path)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(INSERT, ("u1", 1, "A", "primary", 1, "a.mp4", None, "legacy", None, None, None))
        await conn.exec_driver_sql(INSERT, ("u2", 1, "A", "variant", 2, "b.mp4", None, "editor", None, None, None))
        await conn.exec_driver_sql(INSERT, ("u3", 1, "A", "retired", 3, "c.mp4", None, "editor", None, None, None))
        await conn.exec_driver_sql(INSERT, ("u4", 2, "B", "primary", 1, "d.mp4", None, "legacy", None, None, None))
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.exec_driver_sql(INSERT, ("u5", 1, "A", "primary", 4, "e.mp4", None, "upload", None, None, None))
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_recording_on_a_printer_is_claimed_by_one_archive(tmp_path):
    engine = await _engine(tmp_path)
    claim = ("P-1", "external", "/timelapse/v.mp4")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(INSERT, ("u1", 1, "A", "primary", 1, "a.mp4", "hash1", "printer", *claim))
        # Another medium, another printer, other bytes at the same path: different recordings.
        await conn.exec_driver_sql(
            INSERT, ("u2", 2, "B", "primary", 1, "b.mp4", "hash1", "printer", "P-1", "internal", "/timelapse/v.mp4")
        )
        await conn.exec_driver_sql(
            INSERT, ("u3", 3, "C", "primary", 1, "c.mp4", "hash1", "printer", "P-2", "external", "/timelapse/v.mp4")
        )
        await conn.exec_driver_sql(INSERT, ("u4", 4, "D", "primary", 1, "d.mp4", "hash2", "printer", *claim))
        # No remote source at all: legacy and uploaded rows never collide with each other.
        await conn.exec_driver_sql(INSERT, ("u5", 5, "E", "primary", 1, "e.mp4", None, "legacy", None, None, None))
        await conn.exec_driver_sql(INSERT, ("u6", 6, "F", "primary", 1, "f.mp4", None, "legacy", None, None, None))
        await conn.exec_driver_sql(INSERT, ("u7", 7, "G", "primary", 1, "g.mp4", "hash1", "upload", None, None, None))
        await conn.exec_driver_sql(INSERT, ("u8", 8, "H", "primary", 1, "h.mp4", "hash1", "upload", None, None, None))
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.exec_driver_sql(INSERT, ("u9", 9, "I", "primary", 1, "i.mp4", "hash1", "printer", *claim))
    await engine.dispose()


@pytest.mark.asyncio
async def test_an_asset_uuid_is_unique(tmp_path):
    engine = await _engine(tmp_path)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(INSERT, ("same", 1, "A", "primary", 1, "a.mp4", None, "legacy", None, None, None))
    async with engine.connect() as conn:
        with pytest.raises(IntegrityError):
            await conn.exec_driver_sql(
                INSERT, ("same", 2, "B", "primary", 1, "b.mp4", None, "legacy", None, None, None)
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_deleted_rows_id_is_not_given_to_the_next_one(tmp_path):
    engine = await _engine(tmp_path)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(INSERT, ("u1", 1, "A", "primary", 1, "a.mp4", None, "legacy", None, None, None))
        await conn.exec_driver_sql("DELETE FROM archive_media_assets")
        await conn.exec_driver_sql(INSERT, ("u2", 1, "A", "primary", 1, "a.mp4", None, "legacy", None, None, None))
        assert (await conn.exec_driver_sql("SELECT id FROM archive_media_assets")).scalar() == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_seed_describes_every_existing_video_as_legacy_and_claims_nothing(tmp_path):
    engine = await _engine(tmp_path)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO print_archives (id, recovery_identity, timelapse_path) VALUES "
            "(1, 'ID-1', 'archive/1/x/timelapse/ID-1/a.mp4'), "
            "(2, 'ID-2', NULL), "
            "(3, 'ID-3', ''), "
            "(4, NULL, 'archive/4/y/timelapse.mp4'), "
            "(5, 'ID-5', 'archive\\5\\z\\timelapse\\ID-5\\b.mp4')"
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await m237.seed(factory)
    await m237.seed(factory)  # idempotent: DEBUG re-runs the newest migration

    async with engine.connect() as conn:
        rows = (
            await conn.exec_driver_sql(
                "SELECT archive_id, archive_identity, role, revision, relative_path, origin, size_bytes, "
                "content_sha256, remote_printer_identity, remote_storage, remote_path, length(asset_uuid) "
                "FROM archive_media_assets ORDER BY archive_id"
            )
        ).fetchall()
    assert [tuple(r) for r in rows] == [
        (1, "ID-1", "primary", 1, "archive/1/x/timelapse/ID-1/a.mp4", "legacy", None, None, None, None, None, 36),
        # The path is copied as stored — backslashes and all — so it equals the pointer it describes.
        (5, "ID-5", "primary", 1, "archive\\5\\z\\timelapse\\ID-5\\b.mp4", "legacy", None, None, None, None, None, 36),
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_seed_leaves_an_archive_that_already_has_a_primary_row_alone(tmp_path):
    engine = await _engine(tmp_path)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "INSERT INTO print_archives (id, recovery_identity, timelapse_path) VALUES (1, 'ID-1', 'new.mp4')"
        )
        await conn.exec_driver_sql(
            INSERT, ("kept", 1, "ID-1", "primary", 7, "new.mp4", "hash", "printer", "P-1", "external", "/t/v.mp4")
        )
    await m237.seed(async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False))
    async with engine.connect() as conn:
        rows = (await conn.exec_driver_sql("SELECT asset_uuid, revision, origin FROM archive_media_assets")).fetchall()
    assert [tuple(r) for r in rows] == [("kept", 7, "printer")]
    await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_is_asked_for_types_it_has(monkeypatch):
    statements = []

    async def no_table(conn, name):
        return False

    async def record(sql):
        statements.append(" ".join(sql.split()))

    monkeypatch.setattr(m237, "table_exists", no_table)
    conn = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), exec_driver_sql=record)
    await m237.upgrade(conn)

    create = next(s for s in statements if s.startswith("CREATE TABLE"))
    assert "id SERIAL PRIMARY KEY" in create
    assert "created_at TIMESTAMP NOT NULL" in create
    assert "DATETIME" not in create and "AUTOINCREMENT" not in create
    assert "size_bytes BIGINT" in create and "remote_size BIGINT" in create
    assert "REFERENCES print_archives(id) ON DELETE CASCADE" in create
    assert "CONSTRAINT uq_archive_media_assets_asset_uuid UNIQUE (asset_uuid)" in create
    assert any(
        s.startswith("CREATE UNIQUE INDEX IF NOT EXISTS uq_archive_media_assets_primary")
        and "WHERE role = 'primary'" in s
        for s in statements
    )
    assert any(
        s.startswith("CREATE UNIQUE INDEX IF NOT EXISTS uq_archive_media_assets_remote_claim") for s in statements
    )


def test_the_model_declares_the_same_table():
    from backend.app.models.archive_media_asset import ArchiveMediaAsset

    table = ArchiveMediaAsset.__table__
    assert table.name == "archive_media_assets"
    assert table.dialect_options["sqlite"]["autoincrement"] is True
    declared = {c.name: (str(c.type), not c.nullable) for c in table.c}
    expected = {name: (kind, required) for name, (kind, required) in COLUMNS.items()}
    expected["id"] = ("INTEGER", True)  # a primary key is NOT NULL in the model; SQLite's pragma reports 0 for it
    assert declared == expected

    indexes = {index.name: index for index in table.indexes}
    assert set(indexes) == {
        "uq_archive_media_assets_primary",
        "uq_archive_media_assets_remote_claim",
        "ix_archive_media_assets_archive",
    }
    primary = indexes["uq_archive_media_assets_primary"]
    assert primary.unique and [c.name for c in primary.columns] == ["archive_id"]
    assert str(primary.dialect_options["sqlite"]["where"]) == "role = 'primary'"
    assert str(primary.dialect_options["postgresql"]["where"]) == "role = 'primary'"
    claim = indexes["uq_archive_media_assets_remote_claim"]
    assert claim.unique and [c.name for c in claim.columns] == [
        "remote_printer_identity",
        "remote_storage",
        "remote_path",
        "content_sha256",
    ]
    assert [c.name for c in indexes["ix_archive_media_assets_archive"].columns] == ["archive_id", "role"]
    uniques = {c.name for c in table.constraints if c.__class__.__name__ == "UniqueConstraint"}
    assert uniques == {"uq_archive_media_assets_asset_uuid"}
    fk = next(iter(table.c.archive_id.foreign_keys))
    assert fk.target_fullname == "print_archives.id" and fk.ondelete == "CASCADE"


@pytest.mark.asyncio
async def test_create_all_builds_what_the_migration_builds(tmp_path):
    """A fresh install (``create_all``) and an upgraded one (m237) end with the same table."""
    from backend.app.models.archive_media_asset import ArchiveMediaAsset

    migrated = await _engine(tmp_path)
    fresh = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}")
    async with fresh.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE print_archives (id INTEGER PRIMARY KEY)")
        await conn.run_sync(lambda sync: ArchiveMediaAsset.__table__.create(sync))

    async def shape(engine):
        async with engine.connect() as conn:
            columns = [
                # A primary key is NOT NULL either way; the pragma says so only when the DDL spells it.
                (r[1], r[2], bool(r[3]) or bool(r[5]))
                for r in (await conn.exec_driver_sql("PRAGMA table_info(archive_media_assets)")).fetchall()
            ]
            indexes = {}
            for row in (await conn.exec_driver_sql("PRAGMA index_list(archive_media_assets)")).fetchall():
                if str(row[1]).startswith("sqlite_autoindex"):
                    continue
                cols = [r[2] for r in (await conn.exec_driver_sql(f"PRAGMA index_info({row[1]})")).fetchall()]
                indexes[row[1]] = (bool(row[2]), cols, bool(row[4]))
            sql = (
                await conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name = 'archive_media_assets'")
            ).scalar()
            return columns, indexes, "AUTOINCREMENT" in sql

    assert await shape(fresh) == await shape(migrated)
    await migrated.dispose()
    await fresh.dispose()
