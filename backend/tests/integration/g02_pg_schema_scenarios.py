"""Historical fixtures were produced by the actual pinned code, not head-minus-markers."""

import gzip
import hashlib
import io
import json
import re
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError

from backend.tests.integration.pg_schema_oracle import capture

FIXTURES = Path(__file__).parent / "fixtures/g02_pg_schema"


def unpack(name, directory):
    receipt = next(r for r in json.loads((FIXTURES / "provenance.json").read_text()) if r["name"] == name)
    compressed = (FIXTURES / f"{name}.db.gz").read_bytes()
    assert hashlib.sha256(compressed).hexdigest() == receipt["compressed_sha256"]
    data = gzip.decompress(compressed)
    assert hashlib.sha256(data).hexdigest() == receipt["sha256"]
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "bamdude.db"
    target.write_bytes(data)
    return target


async def load_failed_pg(database, name):
    """Replay a trusted pg_restore SQL fixture, including COPY through PG's protocol.

    These are actual failure dumps, never a new schema with removed markers.
    No host pg_restore/client-version dependency is needed in CI.
    """
    receipt = next(r for r in json.loads((FIXTURES / "retry-provenance.json").read_text()) if r["name"] == name)
    data = gzip.decompress((FIXTURES / (name + ".sql.gz")).read_bytes())
    assert hashlib.sha256(data).hexdigest() == receipt["sha256"]
    async with database.engine.connect() as conn:
        raw = (await conn.get_raw_connection()).driver_connection
        await raw.execute("DROP FUNCTION IF EXISTS public.archive_search_vector_update() CASCADE")
        pending = []
        lines = iter(data.decode("utf-8").splitlines(keepends=True))
        for line in lines:
            if line.startswith("\\restrict ") or line.startswith("\\unrestrict "):
                continue
            match = re.fullmatch(r"COPY public\.([a-z_][a-z_0-9]*) \(([^)]+)\) FROM stdin;\s*", line)
            if not match:
                pending.append(line)
                continue
            if any(p.strip() and not p.lstrip().startswith("--") for p in pending):
                await raw.execute("".join(pending))
            pending = []
            contents = []
            for copied in lines:
                if copied.rstrip("\r\n") == "\\.":
                    break
                contents.append(copied)
            names = [c.strip().strip('"') for c in match[2].split(",")]
            assert all(re.fullmatch("[a-z_][a-z_0-9]*", c) for c in names)
            await raw.copy_to_table(
                match[1],
                schema_name="public",
                columns=names,
                source=io.BytesIO("".join(contents).encode()),
                format="text",
            )
        if any(p.strip() and not p.lstrip().startswith("--") for p in pending):
            await raw.execute("".join(pending))
        # pg_restore owns a short-lived connection. This loader uses a pool,
        # so do not leak its empty search_path or other restore-only settings.
        await raw.execute("RESET ALL")
        assert await raw.fetchval("SELECT max(version) FROM _migrations") == receipt["max_version"]


async def run(case):
    from backend.app.core import database
    from backend.app.core.config import settings
    from backend.app.migrations import m228_postgres_historical_schema as repair

    if case.startswith("import:"):
        unpack(case.split(":")[1], Path(settings.data_dir))
    if case == "prepare_head_v061":
        unpack("v061", Path(settings.data_dir))
    if case.startswith("retry"):
        await load_failed_pg(database, case)
    await database.init_db()
    if case in ("prepare_head_v061", "prepare_head_fresh", "prepare_head_fresh_affected"):
        assert database.engine.dialect.name == "sqlite"
        if case == "prepare_head_fresh_affected":
            async with database.engine.begin() as conn:
                await conn.execute(
                    text("CREATE UNIQUE INDEX uq_user_notifications_dedupe_key ON user_notifications(dedupe_key)")
                )
        async with database.engine.connect() as conn:
            level = await conn.scalar(text("SELECT max(version) FROM _migrations"))
            assert await conn.scalar(text("SELECT count(*) FROM _migrations WHERE version=228")) == 1
            redundant = await conn.scalar(
                text(
                    "SELECT count(*) FROM sqlite_master WHERE type='index' AND name='uq_user_notifications_dedupe_key'"
                )
            )
            if case != "prepare_head_v061":
                assert redundant == int(case.endswith("affected"))
        await database.engine.dispose()
        return {"source_level": level, "has_b_marker": True, "dialect": "sqlite", "redundant_unique": redundant}
    if case == "repair":
        await exercise_repair(database, repair)
        await exercise_failed_start(database)
    if case == "deletes":
        await exercise_deletes(database)
    await exercise_roundtrips(database)
    async with database.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM _migrations WHERE version=228")) == 1
        before = await capture(conn)
    writes = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().split(None, 1)[0].upper() in {"UPDATE", "INSERT", "DELETE", "ALTER", "CREATE", "DROP"}:
            writes.append(statement)

    event.listen(database.engine.sync_engine, "before_cursor_execute", record)
    try:
        # Explicit DEBUG-style replay plus import's second guarded FTS call.
        async with database.engine.begin() as conn:
            await repair.replay(conn)
            from backend.app.migrations._pg_schema_228_fts import ensure_archive_fts

            await ensure_archive_fts(conn)
        from backend.app.migrations import _run_pending

        previous_debug = settings.debug
        settings.debug = True
        try:
            await _run_pending(database.engine, database.async_session)
        finally:
            settings.debug = previous_debug
        assert not writes, writes
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    async with database.engine.connect() as conn:
        assert before == await capture(conn)
    await database.engine.dispose()
    return before


async def exercise_repair(database, repair):
    """All changes roll back, including FTS failures and physical DELETE links."""
    from backend.app.migrations._pg_schema_228_fts import ensure_archive_fts
    from backend.app.models.camera import Camera
    from backend.app.models.label_device import LabelCassette, LabelDevice, LabelJob
    from backend.app.models.label_template import LabelSheet, LabelTemplate
    from backend.app.models.library import LibraryFolder
    from backend.app.models.library_scan import LibraryScanJob
    from backend.app.models.spool import Spool

    async with database.engine.connect() as conn:
        outer = await conn.begin()
        try:
            await conn.execute(text("CREATE TEMP TABLE old_baselines (id INTEGER, value REAL) ON COMMIT DROP"))
            await conn.execute(text("ALTER TABLE spool ALTER COLUMN weight_used_baseline TYPE REAL"))
            from sqlalchemy.ext.asyncio import AsyncSession

            session = AsyncSession(bind=conn, expire_on_commit=False)
            values = [0.1, 1234.567, 12345.67, 37.839203, 999.9999, 0, float("nan"), float("inf"), float("-inf")]
            for value in values:
                spool = Spool(material="PLA", weight_used_baseline=value)
                session.add(spool)
                await session.flush()
            await conn.execute(text("INSERT INTO old_baselines SELECT id,weight_used_baseline FROM spool"))
            await session.close()
            # Exercise the missing server-default path, including columns
            # already NOT NULL. Unknown timestamps must never get now().
            default_targets = [r for r in repair.REPAIRS if r["object"].endswith(".server_default")]
            for row in default_targets:
                table, column, _ = row["object"].split(".")
                await conn.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP DEFAULT'))
            await conn.execute(text("SET LOCAL extra_float_digits=0"))
            await repair.upgrade(conn)
            assert await conn.scalar(text("SHOW extra_float_digits")) == "0"
            assert await conn.scalar(
                text("""
                SELECT bool_and(s.weight_used_baseline::real=o.value) FROM spool s JOIN old_baselines o USING(id)
            """)
            )
            assert await conn.scalar(text("SELECT count(*) FROM spool")) == await conn.scalar(
                text("SELECT count(*) FROM old_baselines")
            )
            await conn.execute(text("SET LOCAL extra_float_digits=1"))
            assert await conn.scalar(
                text("SELECT weight_used_baseline=0.1::double precision FROM spool ORDER BY id LIMIT 1")
            )

            session = AsyncSession(bind=conn, expire_on_commit=False)
            folder = LibraryFolder(name="B library")
            device = LabelDevice(installation_id="B job-device")
            session.add_all([folder, device])
            await session.flush()
            objects = [
                Camera(name="B camera", camera_type="mjpeg", url="http://127.0.0.1:9/"),
                LabelCassette(barcode="B cassette", width_mm=40, height_mm=30),
                LabelDevice(installation_id="B device"),
                LabelJob(device_id=device.id, width_mm=40, height_mm=30, image_png=b"synthetic"),
                LabelSheet(name="B sheet", cell_width_mm=40, cell_height_mm=30, cols=2, rows=3),
                LabelTemplate(name="B template", width_mm=40, height_mm=30, elements=[]),
                LibraryScanJob(folder_id=folder.id, status="queued", created_by=None),
            ]
            session.add_all(objects)
            await session.flush()
            for obj in objects:
                await session.refresh(obj)
                assert obj.created_at is not None
            assert objects[0].updated_at is not None
            await session.close()

            await exercise_catalogue_and_orphans(conn, repair)

            # Disabled trigger must repair stale *non-NULL* vectors.
            from backend.app.models.archive import PrintArchive

            session = AsyncSession(bind=conn, expire_on_commit=False)
            session.add(
                PrintArchive(
                    filename="B.3mf",
                    file_path="",
                    file_size=0,
                    status="completed",
                    recovery_identity="B-search-identity",
                    print_name="before",
                )
            )
            await session.flush()
            await session.close()
            await conn.execute(text("ALTER TABLE print_archives DISABLE TRIGGER archive_search_vector_trigger"))
            await conn.execute(text("UPDATE print_archives SET print_name='after'"))
            assert not await conn.scalar(
                text(
                    "SELECT search_vector @@ to_tsquery('simple','after') FROM print_archives WHERE recovery_identity='B-search-identity'"
                )
            )
            await ensure_archive_fts(conn)
            assert await conn.scalar(
                text(
                    "SELECT search_vector @@ to_tsquery('simple','after') FROM print_archives WHERE recovery_identity='B-search-identity'"
                )
            )
            await conn.execute(
                text("UPDATE pg_index SET indisvalid=false WHERE indexrelid='idx_archives_search_vector'::regclass")
            )
            await ensure_archive_fts(conn)
            assert await conn.scalar(
                text("SELECT indisvalid FROM pg_index WHERE indexrelid='idx_archives_search_vector'::regclass")
            )

            # A real oversized tsvector fails closed. Roll the attempted repair
            # back before returning, rather than swallowing its DB error.
            point = await conn.begin_nested()
            await conn.execute(text("ALTER TABLE print_archives DISABLE TRIGGER archive_search_vector_trigger"))
            await conn.execute(
                text("""
                UPDATE print_archives SET notes=(SELECT string_agg(md5(i::text),' ') FROM generate_series(1,100000) i)
                WHERE recovery_identity='B-search-identity'
            """)
            )
            try:
                await ensure_archive_fts(conn)
            except DBAPIError as exc:
                assert getattr(exc.orig, "sqlstate", None) == "54000" and "tsvector" in str(exc).lower(), str(exc)
            else:
                raise AssertionError("oversized FTS unexpectedly succeeded")
            await point.rollback()
        finally:
            await outer.rollback()


async def exercise_catalogue_and_orphans(conn, repair):
    from backend.app.migrations._pg_schema_228_fts import ensure_archive_fts
    from backend.tests.integration.pg_schema_oracle import unexpected

    reference = await capture(conn)
    # SQLAlchemy reflects invalid plain indexes differently from FTS's raw
    # catalogue helper. Exercise both flags on a normal frozen repair target.
    plain = next(r for r in repair.REPAIRS if r["object"] == "api_keys.indexes.ix_api_keys_owner_identity")
    for flag in ("indisvalid", "indisready"):
        point = await conn.begin_nested()
        await conn.execute(
            text(f"UPDATE pg_index SET {flag}=false WHERE indexrelid='ix_api_keys_owner_identity'::regclass")
        )
        assert unexpected(reference, await capture(conn), [])
        residuals = []
        await repair._repair_index(conn, plain, residuals)
        assert not residuals, residuals
        assert not unexpected(reference, await capture(conn), [])
        await point.rollback()
    for sql in [
        "ALTER TABLE print_archives ENABLE REPLICA TRIGGER archive_search_vector_trigger",
        "UPDATE pg_index SET indisvalid=false WHERE indexrelid='idx_archives_search_vector'::regclass",
        "UPDATE pg_index SET indisready=false WHERE indexrelid='idx_archives_search_vector'::regclass",
    ]:
        point = await conn.begin_nested()
        await conn.execute(text(sql))
        assert unexpected(reference, await capture(conn), []), sql
        await ensure_archive_fts(conn)
        assert not unexpected(reference, await capture(conn), []), sql
        await point.rollback()

    row = next(r for r in repair.REPAIRS if r["object"] == "spool_usage_history.archive_id.ondelete")
    columns = await repair._columns(conn)
    point = await conn.begin_nested()
    await conn.execute(text("ALTER TABLE spool_usage_history DROP CONSTRAINT spool_usage_history_archive_id_fkey"))
    await conn.execute(
        text(
            "ALTER TABLE spool_usage_history ADD CONSTRAINT spool_usage_history_archive_id_fkey FOREIGN KEY(archive_id) REFERENCES print_archives(id) ON DELETE SET NULL NOT VALID"
        )
    )
    assert unexpected(reference, await capture(conn), [])
    residuals = []
    await repair._repair_fk(conn, row, columns, residuals)
    assert not residuals
    assert not unexpected(reference, await capture(conn), [])
    await point.rollback()

    point = await conn.begin_nested()
    await conn.execute(text("ALTER TABLE spool_usage_history DROP CONSTRAINT spool_usage_history_archive_id_fkey"))
    await conn.execute(
        text(
            "INSERT INTO spool_usage_history(spool_id,archive_id,weight_used,percent_used,status) SELECT min(id),999999,12.5,3,'completed' FROM spool"
        )
    )
    residuals = []
    await repair._repair_fk(conn, row, columns, residuals)
    assert not residuals
    assert (
        await conn.execute(text("SELECT archive_id,weight_used,percent_used,status FROM spool_usage_history"))
    ).one() == (None, 12.5, 3, "completed")
    await point.rollback()

    point = await conn.begin_nested()
    row = next(
        r
        for r in repair.REPAIRS
        if r["object"] == "print_options_preferences.foreign_key.print_options_preferences_user_id_fkey"
    )
    await conn.execute(
        text("ALTER TABLE print_options_preferences DROP CONSTRAINT print_options_preferences_user_id_fkey")
    )
    await conn.execute(
        text(
            "INSERT INTO print_options_preferences(user_id,printer_model,options,updated_at) VALUES(999999,'A1 Mini','{}'::json,now())"
        )
    )
    residuals = []
    await repair._repair_fk(conn, row, columns, residuals)
    assert residuals == [(row["object"], "native_orphan_before_import_conform", 1)]
    assert await conn.scalar(text("SELECT user_id FROM print_options_preferences")) == 999999
    await point.rollback()


async def exercise_failed_start(database):
    from backend.app.models.archive import PrintArchive

    async with database.async_session() as session:
        item = PrintArchive(filename="oversize.3mf", file_path="", file_size=0, status="completed")
        session.add(item)
        await session.flush()
        archive_id = item.id
        await session.commit()
    async with database.engine.begin() as conn:
        await conn.execute(text("ALTER TABLE print_archives DISABLE TRIGGER archive_search_vector_trigger"))
        await conn.execute(
            text(
                "UPDATE print_archives SET notes=(SELECT string_agg(md5(i::text),' ') FROM generate_series(1,100000) i) WHERE id=:id"
            ),
            {"id": archive_id},
        )
        await conn.execute(text("ALTER TABLE cameras ALTER COLUMN created_at DROP DEFAULT"))
        await conn.execute(text("DELETE FROM _migrations WHERE version=228"))
    try:
        await database.init_db()
    except DBAPIError as exc:
        assert getattr(exc.orig, "sqlstate", None) == "54000"
    else:
        raise AssertionError("FTS failure did not stop startup")
    async with database.engine.begin() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM _migrations WHERE version=228")) == 0
        assert (
            await conn.scalar(
                text(
                    "SELECT column_default FROM information_schema.columns WHERE table_schema='public' AND table_name='cameras' AND column_name='created_at'"
                )
            )
            is None
        )
        assert (
            await conn.scalar(text("SELECT length(notes) FROM print_archives WHERE id=:id"), {"id": archive_id})
            > 1000000
        )
        await conn.execute(text("DELETE FROM print_archives WHERE id=:id"), {"id": archive_id})
    await database.init_db()
    async with database.engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM _migrations WHERE version=228")) == 1


async def exercise_deletes(database):
    from datetime import datetime

    from sqlalchemy import select

    from backend.app.api.routes.users import delete_user
    from backend.app.models.archive import PrintArchive
    from backend.app.models.spool import Spool
    from backend.app.models.spool_usage_history import SpoolUsageHistory
    from backend.app.models.user import User
    from backend.app.services.archive_purge import archive_purge_service
    from backend.tests.auth_helpers import mint_test_authority

    async with database.async_session() as session:
        owner = User(username="B owner")
        admin = User(username="B admin", role="admin")
        spool = Spool(material="PLA")
        session.add_all([owner, admin, spool])
        await session.flush()
        owner_id = owner.id
        archives = []
        history = []
        for name in ["raw", "trash", "owner"]:
            archive = PrintArchive(
                filename=name + ".3mf",
                file_path="",
                file_size=0,
                status="completed",
                created_by_id=owner.id if name == "owner" else None,
                deleted_at=datetime(2026, 1, 1) if name == "trash" else None,
            )
            session.add(archive)
            await session.flush()
            usage = SpoolUsageHistory(
                spool_id=spool.id,
                archive_id=archive.id,
                weight_used=12.5,
                cost=0.75,
                status="completed",
                print_name=name,
            )
            session.add(usage)
            await session.flush()
            archives.append(archive.id)
            history.append(usage.id)
        await session.commit()
        # Actual FK action, and rollback must restore the very same link.
        await session.execute(text("DELETE FROM print_archives WHERE id=:id"), {"id": archives[0]})
        assert (
            await session.scalar(select(SpoolUsageHistory.archive_id).where(SpoolUsageHistory.id == history[0])) is None
        )
        await session.rollback()
        assert (
            await session.scalar(select(SpoolUsageHistory.archive_id).where(SpoolUsageHistory.id == history[0]))
            == archives[0]
        )
        await session.execute(text("DELETE FROM print_archives WHERE id=:id"), {"id": archives[0]})
        await session.commit()
        assert await archive_purge_service.hard_delete_now(archives[1])
        await session.refresh(admin)
        admin._auth_capture = await mint_test_authority(session, username=admin.username)
        await delete_user(user_id=owner_id, delete_items=True, _admin=admin, current_user=admin, db=session)
        rows = (
            await session.execute(
                select(
                    SpoolUsageHistory.id,
                    SpoolUsageHistory.archive_id,
                    SpoolUsageHistory.weight_used,
                    SpoolUsageHistory.cost,
                    SpoolUsageHistory.status,
                )
                .where(SpoolUsageHistory.id.in_(history))
                .order_by(SpoolUsageHistory.id)
            )
        ).all()
        assert [tuple(r) for r in rows] == [(i, None, 12.5, 0.75, "completed") for i in history]


async def exercise_roundtrips(database):
    from sqlalchemy.ext.asyncio import AsyncSession

    from backend.app.models.active_print_spoolman import ActivePrintSpoolman
    from backend.app.models.archive import PrintArchive
    from backend.app.models.printer import Printer
    from backend.app.models.smart_plug import SmartPlug
    from backend.app.models.spool import Spool

    async with database.engine.connect() as conn:
        outer = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        try:
            maximum = await conn.scalar(text("SELECT coalesce(max(id),0) FROM spool"))
            spool = Spool(material="PLA")
            printer = Printer(
                name="B roundtrip", serial_number="B-ROUNDTRIP", ip_address="127.0.0.1", access_code="synthetic"
            )
            archive = PrintArchive(filename="roundtrip.3mf", file_path="", file_size=0, status="completed")
            plug = SmartPlug(
                name="B roundtrip", plug_type="rest", controls_printer_power=False, rest_energy_total_multiplier=0.001
            )
            session.add_all([spool, printer, archive, plug])
            await session.flush()
            assert spool.id > maximum
            value = {"0-1": {"remain": 0, "tray_uuid": "synthetic", "label": "Котушка", "extra": None}}
            context = ActivePrintSpoolman(
                printer_id=printer.id, archive_id=archive.id, filament_usage=[], ams_trays={}, tray_remain_start=value
            )
            session.add(context)
            await session.flush()
            await session.refresh(context)
            await session.refresh(plug)
            assert context.tray_remain_start == value
            assert plug.rest_energy_total_multiplier == 0.001 and plug.controls_printer_power is False
        finally:
            await session.close()
            await outer.rollback()
