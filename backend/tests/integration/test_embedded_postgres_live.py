"""The bundled PostgreSQL really starts, answers and stops.

Uses the embedded-postgres wheel from the environment, a temporary data
directory and a free port; nothing touches the developer's DATA_DIR. Skipped
when the wheel is not installed.
"""

import asyncio
import os
import socket
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.core.database import Base, import_all_models
from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer
from backend.app.services import embedded_postgres as ep
from backend.app.services.archive import ArchiveService

pytest.importorskip("embedded_postgres")

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REPO_ROOT = Path(__file__).resolve().parents[3]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def live_settings(tmp_path, monkeypatch):
    # PostgreSQL refuses to initdb as root - its rule, not ours - and the
    # integration suite is also run inside a container that has no other
    # user. The bundled server is a native-install option (Linux service,
    # the Windows installer); Docker gets the sidecar or an external server,
    # and docker-install.sh no longer offers it. So this is nothing to fail
    # over there - but it stays a hard failure everywhere it CAN run.
    if getattr(os, "geteuid", lambda: -1)() == 0:
        pytest.skip("running as root: initdb refuses, and the bundled server is not a container option")
    pgdata = tmp_path / "postgres" / ep.bundled_major()
    password_file = tmp_path / "postgres" / "password"
    password_file.parent.mkdir(parents=True)
    password_file.write_text("live-test-password\n", encoding="utf-8")
    monkeypatch.setattr(settings, "embedded_postgres", True)
    monkeypatch.setattr(settings, "embedded_pg_data_dir", pgdata)
    monkeypatch.setattr(settings, "embedded_pg_password_file", password_file)
    monkeypatch.setattr(settings, "embedded_pg_port", _free_port())
    monkeypatch.setattr(settings, "embedded_pg_max_connections", 120)
    return pgdata


async def test_initdb_start_query_stop(live_settings):
    pgdata = live_settings
    try:
        await ep.start()
        assert (pgdata / "PG_VERSION").read_text(encoding="utf-8").strip() == ep.bundled_major()
        assert await ep.is_running()
        assert ep._running_port() == settings.embedded_pg_port

        version = await ep._psql("select version()", database=ep.PG_DATABASE)
        assert f"PostgreSQL {ep.bundled_major()}." in version
        assert await ep._psql("select extname from pg_extension where extname = 'pg_stat_statements'") == ""
        assert (
            await ep._psql(
                "select extname from pg_extension where extname = 'pg_stat_statements'", database=ep.PG_DATABASE
            )
            == "pg_stat_statements"
        )
        # Unicode-aware case mapping from the builtin C.UTF-8 provider. Spelled
        # with SQL Unicode escapes: a Cyrillic literal on psql's command line
        # goes through the Windows console code page and arrives as "????".
        assert (
            await ep._psql(
                r"select lower(U&'\041F\0415\0422\0413') = U&'\043F\0435\0442\0433'", database=ep.PG_DATABASE
            )
            == "t"
        )

        # a second start() finds the running server and reuses it
        await ep.start()
        assert await ep.is_running()
    finally:
        await ep.stop()
    assert not await ep.is_running()


async def test_start_refuses_a_data_directory_of_another_major(live_settings, monkeypatch):
    pgdata = live_settings
    pgdata.mkdir(parents=True)
    (pgdata / "PG_VERSION").write_text("9\n", encoding="utf-8")
    with pytest.raises(ep.EmbeddedPostgresError):
        await ep.start()


async def test_archive_attach_rollback_and_retry_use_the_native_windows_postgres(live_settings, tmp_path, monkeypatch):
    """A smoke start is insufficient: exercise the actual archive transaction."""
    await ep.start()
    password = quote(ep._password(), safe="")
    url = f"postgresql+asyncpg://{ep.PG_USER}:{password}@{ep.PG_HOST}:{settings.embedded_pg_port}/{ep.PG_DATABASE}"
    engine = create_async_engine(url)
    try:
        import_all_models()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        monkeypatch.setattr(settings, "base_dir", tmp_path)
        monkeypatch.setattr(settings, "archive_dir", tmp_path / "archive")
        src = tmp_path / "recovered.gcode.3mf"
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("Metadata/slice_info.config", "<config><plate /></config>")

        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db:
            printer = Printer(
                name="embedded-attach",
                ip_address="127.0.0.1",
                access_code="00000000",
                serial_number="EMBEDATTACH1",
                model="P1S",
            )
            db.add(printer)
            await db.commit()
            printer_id = printer.id
            archive = PrintArchive(
                printer_id=printer_id,
                filename="recovered.gcode.3mf",
                file_path="",
                file_size=0,
                print_name="Recovered",
                status="printing",
            )
            db.add(archive)
            await db.commit()
            archive_id = archive.id
            service = ArchiveService(db)

            assert not await service.attach_3mf_to_archive(archive_id, src, "../escape.gcode.3mf")
            assert await service.mark_3mf_unavailable(archive_id)
            assert await service.attach_3mf_to_archive(archive_id, src, "recovered.gcode.3mf")
            recovered = await db.get(PrintArchive, archive_id)
            assert recovered is not None and recovered.file_path
            assert (recovered.extra_data or {}).get("no_3mf_available") is None

            concurrent = PrintArchive(
                printer_id=printer_id,
                filename="concurrent.gcode.3mf",
                file_path="",
                file_size=0,
                print_name="Concurrent",
                status="printing",
            )
            db.add(concurrent)
            await db.commit()
            concurrent_id = concurrent.id

        async def attach_once():
            async with maker() as concurrent_db:
                return await ArchiveService(concurrent_db).attach_3mf_to_archive(
                    concurrent_id, src, "concurrent.gcode.3mf"
                )

        assert await asyncio.gather(attach_once(), attach_once()) == [True, True]
        async with maker() as db:
            concurrent = await db.get(PrintArchive, concurrent_id)
            assert concurrent is not None and concurrent.file_path
        # The first call reused the existing chain's file; the second call must
        # not cut another directory or copy over it after waiting on the guard.
        assert len(list((tmp_path / "archive").rglob("*.3mf"))) == 1
    finally:
        await engine.dispose()
        await ep.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="a console's Ctrl+C is a Windows matter; pg_ctl setsid()s on Unix")
def test_a_console_ctrl_c_never_reaches_the_server(tmp_path):
    """The operator's Ctrl+C in the uvicorn window must stop uvicorn only: the
    server is stopped by the lifespan afterwards, with a clean checkpoint. A
    server sharing that console shut itself down at the keypress instead,
    racing its own checkpointer ("abnormal database system shutdown"). The
    probe runs on a fresh hidden console and sends that console's Ctrl+C to
    every process attached to it."""
    probe = Path(__file__).with_name("console_ctrl_c_probe.py")
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path),
        "DATABASE_URL": "embedded",
        "EMBEDDED_PG_PORT": str(_free_port()),
        "PYTHONPATH": str(REPO_ROOT),
    }
    try:
        result = subprocess.run(
            [sys.executable, str(probe)],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
            timeout=180,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    finally:
        # never leave a server behind on a failed run
        pgdata = tmp_path / "postgres" / ep.bundled_major()
        if (pgdata / "postmaster.pid").exists():
            subprocess.run(
                [str(ep._bin("pg_ctl")), "-D", str(pgdata), "-m", "fast", "-w", "stop"],
                capture_output=True,
                timeout=90,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
    report = result.stdout + result.stderr
    assert "PROBE_GOT_CTRL_C" in report, report  # the keypress really was delivered
    assert "ALIVE_AFTER_CTRL_C True" in report, report
    assert "STOPPED_CLEANLY True" in report, report
    log = (tmp_path / "postgres" / "postgres.log").read_text(encoding="utf-8", errors="replace")
    assert "abnormal database system shutdown" not in log
    assert "database system is shut down" in log
