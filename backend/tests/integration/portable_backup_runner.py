"""Real init/ZIP/restore in a fresh process, with DATA_DIR pointing only at test files."""

import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from sqlalchemy import select, text


async def seed():
    from backend.app.core.config import settings
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.models.notification import NotificationProvider
    from backend.app.models.oidc_provider import OIDCProvider
    from backend.app.models.printer import Printer
    from backend.app.models.project import Project

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
        await db.flush()
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

    async with async_session() as db:
        archive = await db.get(PrintArchive, 51)
        provider = await db.get(NotificationProvider, 61)
        oidc = await db.get(OIDCProvider, 71)
        await db.refresh(oidc, ["icon_data"])
        history = (
            await db.execute(text("SELECT id, version, name, applied_at FROM _migrations ORDER BY version"))
        ).all()
        return {
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


async def main(mode, path):
    from fastapi import UploadFile

    from backend.app.api.routes.settings import create_backup_zip, restore_backup
    from backend.app.core import database
    from backend.app.core.config import settings
    from backend.app.services.backup_files import directories

    try:
        # Export and restore on different external archive roots.
        settings.archive_dir = settings.base_dir.parent / (settings.base_dir.name + "-archives")
        await database.init_db()
        if mode == "export":
            await seed()
            result = await report()
            output, _ = await create_backup_zip(path)
            result["zip"] = str(output)
            return result
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
        return await exercise_restored_database()
    finally:
        await database.engine.dispose()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(sys.argv[1], Path(sys.argv[2])))))
