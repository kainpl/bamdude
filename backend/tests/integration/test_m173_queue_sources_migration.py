"""m173 on a populated database that predates it.

The test engine builds TODAY's schema from the models, and the two new columns
cannot simply be taken back off it — SQLite refuses to drop a column a foreign
key names, and refuses again if an index names it. So the pre-m173 database is
written out by hand here, the way ``test_m172`` and ``test_m163`` do it: the
parents the foreign keys point at, the two queue tables as they stood BEFORE
this migration (``archive_id`` still ``ON DELETE CASCADE``, no
``queue_source_id``, no ``source_snapshot``, no ``queue_sources`` at all), and
real rows in both of them.

What matters is what an *existing* farm gets: the table, its constraints and its
index; two NULL columns on both queues; and not one byte changed in the work
already queued. Plus the two things a migration is usually wrong about — a second
run (``DEBUG=true`` re-runs the head migration on every boot) and the dialect the
maintainer does not develop on.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import backend.app.migrations.helpers as helpers
from backend.app.migrations import m173_queue_sources as m173
from backend.app.migrations.helpers import column_exists, table_exists

pytestmark = pytest.mark.integration


#: The shape a 0.5.6 database actually has where m173 touches it. Only the
#: columns this migration reads or is asked about, plus what the FKs need.
_PRE_M173_DDL = (
    "CREATE TABLE print_archives (id INTEGER PRIMARY KEY, filename TEXT)",
    "CREATE TABLE library_files (id INTEGER PRIMARY KEY, filename TEXT)",
    "CREATE TABLE printer_queues (id INTEGER PRIMARY KEY, printer_id INTEGER)",
    """
    CREATE TABLE print_queue (
        id INTEGER PRIMARY KEY,
        queue_id INTEGER NOT NULL REFERENCES printer_queues(id),
        waiting_reason TEXT,
        archive_id INTEGER REFERENCES print_archives(id) ON DELETE CASCADE,
        library_file_id INTEGER REFERENCES library_files(id) ON DELETE SET NULL,
        position INTEGER NOT NULL DEFAULT 0,
        plate_id INTEGER,
        status VARCHAR(20) NOT NULL DEFAULT 'pending',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE auto_queue_items (
        id INTEGER PRIMARY KEY,
        archive_id INTEGER REFERENCES print_archives(id) ON DELETE CASCADE,
        library_file_id INTEGER REFERENCES library_files(id) ON DELETE SET NULL,
        target_model VARCHAR(50),
        position INTEGER NOT NULL DEFAULT 0,
        plate_id INTEGER,
        status VARCHAR(20) NOT NULL DEFAULT 'pending'
    )
    """,
)

#: A farm mid-shift: a per-printer job waiting on a plate, and a router row.
_ROWS = (
    "INSERT INTO printer_queues (id, printer_id) VALUES (4, 4)",
    "INSERT INTO library_files (id, filename) VALUES (9, 'lamp.gcode.3mf')",
    "INSERT INTO print_queue (id, queue_id, library_file_id, position, plate_id, status, waiting_reason) "
    "VALUES (11, 4, 9, 3, 2, 'pending', 'Plate not cleared')",
    "INSERT INTO auto_queue_items (id, library_file_id, target_model, position, plate_id, status) "
    "VALUES (21, 9, 'X1C', 1, 2, 'pending')",
)


@pytest.fixture
async def pre_m173(tmp_path):
    """A populated database at migration level 172."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'pre-m173.db').as_posix()}")
    async with engine.begin() as conn:
        for ddl in _PRE_M173_DDL:
            await conn.exec_driver_sql(ddl)
        for sql in _ROWS:
            await conn.exec_driver_sql(sql)
        assert not await table_exists(conn, "queue_sources")
        assert not await column_exists(conn, "print_queue", "queue_source_id")
        assert not await column_exists(conn, "auto_queue_items", "source_snapshot")
    try:
        yield engine
    finally:
        await engine.dispose()


async def _run_upgrade(engine):
    async with engine.begin() as conn:
        # What the runner does around a migration's DDL on SQLite.
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        await m173.upgrade(conn)


def test_the_migration_declares_its_version_and_name():
    assert m173.version == 173
    assert m173.name == "queue_sources"


async def test_the_table_and_its_index_appear(pre_m173):
    await _run_upgrade(pre_m173)

    async with pre_m173.begin() as conn:
        assert await table_exists(conn, "queue_sources")
        info = (await conn.execute(text("PRAGMA table_info(queue_sources)"))).fetchall()
        cols = {row[1]: (row[2].upper(), row[3]) for row in info}
        assert set(cols) == {
            "id",
            "sha256",
            "size_bytes",
            "relative_path",
            "format",
            "state",
            "created_at",
            "unreferenced_at",
        }
        assert cols["sha256"][1] == 1, "the hash is the identity — it may not be NULL"
        assert cols["size_bytes"][1] == 1
        assert cols["relative_path"][1] == 1
        assert cols["unreferenced_at"][1] == 0, "only the GC's hint; NULL means nobody let go yet"

        indexes = (await conn.execute(text("PRAGMA index_list(queue_sources)"))).fetchall()
        assert "ix_queue_sources_state_unreferenced_at" in {row[1] for row in indexes}
        unique_columns: set[str] = set()
        for row in (r for r in indexes if r[2] == 1):
            unique_columns |= {c[2] for c in (await conn.execute(text(f"PRAGMA index_info({row[1]})"))).fetchall()}
        assert "sha256" in unique_columns, "the same bytes must be one row (S4)"


async def test_the_two_columns_land_null_on_both_queues(pre_m173):
    await _run_upgrade(pre_m173)

    async with pre_m173.begin() as conn:
        for table, row_id in (("print_queue", 11), ("auto_queue_items", 21)):
            assert await column_exists(conn, table, "queue_source_id")
            assert await column_exists(conn, table, "source_snapshot")
            indexes = {r[1] for r in (await conn.execute(text(f"PRAGMA index_list({table})"))).fetchall()}
            assert f"ix_{table}_queue_source_id" in indexes
            row = (
                await conn.execute(
                    text(f"SELECT queue_source_id, source_snapshot FROM {table} WHERE id = :i"), {"i": row_id}
                )
            ).one()
            assert row == (None, None), "no backfill: an existing row comes out legacy (spec §8)"


async def test_the_work_already_queued_is_untouched(pre_m173):
    """No backfill, no SMB I/O, no status change — spec §8. Hydration happens
    later in a bounded background pass; a migration that moved a queue row would
    move somebody's shift."""
    await _run_upgrade(pre_m173)

    async with pre_m173.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT queue_id, library_file_id, position, plate_id, status, waiting_reason "
                    "FROM print_queue WHERE id = 11"
                )
            )
        ).one()
        assert row == (4, 9, 3, 2, "pending", "Plate not cleared")
        row = (
            await conn.execute(
                text(
                    "SELECT library_file_id, target_model, position, plate_id, status FROM auto_queue_items WHERE id = 21"
                )
            )
        ).one()
        assert row == (9, "X1C", 1, 2, "pending")
        assert (await conn.execute(text("SELECT COUNT(*) FROM print_queue"))).scalar() == 1
        assert (await conn.execute(text("SELECT COUNT(*) FROM auto_queue_items"))).scalar() == 1


async def test_the_constraints_reach_an_upgraded_database_too(pre_m173):
    """A CHECK written for fresh installs only never reaches an existing file —
    SQLite cannot add one without rebuilding the table, so they are part of the
    CREATE. All five are exercised against the migrated database itself."""
    await _run_upgrade(pre_m173)

    insert = "INSERT INTO queue_sources (sha256, size_bytes, relative_path, format, state) VALUES (:h, :n, 'p', :f, :s)"
    good = {"h": "a" * 64, "n": 10, "f": "3mf", "s": "ready"}
    async with pre_m173.begin() as conn:
        await conn.execute(text(insert), good)

    for bad, why in (
        ({**good, "h": "a" * 64}, "UNIQUE"),
        ({**good, "h": "b" * 63}, "CHECK"),
        ({**good, "h": ""}, "CHECK"),
        ({**good, "h": "b" * 64, "n": -1}, "CHECK"),
        ({**good, "h": "c" * 64, "f": "stl"}, "CHECK"),
        ({**good, "h": "d" * 64, "s": "preparing"}, "CHECK"),
    ):
        async with pre_m173.begin() as conn:
            with pytest.raises(Exception, match=why):
                await conn.execute(text(insert), bad)

    async with pre_m173.begin() as conn:
        assert (await conn.execute(text("SELECT COUNT(*) FROM queue_sources"))).scalar() == 1


async def test_a_second_run_changes_nothing(pre_m173):
    """``DEBUG=true`` re-runs the head migration on every boot, and an attached
    job must survive it."""
    await _run_upgrade(pre_m173)
    async with pre_m173.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO queue_sources (id, sha256, size_bytes, relative_path, format, state) "
                "VALUES (1, :h, 10, 'queue-spool/objects/aa/aa.3mf', '3mf', 'ready')"
            ),
            {"h": "a" * 64},
        )
        await conn.execute(text("UPDATE print_queue SET queue_source_id = 1 WHERE id = 11"))

    await _run_upgrade(pre_m173)

    async with pre_m173.begin() as conn:
        assert (await conn.execute(text("SELECT COUNT(*) FROM queue_sources"))).scalar() == 1
        assert (await conn.execute(text("SELECT queue_source_id FROM print_queue WHERE id = 11"))).scalar() == 1


async def test_a_fresh_install_is_a_noop(test_engine):
    """``create_all`` built everything from the models; running the migration over
    it twice changes nothing.

    ⚠️ Passing here does NOT by itself prove the model and the migration describe
    the same table: ``add_column`` short-circuits on the column *name*, and
    ``CREATE INDEX IF NOT EXISTS`` under a name the models do not use would add a
    **second** index rather than fail. The actual guard is the index-name
    assertions at the end — that the names the migration writes are the names
    ``create_all`` generates from the models (``ix_<table>_<column>`` for
    ``index=True``, and the explicit ``Index(...)`` name on ``queue_sources``),
    and that neither queue table ends up with two indexes over the same column.
    """
    for _ in range(2):
        async with test_engine.begin() as conn:
            await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
            await m173.upgrade(conn)

    async with test_engine.begin() as conn:
        assert await table_exists(conn, "queue_sources")
        assert await column_exists(conn, "print_queue", "queue_source_id")
        assert await column_exists(conn, "auto_queue_items", "source_snapshot")

        indexes = {r[1] for r in (await conn.execute(text("PRAGMA index_list(queue_sources)"))).fetchall()}
        assert "ix_queue_sources_state_unreferenced_at" in indexes

        for table in ("print_queue", "auto_queue_items"):
            rows = (await conn.execute(text(f"PRAGMA index_list({table})"))).fetchall()
            assert f"ix_{table}_queue_source_id" in {r[1] for r in rows}
            over_the_column = [
                r[1]
                for r in rows
                if [c[2] for c in (await conn.execute(text(f"PRAGMA index_info({r[1]})"))).fetchall()]
                == ["queue_source_id"]
            ]
            assert over_the_column == [f"ix_{table}_queue_source_id"], over_the_column


# ── The dialect the maintainer does not develop on ──────────────────────────


class _Result:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class _RecordingConn:
    """Everything a migration would send to a server that has neither the table
    nor the columns — the fresh-upgrade path, without a server to run it on."""

    def __init__(self):
        self.statements: list[str] = []

    async def execute(self, clause, params=None):
        self.statements.append(str(clause))
        return _Result([])

    async def exec_driver_sql(self, sql):
        self.statements.append(sql)
        return _Result([])


async def _record(monkeypatch, *, postgres: bool) -> _RecordingConn:
    monkeypatch.setattr(m173, "is_sqlite", lambda: not postgres)
    monkeypatch.setattr(m173, "is_postgres", lambda: postgres)
    monkeypatch.setattr(helpers, "is_postgres", lambda: postgres)
    conn = _RecordingConn()
    await m173.upgrade(conn)
    return conn


async def test_postgresql_gets_ddl_it_can_parse_and_a_key_that_numbers_itself(monkeypatch):
    conn = await _record(monkeypatch, postgres=True)

    created = [sql for sql in conn.statements if "CREATE TABLE" in sql.upper()]
    assert len(created) == 1, conn.statements
    ddl = created[0].upper()
    for token in ("AUTOINCREMENT", "DATETIME", "WITHOUT ROWID"):
        assert token not in ddl, created[0]
    assert "SERIAL PRIMARY KEY" in ddl
    assert "TIMESTAMP" in ddl
    added = [sql for sql in conn.statements if "ADD COLUMN" in sql.upper()]
    assert len(added) == 4, conn.statements
    assert any("JSON" in sql.upper() for sql in added), "the dialect has a json type — use it"


async def test_postgresql_rewrites_the_archive_rule_on_both_queues(monkeypatch):
    """Spec §4: the CASCADE that deleted a job when its archive went away. Only
    PostgreSQL enforces an ON DELETE rule at all, so only PostgreSQL is
    rewritten — and the old constraint is looked up rather than assumed, because
    a database that has been through a table rebuild carries a generated name."""
    conn = await _record(monkeypatch, postgres=True)

    joined = " ".join(conn.statements)
    for table in ("print_queue", "auto_queue_items"):
        assert f"WHERE t.relname = '{table}' AND a.attname = 'archive_id'" in joined
        assert f"ADD CONSTRAINT {table}_archive_id_fkey" in joined
    assert joined.upper().count("REFERENCES PRINT_ARCHIVES(ID) ON DELETE SET NULL") == 2


async def test_sqlite_never_rebuilds_a_queue_table(monkeypatch):
    """A ``recreate_table`` of ``print_queue`` would mean re-listing ~50 columns
    by hand to change a rule SQLite never enforces — risking real work to correct
    a comment. A copy-drop-rename of the live queue is the one operation here
    that can lose somebody's shift. The model carries SET NULL for fresh installs
    and for the PostgreSQL conform pass; the detach an upgraded SQLite file needs
    is code, and lands in Task 9."""
    conn = await _record(monkeypatch, postgres=False)

    joined = " ".join(conn.statements).upper()
    assert "CREATE TABLE PRINT_QUEUE" not in joined
    assert "CREATE TABLE AUTO_QUEUE_ITEMS" not in joined
    assert "DROP TABLE" not in joined
    assert "DROP CONSTRAINT" not in joined
    assert "FKEY" not in joined
    # And it still does its own work.
    assert "CREATE TABLE QUEUE_SOURCES" in joined
    assert joined.count("ADD COLUMN") == 4
