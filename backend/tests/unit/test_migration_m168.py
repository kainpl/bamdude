"""m168 adds the four measured hot-path indexes, on either engine, idempotently."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m168_hot_path_indexes as m168
from backend.app.models.archive import PrintArchive
from backend.app.models.spool_k_profile import SpoolKProfile

_EXPECTED = {
    "ix_print_archives_active_created",
    "ix_print_archives_printer_created",
    "ix_print_archives_effective_hash",
    "ix_spool_k_profile_spool_id",
}


def test_the_migration_declares_its_version_and_name():
    assert m168.version == 168
    assert m168.name == "hot_path_indexes"


def test_fresh_installs_get_the_same_indexes_from_the_models():
    """create_all must not leave a fresh install slower than a migrated one —
    every index m168 adds is declared on the model too."""
    declared = {i.name for i in PrintArchive.__table__.indexes} | {i.name for i in SpoolKProfile.__table__.indexes}
    assert declared >= _EXPECTED


async def test_sqlite_gets_every_index_and_the_rerun_is_a_no_op(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'm168.db').as_posix()}")
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE print_archives (id INTEGER PRIMARY KEY, printer_id INTEGER, "
                    "deleted_at TIMESTAMP, created_at TIMESTAMP, content_hash VARCHAR(64), "
                    "source_content_hash VARCHAR(64))"
                )
            )
            await conn.execute(text("CREATE TABLE spool_k_profile (id INTEGER PRIMARY KEY, spool_id INTEGER)"))
            await m168.upgrade(conn)
            # IF NOT EXISTS: running the chain twice must not raise
            await m168.upgrade(conn)
            names = {
                row[0] for row in (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))).all()
            }
        assert names >= _EXPECTED
    finally:
        await engine.dispose()


async def test_the_archive_list_no_longer_sorts_the_whole_table(tmp_path):
    """The point of the index, not merely its existence: the list's
    ``deleted_at IS NULL ORDER BY created_at DESC`` used a temp B-tree to sort
    every matching row on every page — the farm-scale "archive is slow" report.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'plan.db').as_posix()}")
    listing = "SELECT id FROM print_archives WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 50"
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE print_archives (id INTEGER PRIMARY KEY, printer_id INTEGER, "
                    "deleted_at TIMESTAMP, created_at TIMESTAMP, content_hash VARCHAR(64), "
                    "source_content_hash VARCHAR(64))"
                )
            )
            await conn.execute(text("CREATE TABLE spool_k_profile (id INTEGER PRIMARY KEY, spool_id INTEGER)"))
            before = " ".join(str(r) for r in (await conn.execute(text(f"EXPLAIN QUERY PLAN {listing}"))).all())
            assert "TEMP B-TREE" in before.upper(), "the sort was supposed to be the problem"

            await m168.upgrade(conn)
            after = " ".join(str(r) for r in (await conn.execute(text(f"EXPLAIN QUERY PLAN {listing}"))).all())
        assert "TEMP B-TREE" not in after.upper()
        assert "ix_print_archives_active_created" in after
    finally:
        await engine.dispose()


async def test_the_inventory_page_stops_scanning_the_k_profile_link_table(tmp_path):
    """``selectinload(Spool.k_profiles)`` runs on every inventory page open and
    had no index to enter by — a full scan of the link table each time."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'kp.db').as_posix()}")
    lookup = "SELECT id FROM spool_k_profile WHERE spool_id IN (1, 2, 3)"
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE TABLE print_archives (id INTEGER PRIMARY KEY, printer_id INTEGER, "
                    "deleted_at TIMESTAMP, created_at TIMESTAMP, content_hash VARCHAR(64), "
                    "source_content_hash VARCHAR(64))"
                )
            )
            await conn.execute(text("CREATE TABLE spool_k_profile (id INTEGER PRIMARY KEY, spool_id INTEGER)"))
            before = " ".join(str(r) for r in (await conn.execute(text(f"EXPLAIN QUERY PLAN {lookup}"))).all())
            assert "SCAN" in before.upper()

            await m168.upgrade(conn)
            after = " ".join(str(r) for r in (await conn.execute(text(f"EXPLAIN QUERY PLAN {lookup}"))).all())
        assert "ix_spool_k_profile_spool_id" in after
    finally:
        await engine.dispose()
