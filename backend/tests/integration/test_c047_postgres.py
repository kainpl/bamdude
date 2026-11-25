"""C047 part 1 on a disposable embedded PostgreSQL: m261's sequence arm, the SQLite import, the portable export.

T7 — the sequence is raised only upward, under a lock another session's INSERT waits on, and
only for a plain sequence; T8 — the import keeps every number the source remembers, its
orphaned history removed by whichever repair owns it; T9 — the portable copy keeps the
counter PostgreSQL reached, and the round trip back continues above it.
"""

import asyncio
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, Text, text
from sqlalchemy.exc import DBAPIError

from backend.app import migrations
from backend.app.core import db_portable, pg_sequences
from backend.app.core.config import settings
from backend.app.core.database import Base
from backend.app.core.pg_sequences import SequenceError
from backend.app.migrations import m261_spool_id_never_reissued as m261
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_postgres import test_engine  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def migration_dialect(test_engine, monkeypatch):
    monkeypatch.setattr(settings, "database_url", test_engine.url.render_as_string(hide_password=False))


async def _fill(conn, table: str, **values):
    """Insert a row, giving every other NOT NULL column without a default a neutral value. FKs are off."""
    columns = (
        await conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = current_schema()"
                " AND table_name = :t AND is_nullable = 'NO' AND column_default IS NULL"
            ),
            {"t": table},
        )
    ).all()
    row = dict(values)
    for name, data_type in columns:
        if name not in row:
            row[name] = _neutral(data_type)
    names = ", ".join(f'"{n}"' for n in row)
    marks = ", ".join(f":{n}" for n in row)
    await conn.execute(text("SET LOCAL session_replication_role = replica"))
    returning = " RETURNING id" if table != "active_print_sessions" else ""
    returned = await conn.execute(text(f'INSERT INTO "{table}" ({names}) VALUES ({marks}){returning}'), row)
    return returned.scalar() if returning else None


def _neutral(data_type: str):
    from datetime import date, datetime

    if "timestamp" in data_type:
        return datetime(2026, 1, 1)
    if data_type == "date":
        return date(2026, 1, 1)
    if data_type in ("integer", "bigint", "smallint"):
        return 0
    if data_type in ("double precision", "real", "numeric"):
        return 0.0
    if data_type == "boolean":
        return False
    if data_type in ("json", "jsonb"):
        return "{}"
    if data_type == "bytea":
        return b"x"
    return "x"


async def _migrations_at(engine, *versions):
    await migrations._ensure_migrations_table(engine)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM _migrations"))
        for v in versions:
            await conn.execute(text("INSERT INTO _migrations (version, name) VALUES (:v, 'x')"), {"v": v})


async def _next_spool_id(engine) -> int:
    async with engine.begin() as conn:
        return await _fill(conn, "spool", material="PLA")


# --- T7: m261 on PostgreSQL --------------------------------------------------------------------


async def test_m261_raises_the_sequence_above_every_remembered_number(test_engine):
    await _migrations_at(test_engine, 147, 153, 221, 260)
    async with test_engine.begin() as conn:
        for _ in range(3):
            await _fill(conn, "spool", material="PLA")
        await conn.execute(text("DELETE FROM spool WHERE id = 3"))
        await _fill(conn, "label_jobs", spool_id=20)
        await _fill(conn, "active_print_sessions", printer_id=1, spool_assignments='{"0-0": 25}')
    async with test_engine.begin() as conn:
        await m261.upgrade(conn)
    assert await _next_spool_id(test_engine) == 26
    async with test_engine.begin() as conn:
        await m261.replay(conn)  # nothing above 26 remembered now: left alone
    assert await _next_spool_id(test_engine) == 27


async def test_m261_leaves_a_sequence_already_past_the_bound(test_engine):
    await _migrations_at(test_engine, 260)
    async with test_engine.begin() as conn:
        ids = [await _fill(conn, "spool", material="PLA") for _ in range(5)]
        await conn.execute(text("DELETE FROM spool WHERE id > 1"))
        assert ids[-1] == 5
        before = await pg_sequences.sequence_state(conn, "spool")
        await m261.upgrade(conn)
        after = await pg_sequences.sequence_state(conn, "spool")
    assert (before.last_value, before.is_called) == (after.last_value, after.is_called) == (5, True)
    assert await _next_spool_id(test_engine) == 6


async def test_m261_on_an_untouched_sequence_without_spools_changes_nothing(test_engine):
    await _migrations_at(test_engine, 260)
    async with test_engine.begin() as conn:
        await m261.upgrade(conn)
        state = await pg_sequences.sequence_state(conn, "spool")
    assert (state.last_value, state.is_called) == (1, False)
    assert await _next_spool_id(test_engine) == 1


@pytest.mark.parametrize(
    ("alter", "message"),
    [
        ("CACHE 5", "CACHE 1"),
        ("INCREMENT BY 2", "increment 2"),
        ("CYCLE", "NO CYCLE"),
        ("MAXVALUE 25", "exhausted"),
    ],
)
async def test_m261_stops_on_a_sequence_it_cannot_raise_safely(test_engine, alter, message):
    await _migrations_at(test_engine, 260)
    async with test_engine.begin() as conn:
        await _fill(conn, "label_jobs", spool_id=25)
        await conn.execute(text(f"ALTER SEQUENCE spool_id_seq {alter}"))
    with pytest.raises(SequenceError, match=message):
        async with test_engine.begin() as conn:
            await m261.upgrade(conn)


async def test_m261_holds_inserts_off_and_a_rollback_never_lowers_the_sequence(test_engine):
    await _migrations_at(test_engine, 260)
    async with test_engine.begin() as conn:
        await _fill(conn, "label_jobs", spool_id=75)
    holder = await test_engine.connect()
    try:
        transaction = await holder.begin()
        await m261.upgrade(holder)  # LOCK taken, setval done, not committed
        async with test_engine.connect() as other:
            with pytest.raises(DBAPIError, match="lock"):
                async with other.begin():
                    await other.execute(text("SET LOCAL lock_timeout = '300ms'"))
                    await _fill(other, "spool", material="PETG")
        await transaction.rollback()
    finally:
        await holder.close()
    async with test_engine.begin() as conn:
        state = await pg_sequences.sequence_state(conn, "spool")
        marker = (await conn.execute(text("SELECT count(*) FROM _migrations WHERE version = 261"))).scalar()
    assert (state.last_value, state.is_called, marker) == (75, True, 0)  # setval outlives its rollback, upward
    assert await _next_spool_id(test_engine) == 76


async def test_the_json_carrier_counts_on_postgres_json(test_engine):
    await _migrations_at(test_engine, 260)
    async with test_engine.begin() as conn:
        data_type = (
            await conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns WHERE table_name = 'active_print_sessions'"
                    " AND column_name = 'spool_assignments'"
                )
            )
        ).scalar()
        assert data_type in ("json", "jsonb")
        await _fill(conn, "active_print_sessions", printer_id=1, spool_assignments='{"1-2": 33, "x": true}')
        await m261.upgrade(conn)
    assert await _next_spool_id(test_engine) == 34


# --- T8: SQLite → PostgreSQL import ------------------------------------------------------------


def _narrow_model(with_fk: bool) -> MetaData:
    model = MetaData()
    Table("spool", model, Column("id", Integer, primary_key=True))
    fk = [ForeignKey("spool.id", ondelete="CASCADE")] if with_fk else []
    Table("spool_usage_history", model, Column("id", Integer, primary_key=True), Column("spool_id", Integer, *fk))
    Table("settings", model, Column("key", String, primary_key=True), Column("value", Text))
    Table("big", model, Column("id", Integer, primary_key=True))
    Table("projects", model, Column("id", Integer, primary_key=True))
    return model


def _source(path, *, with_fk: bool, script: str = "") -> bytes:
    fk = " REFERENCES spool (id)" if with_fk else ""
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE _migrations(id INTEGER PRIMARY KEY, version INTEGER NOT NULL UNIQUE);"
            "INSERT INTO _migrations VALUES (1, 41);"
            "CREATE TABLE settings(key VARCHAR PRIMARY KEY, value TEXT);"
            "CREATE TABLE spool(id INTEGER NOT NULL, PRIMARY KEY (id));"
            "INSERT INTO spool VALUES (49);"
            f"CREATE TABLE spool_usage_history(id INTEGER PRIMARY KEY, spool_id INTEGER{fk});"
            "INSERT INTO spool_usage_history VALUES (1, 150);" + script
        )
    return path.read_bytes()


async def _next_id(engine, table: str) -> int:
    async with engine.begin() as conn:
        return (await conn.execute(text(f'INSERT INTO "{table}" DEFAULT VALUES RETURNING id'))).scalar()


@pytest.mark.parametrize("with_fk", [True, False], ids=["phase3-repairs", "conformer-repairs"])
async def test_import_keeps_the_numbers_of_history_it_does_not_carry(test_engine, tmp_path, caplog, with_fk):
    from backend.app.core.schema_conform_repair import foreign_keys

    source = tmp_path / "source.db"
    before = _source(source, with_fk=with_fk)
    model = _narrow_model(with_fk=True)
    caplog.set_level("INFO", logger="backend.app.core.db_portable")
    await db_portable.import_sqlite_to_postgres(test_engine, model, source)
    async with test_engine.begin() as conn:
        await foreign_keys(conn, model)  # what conform_imported_schema does after the chain
        remaining = (await conn.execute(text("SELECT count(*) FROM spool_usage_history"))).scalar()
    assert remaining == 0 and source.read_bytes() == before
    assert await _next_id(test_engine, "spool") == 151
    assert "1 usage-history row(s) naming spools it no longer has" in caplog.text
    assert "Spool numbers up to 150 stay reserved" in caplog.text


async def test_import_carries_an_emptied_tables_counter_and_leaves_seq_zero(test_engine, tmp_path):
    source = tmp_path / "source.db"
    _source(
        source,
        with_fk=True,
        script=(
            "CREATE TABLE big(id INTEGER PRIMARY KEY AUTOINCREMENT);"
            "INSERT INTO big VALUES (60); DELETE FROM big;"  # emptied, sqlite_sequence keeps 60
        ),
    )
    model = _narrow_model(with_fk=True)
    await db_portable.import_sqlite_to_postgres(test_engine, model, source)
    assert await _next_id(test_engine, "big") == 61

    zero = tmp_path / "zero.db"
    _source(
        zero,
        with_fk=True,
        script="CREATE TABLE big(id INTEGER PRIMARY KEY AUTOINCREMENT); INSERT INTO sqlite_sequence VALUES ('big', 0);",
    )
    await db_portable.import_sqlite_to_postgres(test_engine, model, zero)
    assert await _next_id(test_engine, "big") == 1


async def test_import_refuses_a_counter_its_sequence_cannot_hold_and_keeps_the_target(test_engine, tmp_path, caplog):
    model = _narrow_model(with_fk=True)
    good = tmp_path / "good.db"
    _source(good, with_fk=True)
    await db_portable.import_sqlite_to_postgres(test_engine, model, good)
    bad = tmp_path / "bad.db"
    _source(
        bad,
        with_fk=True,
        script="CREATE TABLE big(id INTEGER PRIMARY KEY AUTOINCREMENT); INSERT INTO sqlite_sequence VALUES ('big', 3000000000);",
    )
    caplog.clear()
    caplog.set_level("INFO", logger="backend.app.core.db_portable")
    with pytest.raises(SequenceError, match="big"):
        await db_portable.import_sqlite_to_postgres(test_engine, model, bad)
    assert "stay reserved" not in caplog.text  # a rolled-back import claims nothing carried
    async with test_engine.begin() as conn:  # the previous import stands
        assert (await conn.execute(text("SELECT id FROM spool"))).scalars().all() == [49]


async def test_import_carries_the_m158_trios_counter_by_the_general_rule(test_engine, tmp_path):
    """A post-m158 file has ``projects`` (orders) as an AUTOINCREMENT table: its counter comes along."""
    source = tmp_path / "source.db"
    _source(
        source,
        with_fk=True,
        script="CREATE TABLE projects(id INTEGER PRIMARY KEY AUTOINCREMENT); INSERT INTO projects VALUES (80); DELETE FROM projects; INSERT INTO projects VALUES (10);",
    )
    await db_portable.import_sqlite_to_postgres(test_engine, _narrow_model(with_fk=True), source)
    assert await _next_id(test_engine, "projects") == 81


# --- T9: PostgreSQL → portable SQLite → back --------------------------------------------------


async def _full_schema_at_head(engine):
    await migrations._ensure_migrations_table(engine)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM _migrations"))
        for mig in migrations._discover_migrations():
            await conn.execute(
                text("INSERT INTO _migrations (version, name) VALUES (:v, :n)"), {"v": mig["version"], "n": mig["name"]}
            )


async def test_the_portable_copy_keeps_the_counter_postgres_reached(test_engine, tmp_path, monkeypatch):
    await _full_schema_at_head(test_engine)
    async with test_engine.begin() as conn:
        for _ in range(4):
            await _fill(conn, "spool", material="PLA")
        await conn.execute(text("DELETE FROM spool WHERE id = 4"))
        product = await _fill(conn, "products", name="Widget")
        for key in ("a", "b"):
            await _fill(conn, "stock_items", product_id=product, config_key=key)
        await conn.execute(text("DELETE FROM stock_items WHERE id = 2"))

    real = db_portable._carry_id_counters

    async def racing(conn, portable, dst):
        # Another session inserts and deletes after the snapshot began: the copy must
        # still never get those numbers back.
        async with test_engine.begin() as other:
            raced = await _fill(other, "spool", material="RACE")
            await other.execute(text("DELETE FROM spool WHERE id = :i"), {"i": raced})
        await real(conn, portable, dst)

    monkeypatch.setattr(db_portable, "_carry_id_counters", racing)
    copy = tmp_path / "portable.db"
    await db_portable.dump_to_sqlite(test_engine, Base.metadata, copy)

    with closing(sqlite3.connect(copy)) as db:
        seqs = dict(db.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
        assert seqs["spool"] == 5 and seqs["stock_items"] == 2
        assert db.execute("SELECT MAX(id) FROM spool").fetchone()[0] == 3  # the snapshot did not see the race
        db.execute("PRAGMA foreign_keys = OFF")
        cursor = db.execute(
            "INSERT INTO spool (material, label_weight, core_weight, weight_used, weight_used_baseline, weight_locked,"
            " filament_diameter, low_stock_notified, created_at, updated_at)"
            " VALUES ('PETG', 1000, 0, 0, 0, 0, '1.75', 0, '2026-01-01', '2026-01-01')"
        )
        assert cursor.lastrowid == 6
        db.commit()

    monkeypatch.setattr(db_portable, "_carry_id_counters", real)
    await db_portable.import_sqlite_to_postgres(test_engine, Base.metadata, copy)
    assert await _next_spool_id(test_engine) == 7


async def test_a_failed_export_keeps_the_previous_copy(test_engine, tmp_path, monkeypatch):
    await _full_schema_at_head(test_engine)
    copy = tmp_path / "portable.db"
    await db_portable.dump_to_sqlite(test_engine, Base.metadata, copy)
    before = copy.read_bytes()

    async def broken(*_args):
        await asyncio.sleep(0)
        raise SequenceError("injected")

    monkeypatch.setattr(db_portable, "_carry_id_counters", broken)
    with pytest.raises(SequenceError):
        await db_portable.dump_to_sqlite(test_engine, Base.metadata, copy)
    assert copy.read_bytes() == before
    with closing(sqlite3.connect(copy)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
