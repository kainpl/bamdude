"""Real init/ZIP/restore in a fresh process, with DATA_DIR pointing only at test files."""

import asyncio
import json
import sqlite3
import sys
import zipfile
from contextlib import closing
from pathlib import Path

from sqlalchemy import select, text


async def seed():
    from backend.app.core.config import settings
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.models.notification import NotificationProvider
    from backend.app.models.oidc_provider import OIDCProvider
    from backend.app.models.order_working_plan import OrderPlanReceipt, OrderPlanSubmission, OrderWorkingPlan
    from backend.app.models.printer import Printer
    from backend.app.models.product import Product
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine

    async with async_session() as db:
        db.add(
            Printer(
                id=41,
                name="backup-printer",
                ip_address="127.0.0.2",
                access_code="00000000",
                serial_number="BACKUP41",
                model="X1C",
            )
        )
        db.add(Project(id=31, name="Замовлення"))
        db.add(Product(id=21, name="Backup product"))
        await db.flush()
        db.add(
            ProjectLine(
                id=11, project_id=31, product_id=21, quantity=4, plan_identity="8c05cce8-6ad9-4682-9b37-1474cbe739c9"
            )
        )
        db.add(
            OrderWorkingPlan(
                id=31,
                entries=[{"entry_id": "retained", "count": 2}],
                revision=7,
                saved_entries=[{"entry_id": "retained", "count": 4}],
                saved_revision=2,
                saved_facts={"workload": {"total_seconds": 7200}},
                saved_epoch=1,
                epoch=2,
                active_operation="backup-operation",
            )
        )
        await db.flush()
        db.add(
            OrderPlanSubmission(
                id="backup-operation", plan_id=31, actor="user:1", request_digest="digest", request={"parts": []}
            )
        )
        await db.flush()
        db.add(
            OrderPlanReceipt(
                id="backup-operation:committed",
                operation_id="backup-operation",
                entry_id="committed",
                count=2,
                outcome={"queue_item_ids": [100, 101]},
            )
        )
        db.add(
            PrintArchive(
                id=51,
                printer_id=41,
                project_id=31,
                filename="test.3mf",
                file_path="archive/test.3mf",
                file_size=7,
                print_name="snapshotneedle",
                source_content_hash="a" * 64,
                extra_data={"nested": [False, "тест", 0, None]},
            )
        )
        db.add(
            NotificationProvider(
                id=61,
                name="backup-provider",
                provider_type="ntfy",
                config='{"url": "test"}',
                printer_ids=[41],
                subscribed_events=["on_print_complete"],
            )
        )
        provider = OIDCProvider(
            id=71,
            name="backup-oidc",
            issuer_url="https://example.invalid",
            client_id="test",
            icon_data=b"\x00\xff\x80icon",
            icon_content_type="image/png",
            icon_etag="test-etag",
        )
        provider.client_secret = "synthetic-secret"
        db.add(provider)
        await db.commit()
    archive = settings.archive_dir
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "test.3mf").write_bytes(b"fixture")
    from backend.app.services.backup_files import directories

    for name, directory in directories(settings).items():
        (directory / "empty").mkdir(parents=True, exist_ok=True)
        if name != "icons":  # An entirely empty directory must clear old files.
            (directory / "fixture.bin").write_bytes(name.encode())
    (settings.base_dir / ".install_id").write_bytes(b"fixture-identity")
    (settings.base_dir / "zigbee").mkdir(exist_ok=True)
    with closing(sqlite3.connect(settings.base_dir / "zigbee/zigbee.db")) as db:
        db.execute("CREATE TABLE network(key TEXT)")
        db.execute("INSERT INTO network VALUES ('fixture-network-key')")
        db.commit()


async def report():
    from backend.app.core.database import async_session, engine
    from backend.app.models.archive import PrintArchive
    from backend.app.models.notification import NotificationProvider
    from backend.app.models.oidc_provider import OIDCProvider
    from backend.app.models.order_working_plan import OrderPlanReceipt, OrderPlanSubmission, OrderWorkingPlan
    from backend.app.models.project_line import ProjectLine

    async with async_session() as db:
        archive = await db.get(PrintArchive, 51)
        line = await db.get(ProjectLine, 11)
        plan = await db.get(OrderWorkingPlan, 31)
        operation = await db.get(OrderPlanSubmission, "backup-operation")
        receipt = await db.get(OrderPlanReceipt, "backup-operation:committed")
        provider = await db.get(NotificationProvider, 61)
        oidc = await db.get(OIDCProvider, 71)
        await db.refresh(oidc, ["icon_data"])
        history = (
            await db.execute(text("SELECT id, version, name, applied_at FROM _migrations ORDER BY version"))
        ).all()
        return {
            "printers": [list(r) for r in (await db.execute(text("SELECT id,name FROM printers ORDER BY id"))).all()],
            "plan_line_identity": line.plan_identity,
            "working_plan": [
                plan.entries,
                plan.revision,
                plan.saved_entries,
                plan.saved_revision,
                plan.saved_facts,
                plan.saved_epoch,
                plan.epoch,
                plan.active_operation,
                operation.status,
                receipt.outcome,
            ],
            "archive": [archive.printer_id, archive.project_id, archive.extra_data, archive.print_name],
            "notification": [provider.printer_ids, provider.subscribed_events],
            "oidc": [oidc.icon_data.hex(), oidc.icon_content_type, oidc.client_secret],
            "history": [[*r[:3], str(r[3])] for r in history],
            "dialect": engine.dialect.name,
        }


async def exercise_restored_database():
    from backend.app.core.database import async_session, engine, init_db
    from backend.app.models.printer import Printer
    from backend.app.models.settings import Settings

    before = await report()
    await init_db()  # Simulate a subsequent restart: migrations must not replay.
    assert await report() == before
    async with async_session() as db:
        printer = Printer(name="after restore", ip_address="127.0.0.3", serial_number="AFTER", access_code="00000000")
        setting = Settings(key="portable-new-setting", value="new")
        db.add_all([printer, setting])
        await db.commit()
        await db.refresh(printer)
        await db.refresh(setting)
        assert printer.id > 41
        assert setting.created_at is not None
        if engine.dialect.name == "postgresql":
            search = "SELECT id FROM print_archives WHERE search_vector @@ plainto_tsquery('simple', :q)"
        else:
            search = "SELECT rowid FROM archive_fts WHERE archive_fts MATCH :q"
        assert (await db.execute(text(search), {"q": "snapshotneedle"})).scalars().all() == [51]
        await db.execute(text("UPDATE print_archives SET print_name='changedneedle' WHERE id=51"))
        await db.commit()
        assert (await db.execute(text(search), {"q": "changedneedle"})).scalars().all() == [51]
        assert (await db.execute(text(search), {"q": "snapshotneedle"})).scalars().all() == []
    return before


_WINDOWS_EXTERNAL = r"C:\Shared\original.gcode.3mf"
_WINDOWS_FOLDER = r"\\workshop\models"
_WINDOWS_MISSING = r"archive\missing\thumbnail.png"


async def seed_windows_paths():
    from backend.app.core.config import settings
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.models.library import LibraryFile, LibraryFolder
    from backend.app.models.printer import Printer
    from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

    model = settings.archive_dir / "2026" / "fixture.gcode.3mf"
    model.parent.mkdir(parents=True)
    write_routing_3mf(model, {1: [{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": 1}]})
    (model.parent / "thumb.png").write_bytes(b"preview fixture")
    library = settings.library_dir / "files" / model.name
    library.parent.mkdir(parents=True)
    library.write_bytes(model.read_bytes())
    (settings.library_dir / "thumbnails").mkdir(exist_ok=True)
    (settings.library_dir / "thumbnails" / "thumb.png").write_bytes(b"library preview")

    async with async_session() as db:
        db.add_all(
            [
                Printer(
                    id=41,
                    name="test printer",
                    ip_address="192.0.2.41",
                    serial_number="PORTABLE41",
                    access_code="00000000",
                    model="X1C",
                ),
                LibraryFolder(id=11, name="Outside", is_external=True, external_path=_WINDOWS_FOLDER),
            ]
        )
        await db.flush()
        db.add_all(
            [
                PrintArchive(
                    id=51,
                    printer_id=41,
                    filename=model.name,
                    file_path=r"archive\2026\fixture.gcode.3mf",
                    thumbnail_path=r"archive\2026\thumb.png",
                    source_3mf_path=_WINDOWS_EXTERNAL,
                    timelapse_path=r"archive\..\library\files\fixture.gcode.3mf",
                    f3d_path=_WINDOWS_MISSING,
                    file_size=model.stat().st_size,
                    plate_index=1,
                    print_name="Windows path fixture",
                ),
                LibraryFile(
                    id=61,
                    filename=model.name,
                    file_path=r"library\files\fixture.gcode.3mf",
                    thumbnail_path=r"library\thumbnails\thumb.png",
                    file_type="gcode",
                    file_size=model.stat().st_size,
                ),
                LibraryFile(
                    id=62,
                    folder_id=11,
                    is_external=True,
                    filename=model.name,
                    file_path=r"library\files\fixture.gcode.3mf",
                    file_type="gcode",
                    file_size=model.stat().st_size,
                ),
            ]
        )
        await db.commit()


async def check_windows_paths():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from fastapi import Request

    from backend.app.api.routes import archives, library
    from backend.app.core.database import async_session, engine
    from backend.app.models.archive import PrintArchive
    from backend.app.models.library import LibraryFile, LibraryFolder
    from backend.app.schemas.archive import ReprintRequest
    from backend.app.services.background_dispatch import background_dispatch
    from backend.app.services.printer_manager import printer_manager

    auth = (None, True)
    async with async_session() as db:
        archive = await db.get(PrintArchive, 51)
        managed = await db.get(LibraryFile, 61)
        external = await db.get(LibraryFile, 62)
        folder = await db.get(LibraryFolder, 11)
        assert archive.file_path == "archive/2026/fixture.gcode.3mf"
        assert archive.thumbnail_path == "archive/2026/thumb.png"
        assert archive.source_3mf_path == _WINDOWS_EXTERNAL
        assert archive.timelapse_path == r"archive\..\library\files\fixture.gcode.3mf"
        assert archive.f3d_path == _WINDOWS_MISSING
        assert managed.file_path == "library/files/fixture.gcode.3mf"
        assert managed.thumbnail_path == "library/thumbnails/thumb.png"
        assert external.file_path == r"library\files\fixture.gcode.3mf"
        assert folder.external_path == _WINDOWS_FOLDER

        preview = await archives.get_thumbnail(51, Request({"type": "http"}), db=db, auth_result=auth)
        assert Path(preview.path).read_bytes() == b"preview fixture"
        archived = await archives.download_archive(51, db=db, auth_result=auth)
        library_copy = await library.download_file(61, db=db, auth_result=auth)
        assert Path(archived.path).read_bytes() == Path(library_copy.path).read_bytes()
        with zipfile.ZipFile(archived.path) as model:
            assert model.read("Metadata/plate_1.gcode").startswith(b"; printer_model")

        dispatch = AsyncMock(return_value={"dispatch_job_id": 9, "dispatch_position": 1})
        with (
            patch.object(printer_manager, "is_connected", return_value=True),
            patch.object(background_dispatch, "dispatch_reprint_archive", dispatch),
        ):
            repeated = await archives.reprint_archive(
                51,
                41,
                ReprintRequest(keep_order=False),
                db,
                auth,
                SimpleNamespace(capture=AsyncMock()),
            )
        assert repeated["status"] == "dispatched"
        assert dispatch.call_args.kwargs["options"]["plate_id"] == 1
    return {"paths": "verified", "dialect": engine.dialect.name}


async def main(mode, path):
    from fastapi import UploadFile

    from backend.app.api.routes.settings import create_backup_zip, restore_backup
    from backend.app.core import database
    from backend.app.core.config import settings
    from backend.app.services.backup_files import directories

    try:
        # Export and restore on different external archive roots.
        if mode not in ("export-windows-paths", "restore-windows-paths"):
            settings.archive_dir = settings.base_dir.parent / (settings.base_dir.name + "-archives")
        await database.init_db()
        if mode == "export-windows-paths":
            await seed_windows_paths()
            output, _ = await create_backup_zip(path)
            return {"zip": str(output)}
        if mode in ("export", "export-r5"):
            await seed()
            if mode == "export-r5":
                async with database.engine.begin() as conn:
                    await conn.execute(text("ALTER TABLE printers ALTER COLUMN name DROP NOT NULL"))
                    await conn.execute(text("UPDATE printers SET name=NULL WHERE id=41"))
            result = await report()
            output, _ = await create_backup_zip(path)
            result["zip"] = str(output)
            return result
        if mode == "cycle":
            from backend.app.core import schema_repair_state

            pending = schema_repair_state.write_pending
            tokens = []

            async def observe_pending(*args, **kwargs):
                debt = await pending(*args, **kwargs)
                tokens.append(debt["id"])
                return debt

            schema_repair_state.write_pending = observe_pending
        for directory in directories(settings).values():
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "old-only.bin").write_bytes(b"old")
        (settings.base_dir / ".install_id").write_bytes(b"old-identity")
        previous_key = (settings.base_dir / ".mfa_encryption_key").read_bytes()
        if mode == "restore-fail":
            from sqlalchemy import event

            before = await report()
            load_failures = []

            def fail_load(conn, cursor, statement, parameters, context, executemany):
                if statement.startswith("INSERT INTO") and '"print_archives"' in statement:
                    load_failures.append(statement)
                    raise RuntimeError("injected real restore load failure")

            event.listen(database.engine.sync_engine, "before_cursor_execute", fail_load)
        # Exercise the route, including request-session closure, ZIP validation,
        # MFA key restoration, reinitialization and pending migrations.
        async with database.async_session() as db:
            await db.execute(text("SELECT id FROM settings LIMIT 1"))
            with path.open("rb") as stream:
                response = await restore_backup(file=UploadFile(file=stream, filename="backup.zip"), db=db, _=None)
            if mode == "restore-fail":
                assert response.status_code == 500
                assert len(load_failures) == 1
                assert await report() == before
                for directory in directories(settings).values():
                    assert (directory / "old-only.bin").read_bytes() == b"old"
                    assert not list(directory.glob(".bamdude-restore-*"))
                assert (settings.base_dir / ".install_id").read_bytes() == b"old-identity"
                assert (settings.base_dir / ".mfa_encryption_key").read_bytes() == previous_key
                return {"rollback": True}
            assert isinstance(response, dict) and response["success"], response
        if mode == "restore-windows-paths":
            return await check_windows_paths()
        assert (settings.archive_dir / "test.3mf").read_bytes() == b"fixture"
        for name, directory in directories(settings).items():
            assert not (directory / "old-only.bin").exists()
            assert (directory / "empty").is_dir()
            assert not list(directory.glob(".bamdude-restore-*"))
            if name != "icons":
                assert (directory / "fixture.bin").read_bytes() == name.encode()
        assert (settings.base_dir / ".install_id").read_bytes() == b"fixture-identity"
        with closing(sqlite3.connect(settings.base_dir / "zigbee/zigbee.db")) as db:
            assert db.execute("SELECT key FROM network").fetchone() == ("fixture-network-key",)
        if mode == "cycle":
            from backend.app.core.schema_conform_repair import verify
            from backend.app.core.schema_repair_state import DEBT, read_record

            result = await report()
            await database.init_db()
            assert await report() == result  # No seed or migration replay.
            async with database.engine.begin() as conn:
                assert await read_record(conn, DEBT) is None
                proofs = await verify(conn, database.Base.metadata)
                summary = [(p["object"], p["reason"], p["count"]) for p in proofs]
                assert summary in ([], [("printers.column.name.nullable", "null_rows", 1)])
            assert len(tokens) == 1
            output, _ = await create_backup_zip(settings.base_dir.parent)
            return {"report": result, "zip": str(output), "restore_id": tokens[0], "residuals": summary}
        return await exercise_restored_database()
    finally:
        await database.engine.dispose()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(sys.argv[1], Path(sys.argv[2])))))
