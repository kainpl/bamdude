"""P-ID spec §A, T1: m262 through the REAL runner on a pooled aiosqlite connection.

The pool holds one physical connection, so the ``PRAGMA foreign_keys`` the runner hands
back, and whether it hands back the same connection at all, are what the next checkout
sees. Two stored shapes of ``printers`` are rebuilt:

* ``table_pk`` — ``create_all``'s ``id INTEGER NOT NULL`` + ``PRIMARY KEY (id)``;
* ``historical`` — the real cold chain m000…m261: m021's inline ``id INTEGER PRIMARY KEY``
  with every later ``ADD COLUMN`` after it — the shape a long-lived install has.
"""

import uuid

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateIndex, CreateTable

from backend.app import migrations
from backend.app.core.config import settings
from backend.app.core.database import Base, configure_sqlite_connection, import_all_models
from backend.app.core.printer_id_bound import MAX_PRINTER_ID
from backend.app.migrations import m262_printer_id_never_reissued as m262

pytestmark = pytest.mark.asyncio


def _legacy_printers_ddl(dialect) -> list[str]:
    table = Base.metadata.tables["printers"]
    options = table.dialect_options["sqlite"]
    options["autoincrement"] = False
    try:
        statements = [str(CreateTable(table).compile(dialect=dialect))]
    finally:
        options["autoincrement"] = True
    statements += [str(CreateIndex(index).compile(dialect=dialect)) for index in table.indexes]
    return statements


async def _fill(conn, table: str, **values) -> None:
    """Insert a row, giving every other NOT NULL column without a default a neutral value. FKs are off."""
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
            else f"x-{table}-{name}-{len(str(values))}-{sorted(values.items())!s}"
        )
    names = ", ".join(f'"{n}"' for n in row)
    marks = ", ".join(f":{n}" for n in row)
    await conn.execute(text(f'INSERT INTO "{table}" ({names}) VALUES ({marks})'), row)


async def _printer(conn, printer_id: int | None = None) -> None:
    unique = str(uuid.uuid4())
    values = {"name": "p", "ip_address": "192.0.2.1", "access_code": "00000000", "serial_number": unique[:20]}
    if printer_id is not None:
        values["id"] = printer_id
    columns = {row[1] for row in (await conn.exec_driver_sql('PRAGMA table_xinfo("printers")')).all()}
    if "connector_identity" in columns:
        values["connector_identity"] = unique
    await _fill(conn, "printers", **values)


async def _mark_applied_below(engine, top: int) -> None:
    await migrations._ensure_migrations_table(engine)
    async with engine.begin() as conn:
        for mig in migrations._discover_migrations():
            if mig["version"] < top:
                await conn.execute(
                    text("INSERT OR IGNORE INTO _migrations (version, name) VALUES (:v, :n)"),
                    {"v": mig["version"], "n": mig["name"]},
                )


def _new_engine(path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", pool_size=1, max_overflow=0)
    event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    return engine


async def _build(shape: str, tmp_path, monkeypatch):
    import_all_models()
    monkeypatch.setattr(settings, "debug", False)
    engine = _new_engine(tmp_path / f"{shape}.db")
    if shape == "historical":
        real_discover = migrations._discover_migrations
        monkeypatch.setattr(
            migrations, "_discover_migrations", lambda: [m for m in real_discover() if m["version"] < 262]
        )
        await migrations.run_all_migrations(engine, async_sessionmaker(engine, expire_on_commit=False))
        monkeypatch.setattr(migrations, "_discover_migrations", real_discover)
    elif shape == "table_pk":
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.exec_driver_sql("DROP TABLE printers")
            for statement in _legacy_printers_ddl(conn.dialect):
                await conn.exec_driver_sql(statement)
        await _mark_applied_below(engine, 262)
    else:  # "fresh": create_all already builds the AUTOINCREMENT table
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _mark_applied_below(engine, 262)
    return engine


async def _deleted_newest_named_everywhere(engine) -> None:
    """Printers 1–3; 3, the newest, deleted while a child row, its queue, an API key, a setting
    and its AMS position label still name it. SQLite without AUTOINCREMENT would hand 3 out again."""
    await _set_foreign_keys(engine, 0)  # the cold chain's runner hands the connection back with them on
    async with engine.begin() as conn:
        for printer_id in (1, 2, 3):
            await _printer(conn, printer_id)
        await conn.exec_driver_sql("DELETE FROM printers WHERE id = 3")
        await _fill(conn, "printer_sensor_history", printer_id=3, sensor_kind="nozzle", value=200.0)
        await _fill(conn, "printer_queues", id=3, printer_id=3)
        await _fill(conn, "api_keys", name="k", key_hash="h", key_prefix="bd_x", printer_ids="[3]")
        await _fill(conn, "settings", key="default_printer_id", value="3")
        await _fill(conn, "ams_labels", ams_serial_number="p3a0", label="Left")


async def _state(engine) -> dict:
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()

        async def one(sql):
            return (await conn.exec_driver_sql(sql)).scalar()

        return {
            "connection": id(raw.driver_connection),
            "foreign_keys": await one("PRAGMA foreign_keys"),
            "legacy_alter_table": await one("PRAGMA legacy_alter_table"),
            "ddl": await one("SELECT sql FROM sqlite_schema WHERE name = 'printers'"),
            "indexes": (
                await conn.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_schema WHERE type = 'index' AND tbl_name = 'printers' ORDER BY name"
                )
            ).all(),
            "sequence": await one("SELECT seq FROM sqlite_sequence WHERE name = 'printers'"),
            "marker": await one("SELECT count(*) FROM _migrations WHERE version = 262"),
            "rows": (await conn.exec_driver_sql("SELECT id, serial_number FROM printers ORDER BY id")).all(),
            "staging": await one("SELECT count(*) FROM sqlite_schema WHERE name LIKE '_c047_%'"),
        }


async def _set_foreign_keys(engine, value: int) -> None:
    async with engine.connect() as conn:
        await conn.exec_driver_sql(f"PRAGMA foreign_keys = {value}")


async def _run(engine) -> None:
    await migrations._run_pending(engine, async_sessionmaker(engine, expire_on_commit=False))


async def _insert_next(engine) -> int:
    async with engine.begin() as conn:
        await _printer(conn)
        return (await conn.exec_driver_sql("SELECT MAX(id) FROM printers")).scalar()


@pytest.fixture(params=["table_pk", "historical"])
async def legacy_engine(request, tmp_path, monkeypatch):
    engine = await _build(request.param, tmp_path, monkeypatch)
    await _deleted_newest_named_everywhere(engine)
    yield engine
    await engine.dispose()


@pytest.mark.parametrize("foreign_keys", [0, 1])
async def test_pending_run_rebuilds_and_hands_back_the_connection_as_it_was(legacy_engine, foreign_keys):
    await _set_foreign_keys(legacy_engine, foreign_keys)
    before = await _state(legacy_engine)
    assert "AUTOINCREMENT" not in before["ddl"].upper()

    await _run(legacy_engine)

    after = await _state(legacy_engine)
    assert "AUTOINCREMENT" in after["ddl"].upper()
    assert after["sequence"] == 3 and after["marker"] == 1 and after["staging"] == 0
    assert after["rows"] == before["rows"] and after["indexes"] == before["indexes"]
    assert after["foreign_keys"] == foreign_keys and after["legacy_alter_table"] == before["legacy_alter_table"]
    assert after["connection"] == before["connection"]
    assert await _insert_next(legacy_engine) == 4
    async with legacy_engine.connect() as conn:  # the new printer does not take the deleted one's AMS name
        labels = (await conn.exec_driver_sql("SELECT label FROM ams_labels WHERE ams_serial_number = 'p4a0'")).all()
    assert labels == []


async def test_an_orphan_above_every_printer_sets_the_counter(legacy_engine):
    async with legacy_engine.begin() as conn:
        await _fill(conn, "auto_queue_returns", id="r", printer_id=40, old_queue_id=40)
    await _run(legacy_engine)
    assert (await _state(legacy_engine))["sequence"] == 40
    assert await _insert_next(legacy_engine) == 41


@pytest.mark.parametrize("foreign_keys", [0, 1])
async def test_debug_replay_takes_the_same_entry_and_only_raises_the_counter(legacy_engine, monkeypatch, foreign_keys):
    await _run(legacy_engine)
    async with legacy_engine.begin() as conn:
        await _fill(conn, "user_notifications", printer_id=50)
    await _set_foreign_keys(legacy_engine, foreign_keys)
    ddl = (await _state(legacy_engine))["ddl"]
    calls = []
    real = m262.upgrade_sqlite
    monkeypatch.setattr(m262, "upgrade_sqlite", lambda c, **kw: calls.append(1) or real(c, **kw))
    monkeypatch.setattr(m262, "replay", lambda _conn: pytest.fail("DEBUG must not replay m262 inside engine.begin()"))
    monkeypatch.setattr(settings, "debug", True)

    await _run(legacy_engine)

    after = await _state(legacy_engine)
    assert calls == [1] and after["ddl"] == ddl and after["sequence"] == 50
    assert after["foreign_keys"] == foreign_keys and after["marker"] == 1


async def test_a_larger_counter_is_kept_on_a_rerun(legacy_engine, monkeypatch):
    await _run(legacy_engine)
    async with legacy_engine.begin() as conn:
        await conn.exec_driver_sql("UPDATE sqlite_sequence SET seq = 70 WHERE name = 'printers'")
    monkeypatch.setattr(settings, "debug", True)
    await _run(legacy_engine)
    assert (await _state(legacy_engine))["sequence"] == 70


@pytest.mark.parametrize("foreign_keys", [0, 1])
async def test_a_failed_rebuild_writes_no_marker_and_keeps_the_pragmas(legacy_engine, monkeypatch, foreign_keys):
    await _set_foreign_keys(legacy_engine, foreign_keys)
    before = await _state(legacy_engine)
    real = m262.upgrade_sqlite

    def failing(conn, **_kw):
        def fault(point):
            if point == "before_commit":
                raise OSError("injected")

        return real(conn, fault=fault)

    monkeypatch.setattr(m262, "upgrade_sqlite", failing)
    with pytest.raises(Exception, match="injected"):
        await _run(legacy_engine)

    after = await _state(legacy_engine)
    assert after["ddl"] == before["ddl"] and after["marker"] == 0 and after["staging"] == 0
    assert after["rows"] == before["rows"] and after["indexes"] == before["indexes"]
    assert after["foreign_keys"] == foreign_keys and after["legacy_alter_table"] == before["legacy_alter_table"]
    assert after["connection"] == before["connection"]


async def test_a_commit_without_its_marker_reruns_as_counter_only(legacy_engine, monkeypatch):
    real_record = migrations._record_migration

    async def record(engine, version, name):
        if version == 262:
            raise OSError("power cut before the marker")
        await real_record(engine, version, name)

    monkeypatch.setattr(migrations, "_record_migration", record)
    with pytest.raises(OSError):
        await _run(legacy_engine)
    middle = await _state(legacy_engine)
    assert "AUTOINCREMENT" in middle["ddl"].upper() and middle["marker"] == 0

    monkeypatch.setattr(migrations, "_record_migration", real_record)
    await _run(legacy_engine)
    after = await _state(legacy_engine)
    assert after["ddl"] == middle["ddl"] and after["marker"] == 1 and after["sequence"] == 3


# --- no printer number left in the domain: stopped before COMMIT, with the real marker ---------


async def _assert_refused_unchanged(engine, foreign_keys: int, match: str = "exhausted") -> None:
    await _set_foreign_keys(engine, foreign_keys)
    before = await _state(engine)
    with pytest.raises(Exception, match=match):
        await _run(engine)
    after = await _state(engine)
    assert after["marker"] == 0 and after["staging"] == 0
    for key in ("ddl", "rows", "indexes", "sequence", "legacy_alter_table", "connection"):
        assert after[key] == before[key], key
    assert after["foreign_keys"] == foreign_keys


@pytest.mark.parametrize("foreign_keys", [0, 1])
async def test_a_legacy_table_with_an_orphan_at_the_top_is_refused(legacy_engine, foreign_keys):
    async with legacy_engine.begin() as conn:
        await _fill(conn, "printer_sensor_history", printer_id=MAX_PRINTER_ID, sensor_kind="bed", value=60.0)
    await _assert_refused_unchanged(legacy_engine, foreign_keys)


async def test_a_printer_number_outside_the_domain_is_refused(legacy_engine):
    async with legacy_engine.begin() as conn:
        await _printer(conn, 3_000_000_000)
    await _assert_refused_unchanged(legacy_engine, 1)


@pytest.mark.parametrize("only_the_counter", [False, True], ids=["orphan", "sqlite_sequence"])
async def test_an_empty_autoincrement_table_at_the_top_is_refused(tmp_path, monkeypatch, only_the_counter):
    engine = await _build("fresh", tmp_path, monkeypatch)
    try:
        async with engine.begin() as conn:
            if only_the_counter:
                await _printer(conn, MAX_PRINTER_ID)
                await conn.exec_driver_sql(f"DELETE FROM printers WHERE id = {MAX_PRINTER_ID}")
            else:
                await _fill(conn, "obico_events", id="e", printer_id=MAX_PRINTER_ID)
        state = await _state(engine)
        assert "AUTOINCREMENT" in state["ddl"].upper() and state["rows"] == []
        assert state["sequence"] == (MAX_PRINTER_ID if only_the_counter else None)
        await _assert_refused_unchanged(engine, 0)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("foreign_keys", [0, 1])
async def test_a_settings_table_of_another_shape_is_refused_before_commit(legacy_engine, foreign_keys):
    """Acceptance PID-A01: a ``settings.value`` that is not text stops the rebuild, nothing changed."""
    async with legacy_engine.begin() as conn:
        await conn.exec_driver_sql("DROP TABLE settings")
        await conn.exec_driver_sql(
            "CREATE TABLE settings (id INTEGER PRIMARY KEY, key VARCHAR(100) NOT NULL UNIQUE, value INTEGER,"
            " created_at DATETIME, updated_at DATETIME)"
        )
        await conn.exec_driver_sql("INSERT INTO settings (key, value) VALUES ('virtual_printer_target_printer_id', 41)")
    await _assert_refused_unchanged(legacy_engine, foreign_keys, match="settings.value: unexpected type")


async def test_one_number_left_still_passes(legacy_engine):
    """The control: a bound of 2³¹ − 2 leaves 2³¹ − 1 to hand out."""
    async with legacy_engine.begin() as conn:
        await _fill(conn, "printer_sensor_history", printer_id=MAX_PRINTER_ID - 1, sensor_kind="bed", value=60.0)
    await _run(legacy_engine)
    assert (await _state(legacy_engine))["marker"] == 1
    assert await _insert_next(legacy_engine) == MAX_PRINTER_ID


# --- the callback itself ----------------------------------------------------------------------


async def test_the_callback_reads_the_registry_and_the_counter_under_the_rebuilds_transaction(
    legacy_engine, monkeypatch
):
    from backend.app.core import printer_id_bound
    from backend.app.migrations._sqlite_autoincrement import _in_transaction

    seen = []
    real_bound = printer_id_bound.sqlite_bound

    def watched(conn, applied):
        seen.append(_in_transaction(conn))
        return real_bound(conn, applied)

    monkeypatch.setattr(printer_id_bound, "sqlite_bound", watched)
    await _run(legacy_engine)
    assert seen == [True]


async def test_a_fresh_install_only_raises_the_counter(tmp_path, monkeypatch):
    """``create_all`` already builds the AUTOINCREMENT table: m262 rebuilds nothing there."""
    engine = await _build("fresh", tmp_path, monkeypatch)
    try:
        async with engine.begin() as conn:
            await _printer(conn)
            await _fill(conn, "cloud_link_printers", printer_id=11)
        before = await _state(engine)
        assert "AUTOINCREMENT" in before["ddl"].upper()

        await _run(engine)

        after = await _state(engine)
        assert after["ddl"] == before["ddl"] and after["sequence"] == 11 and after["marker"] == 1
        assert await _insert_next(engine) == 12
    finally:
        await engine.dispose()
