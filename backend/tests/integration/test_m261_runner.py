"""C047 spec §A, T2/T4/T5: m261 through the REAL runner on a pooled aiosqlite connection.

The pool holds one physical connection, so the ``PRAGMA foreign_keys`` the runner hands
back, and whether it hands back the same connection at all, are what the next checkout
sees. The schema is the full current one with ``spool`` put back into the shape every
installation before m261 has (``create_all``'s ``id INTEGER NOT NULL`` + ``PRIMARY KEY (id)``).
"""

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

from backend.app import migrations
from backend.app.core.config import settings
from backend.app.core.database import Base, configure_sqlite_connection, import_all_models
from backend.app.migrations import m261_spool_id_never_reissued as m261

pytestmark = pytest.mark.asyncio


def _legacy_spool_ddl(dialect) -> list[str]:
    table = Base.metadata.tables["spool"]
    options = table.dialect_options["sqlite"]
    options["autoincrement"] = False
    try:
        statements = [str(CreateTable(table).compile(dialect=dialect))]
    finally:
        options["autoincrement"] = True
    statements += [str(CreateIndex(index).compile(dialect=dialect)) for index in table.indexes]
    return statements


async def _fill(conn, table: str, **values) -> None:
    """Insert a row, giving every other NOT NULL column without a default a neutral value."""
    columns = (await conn.exec_driver_sql(f'PRAGMA table_xinfo("{table}")')).all()
    row = dict(values)
    for _cid, name, declared, notnull, default, pk, _hidden in columns:
        if name in row or pk or not notnull or default is not None:
            continue
        declared = (declared or "").upper()
        row[name] = (
            "2026-01-01 00:00:00"
            if "DATE" in declared or "TIME" in declared
            else 0
            if "INT" in declared or "BOOL" in declared or "FLOAT" in declared or "NUMERIC" in declared
            else "x"
        )
    names = ", ".join(f'"{n}"' for n in row)
    marks = ", ".join(f":{n}" for n in row)
    await conn.execute(text(f'INSERT INTO "{table}" ({names}) VALUES ({marks})'), row)


@pytest.fixture
async def legacy_engine(tmp_path, monkeypatch):
    import_all_models()
    path = tmp_path / "legacy.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", pool_size=1, max_overflow=0)
    event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    monkeypatch.setattr(settings, "debug", False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.exec_driver_sql("DROP TABLE spool")
        for statement in _legacy_spool_ddl(conn.dialect):
            await conn.exec_driver_sql(statement)
    await migrations._ensure_migrations_table(engine)
    async with engine.begin() as conn:
        for mig in migrations._discover_migrations():
            if mig["version"] < 261:
                await conn.execute(
                    text("INSERT INTO _migrations (version, name) VALUES (:v, :n)"),
                    {"v": mig["version"], "n": mig["name"]},
                )
        for spool_id, material in ((1, "PLA"), (2, "PETG"), (3, "ABS")):
            await _fill(
                conn,
                "spool",
                id=spool_id,
                material=material,
                label_identity=f"00000000-0000-0000-0000-00000000000{spool_id}",
            )
        await conn.exec_driver_sql("DELETE FROM spool WHERE id = 3")  # the newest: SQLite would hand 3 out again
        await _fill(conn, "label_jobs", spool_id=11)  # a label job outlived spool 11
        await _fill(conn, "spool_usage_history", spool_id=7)  # and history outlived spool 7
        await conn.execute(
            text(
                "INSERT INTO active_print_sessions (printer_id, print_name, started_at, tray_now_at_start, spool_assignments) VALUES (5, 'x', '2026-01-01', 0, :a)"
            ),
            {"a": '{"0-0": 9}'},
        )
    yield engine
    await engine.dispose()


async def _state(engine) -> dict:
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        return {
            "connection": id(raw.driver_connection),
            "foreign_keys": (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar(),
            "ddl": (await conn.exec_driver_sql("SELECT sql FROM sqlite_schema WHERE name = 'spool'")).scalar(),
            "sequence": (await conn.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name = 'spool'")).scalar(),
            "marker": (await conn.exec_driver_sql("SELECT count(*) FROM _migrations WHERE version = 261")).scalar(),
            "rows": (await conn.exec_driver_sql("SELECT id, material FROM spool ORDER BY id")).all(),
            "staging": (
                await conn.exec_driver_sql("SELECT count(*) FROM sqlite_schema WHERE name LIKE '_c047_%'")
            ).scalar(),
        }


async def _set_foreign_keys(engine, value: int) -> None:
    async with engine.connect() as conn:
        await conn.exec_driver_sql(f"PRAGMA foreign_keys = {value}")


def _session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.parametrize("foreign_keys", [0, 1])
async def test_pending_run_rebuilds_and_hands_back_the_connection_as_it_was(legacy_engine, foreign_keys):
    await _set_foreign_keys(legacy_engine, foreign_keys)
    before = await _state(legacy_engine)
    assert "AUTOINCREMENT" not in before["ddl"].upper()

    await migrations._run_pending(legacy_engine, _session_factory(legacy_engine))

    after = await _state(legacy_engine)
    assert "AUTOINCREMENT" in after["ddl"].upper()
    assert after["sequence"] == 11 and after["marker"] == 1 and after["staging"] == 0
    assert after["rows"] == before["rows"]
    assert after["foreign_keys"] == foreign_keys
    assert after["connection"] == before["connection"]
    async with legacy_engine.begin() as conn:
        await _fill(conn, "spool", material="TPU", label_identity="00000000-0000-0000-0000-000000000099")
        assert (await conn.exec_driver_sql("SELECT MAX(id) FROM spool")).scalar() == 12


@pytest.mark.parametrize("foreign_keys", [0, 1])
async def test_debug_replay_takes_the_same_entry_and_only_raises_the_counter(legacy_engine, monkeypatch, foreign_keys):
    await migrations._run_pending(legacy_engine, _session_factory(legacy_engine))
    async with legacy_engine.begin() as conn:
        await _fill(conn, "label_jobs", spool_id=40)
    await _set_foreign_keys(legacy_engine, foreign_keys)
    ddl = (await _state(legacy_engine))["ddl"]
    real_discover = migrations._discover_migrations  # later migrations must not take m261's place as the head
    monkeypatch.setattr(migrations, "_discover_migrations", lambda: [m for m in real_discover() if m["version"] <= 261])
    calls = []
    real = m261.upgrade_sqlite
    monkeypatch.setattr(m261, "upgrade_sqlite", lambda c, **kw: calls.append(1) or real(c, **kw))
    monkeypatch.setattr(m261, "replay", lambda _conn: pytest.fail("DEBUG must not replay m261 inside engine.begin()"))
    monkeypatch.setattr(settings, "debug", True)

    await migrations._run_pending(legacy_engine, _session_factory(legacy_engine))

    after = await _state(legacy_engine)
    assert calls == [1] and after["ddl"] == ddl and after["sequence"] == 40
    assert after["foreign_keys"] == foreign_keys and after["marker"] == 1


@pytest.mark.parametrize("foreign_keys", [0, 1])
async def test_a_failed_rebuild_writes_no_marker_and_keeps_the_pragma(legacy_engine, monkeypatch, foreign_keys):
    await _set_foreign_keys(legacy_engine, foreign_keys)
    before = await _state(legacy_engine)
    real = m261.upgrade_sqlite

    def failing(conn, **_kw):
        def fault(point):
            if point == "after_drop":
                raise OSError("injected")

        return real(conn, fault=fault)

    monkeypatch.setattr(m261, "upgrade_sqlite", failing)
    with pytest.raises(Exception, match="injected"):
        await migrations._run_pending(legacy_engine, _session_factory(legacy_engine))

    after = await _state(legacy_engine)
    assert after["ddl"] == before["ddl"] and after["marker"] == 0 and after["staging"] == 0
    assert after["rows"] == before["rows"] and after["foreign_keys"] == foreign_keys
    assert after["connection"] == before["connection"]


async def test_a_commit_without_its_marker_reruns_as_counter_only(legacy_engine, monkeypatch):
    real_record = migrations._record_migration

    async def record(engine, version, name):
        if version == 261:
            raise OSError("power cut before the marker")
        await real_record(engine, version, name)

    monkeypatch.setattr(migrations, "_record_migration", record)
    with pytest.raises(OSError):
        await migrations._run_pending(legacy_engine, _session_factory(legacy_engine))
    middle = await _state(legacy_engine)
    assert "AUTOINCREMENT" in middle["ddl"].upper() and middle["marker"] == 0

    monkeypatch.setattr(migrations, "_record_migration", real_record)
    await migrations._run_pending(legacy_engine, _session_factory(legacy_engine))
    after = await _state(legacy_engine)
    assert after["ddl"] == middle["ddl"] and after["marker"] == 1 and after["sequence"] == 11


async def test_an_unproven_rollback_discards_the_connection(legacy_engine, monkeypatch):
    from backend.app.migrations import _sqlite_autoincrement as helper

    before = await _state(legacy_engine)
    real_run, real_upgrade = helper._run, m261.upgrade_sqlite

    def run(conn, sql, params=()):
        if sql == "ROLLBACK":
            raise OSError("rollback refused")
        return real_run(conn, sql, params)

    def failing(conn, **_kw):
        def fault(point):
            if point == "after_copy":
                raise OSError("injected")

        return real_upgrade(conn, fault=fault)

    monkeypatch.setattr(helper, "_run", run)
    monkeypatch.setattr(m261, "upgrade_sqlite", failing)
    with pytest.raises(helper.SelfManagedFailure) as caught:
        await migrations._run_pending(legacy_engine, _session_factory(legacy_engine))
    assert not caught.value.connection_safe
    monkeypatch.setattr(helper, "_run", real_run)
    after = await _state(legacy_engine)
    assert after["connection"] != before["connection"]  # the pool did not hand the unproven one back


async def test_a_fresh_install_only_raises_the_counter(tmp_path, monkeypatch):
    """``create_all`` already builds the AUTOINCREMENT table: m261 rebuilds nothing there."""
    import_all_models()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh.db'}", pool_size=1, max_overflow=0)
    event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    monkeypatch.setattr(settings, "debug", False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await migrations._ensure_migrations_table(engine)
        async with engine.begin() as conn:
            for mig in migrations._discover_migrations():
                if mig["version"] < 261:
                    await conn.execute(
                        text("INSERT INTO _migrations (version, name) VALUES (:v, :n)"),
                        {"v": mig["version"], "n": mig["name"]},
                    )
            await _fill(conn, "spool", material="PLA", label_identity="00000000-0000-0000-0000-0000000000f1")
            await _fill(conn, "label_jobs", spool_id=11)
        before = await _state(engine)
        assert "AUTOINCREMENT" in before["ddl"].upper()

        await migrations._run_pending(engine, _session_factory(engine))

        after = await _state(engine)
        assert after["ddl"] == before["ddl"] and after["sequence"] == 11 and after["marker"] == 1
    finally:
        await engine.dispose()
