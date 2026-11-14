"""Fresh, disposable interpreter for process-resource acceptance. No conftest."""

import asyncio
import gc
import json
import os
import resource
import socket
import subprocess
import sys
import tempfile
import time
import weakref
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path


def startup():
    original = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(1024, original[1]), original[1]))
    before = resource.getrlimit(resource.RLIMIT_NOFILE)
    import backend.app.main as main

    marks = []
    prepare = main.process_gc.prepare_startup_heap
    raise_limit = main.process_resources.raise_nofile_limit

    def heap():
        marks.append("heap")
        prepare()

    def limits():
        assert marks == ["heap"] and gc.get_freeze_count() > 0
        marks.append("limits")
        raise_limit()

    main.process_gc.prepare_startup_heap = heap
    main.process_resources.raise_nofile_limit = limits
    inherited = None

    class BoundaryReached(Exception):
        pass

    @asynccontextmanager
    async def first_child(_app):
        nonlocal inherited
        assert marks == ["heap", "limits"]
        marks.append("child")
        inherited = json.loads(
            subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "import json,resource;print(json.dumps(resource.getrlimit(resource.RLIMIT_NOFILE)))",
                ]
            )
        )
        raise BoundaryReached
        yield  # pragma: no cover - stop before unrelated runtimes or hardware

    main._application_lifespan = first_child

    async def enter():
        try:
            async with main.lifespan(main.app):
                raise AssertionError("did not stop at child boundary")
        except BoundaryReached:
            pass

    asyncio.run(enter())
    after = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert after == (min(65536, before[1]), before[1])
    assert inherited == list(after) and marks == ["heap", "limits", "child"]
    return {
        "uid": os.getuid(),
        "original": original,
        "before": before,
        "after": after,
        "child": inherited,
        "marks": marks,
        "farm_standin": True,
    }


def fd_snapshot():
    import builtins

    from backend.app.core import process_resources as resources

    import_calls = []
    original_import = builtins.__import__

    def tracked(*args, **kwargs):
        import_calls.append(args[0])
        return original_import(*args, **kwargs)

    builtins.__import__ = tracked
    try:
        quiet = resources.collect_fd_info()
    finally:
        builtins.__import__ = original_import
    assert not import_calls and quiet["count"] is not None
    with tempfile.TemporaryDirectory(prefix="G02_SECRET_PATH_") as directory, ExitStack() as files:
        db = files.enter_context(open(Path(directory) / "secret.db", "w"))
        files.enter_context(open(Path(directory) / "secret.db-wal", "w"))
        files.enter_context(open(Path(directory) / "secret.db-shm", "w"))
        os.unlink(db.name)
        pipe = os.pipe()
        sockets = socket.socketpair()
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            before_child = resources.collect_fd_info()
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import os; files=[open('/dev/null') for _ in range(40)]; print('ready',flush=True); input()",
                    "G02_ARG_SECRET",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert child.stdout.readline().strip() == "ready"
                info = resources.collect_fd_info()
                assert info["count"] - before_child["count"] < 10  # not the child's 40 descriptors
                assert info["kinds"]["db_file"] >= 1 and info["kinds"]["wal_file"] >= 1
                assert info["kinds"]["shm_file"] >= 1 and info["kinds"]["socket"] >= 2
                assert info["kinds"]["pipe"] >= 2 and info["kinds"]["other_regular"] >= 1
                assert "SECRET" not in json.dumps(info) and directory not in json.dumps(info)
            finally:
                child.communicate("\n", timeout=5)
        finally:
            for fd in pipe:
                os.close(fd)
            for sock in sockets:
                sock.close()
            os.close(directory_fd)
    # Exact exhaustion in this child only. Limits remain readable without a new FD.
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
    fillers = []
    try:
        while True:
            fillers.append(os.open("/dev/null", os.O_RDONLY))
    except OSError:
        exhausted = resources.collect_fd_info()
    finally:
        for fd in fillers:
            os.close(fd)
    assert exhausted["count"] is None and exhausted["kinds"] is None
    assert exhausted["soft_limit"] == 64 and exhausted["hard_limit"] == hard
    return {"quiet": quiet, "fixture": info, "exhausted": exhausted, "first_call_imports": import_calls}


async def sticky_recovery():
    import paho.mqtt.client as mqtt
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app.core import database
    from backend.app.models.printer import Printer
    from backend.app.services import connection_watchdog as watchdog
    from backend.app.services.bambu_mqtt import BambuMQTTClient
    from backend.app.services.printer_manager import PrinterManager

    assert database.is_sqlite(), "probe may only open its disposable SQLite database"
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(65536, hard), hard))
    assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] > 1200
    database.import_all_models()
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    factory = async_sessionmaker(database.engine, expire_on_commit=False)
    async with factory() as session:
        printer = Printer(
            name="FD recovery probe",
            serial_number="G02F1PROBE",
            ip_address="127.0.0.1",
            access_code="probe-only",
            model="A1",
            is_active=True,
        )
        session.add(printer)
        await session.commit()
    writers, tasks = set(), set()
    publications = []

    async def broker(reader, writer):
        writers.add(writer)
        tasks.add(asyncio.current_task())
        try:
            while True:
                header = (await reader.readexactly(1))[0]
                length, shift = 0, 0
                while True:
                    byte = (await reader.readexactly(1))[0]
                    length += (byte & 127) << shift
                    if not byte & 128:
                        break
                    shift += 7
                body = await reader.readexactly(length)
                kind = header >> 4
                if kind == 1:
                    writer.write(b"\x20\x02\x00\x00")
                elif kind == 8:
                    writer.write(b"\x90\x03" + body[:2] + b"\x01")
                elif kind == 3:
                    publications.append(body)
                elif kind == 12:
                    writer.write(b"\xd0\x00")
                elif kind == 14:
                    break
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            writers.discard(writer)
            tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(broker, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    BambuMQTTClient.MQTT_PORT = port
    watchdog.PORT_MQTT = port
    manager = PrinterManager()
    watchdog.printer_manager = manager
    watchdog.async_session = factory
    # Plain loopback broker isolates FD/owner behaviour from TLS, not a TLS test.
    original_tls = mqtt.Client.tls_set_context
    mqtt.Client.tls_set_context = lambda *_: None
    fillers = []
    try:
        while not fillers or fillers[-1] < 1100:
            fillers.append(os.open("/dev/null", os.O_RDONLY))
        assert not await manager.connect_printer(printer)
        old_wrapper = manager.get_client(printer.id)
        old = old_wrapper._client
        old_fds = [(s.fileno(), os.fstat(s.fileno()).st_ino) for s in (old._sockpairR, old._sockpairW)]
        assert all(fd >= 1024 for fd, _ in old_fds)
        old_ref, wrapper_ref = weakref.ref(old), weakref.ref(old_wrapper)
        old.publish("g02/queued-old", b"G02_MUST_NOT_REPLAY", qos=1)
        old_wrapper._was_running = True
        old_wrapper._previous_gcode_state = "RUNNING"
        for fd in fillers:
            os.close(fd)
        fillers.clear()
        await asyncio.sleep(0.2)
        assert not old_wrapper.state.connected and old_wrapper._last_message_time == 0
        old_wrapper._connect_started_at = time.time() - watchdog.SILENCE_GRACE_SECONDS - 1
        assert await watchdog.should_rebuild(printer, old_wrapper)
        del old, old_wrapper
        assert await watchdog.sweep_once() == 1
        fresh = manager.get_client(printer.id)
        assert fresh.state.connected and fresh._was_running
        assert time.time() - fresh._connect_started_at < 10
        for _ in range(100):
            gc.collect()
            if old_ref() is None and wrapper_ref() is None:
                break
            await asyncio.sleep(0.03)
        assert old_ref() is None and wrapper_ref() is None, "old client retained after rebuild"
        for fd, inode in old_fds:
            try:
                assert os.fstat(fd).st_ino != inode, "old high socketpair descriptor still alive"
            except OSError:
                pass
        assert not any(b"G02_MUST_NOT_REPLAY" in body for body in publications)
        return {
            "old_socketpair_fds": [fd for fd, _ in old_fds],
            "old_client_collected": True,
            "old_wrapper_collected": True,
            "old_socketpair_released": True,
            "new_client_connected": True,
            "lifecycle_carried": True,
            "old_queue_not_replayed": True,
            "plain_loopback_broker": True,
        }
    finally:
        for fd in fillers:
            os.close(fd)
        manager.disconnect_all()
        await asyncio.sleep(0.1)
        mqtt.Client.tls_set_context = original_tls
        server.close()
        for writer in list(writers):
            writer.close()
        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)
        await server.wait_closed()
        await database.engine.dispose()


if __name__ == "__main__":
    mode = sys.argv[1]
    result = startup() if mode == "startup" else fd_snapshot() if mode == "fd" else asyncio.run(sticky_recovery())
    print("RESULT=" + json.dumps(result), flush=True)
