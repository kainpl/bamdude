"""Helper for test_process_gc_startup — not a test, the test spawns it once per scenario.

A fresh interpreter that never imports ``conftest``: it imports the application the way uvicorn
does and enters the REAL ``main.lifespan`` with the policy as the environment sets it. The farm
behind the lifespan is replaced by a stand-in that opens the real database (``init_db`` on the
temporary DATA_DIR the test hands over) and nothing else — no printers, cameras, telemetry or
runtimes, so a start-up proof never touches the network. What the stand-in and the hooks below
record is what the parent asserts:

* the collector's state before and after importing ``main`` (G-A01);
* the freeze count when the farm guard starts, when the database first connects and when the farm
  starts — all after the boundary (G-A02);
* how many times ``gc.freeze`` ran over several lifespans, a failed start or a restore (G-A03);
* whether a cycle created inside the lifespan is collected afterwards — a runtime object the
  boundary never took (G-A03);
* the outcome and every record the policy logged (G-A04).
"""

import asyncio
import gc
import json
import logging
import sys
import weakref
from contextlib import asynccontextmanager


class _Sentinel:
    pass


def _cycle() -> weakref.ref:
    node = _Sentinel()
    node.link = node
    return weakref.ref(node)


def _collector() -> dict:
    return {"enabled": gc.isenabled(), "threshold": list(gc.get_threshold()), "frozen": gc.get_freeze_count()}


def run(scenario: str) -> dict:
    out: dict = {"scenario": scenario, "before_import": _collector()}
    if scenario == "host_disabled":
        gc.disable()

    import backend.app.main as main
    from backend.app.core import database, process_gc

    out["after_import"] = _collector()
    out["conftest_imported"] = any(name.endswith("conftest") for name in sys.modules)
    if scenario == "host_frozen":
        # After the import: the interpreter's own start-up set is counted at import.
        gc.freeze()
        out["after_import"] = _collector()

    records: list = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append([record.levelname, record.getMessage()])

    policy_logger = logging.getLogger(process_gc.logger.name)
    policy_logger.addHandler(_Capture())
    policy_logger.setLevel(logging.INFO)

    freezes: list = []
    real_freeze = gc.freeze

    def counting_freeze():
        freezes.append(1)
        real_freeze()

    gc.freeze = counting_freeze

    marks: list = []
    guard = main.farm_startup_guard
    real_start = guard.start

    def start(data_dir):
        marks.append(["guard_start", gc.get_freeze_count()])
        return real_start(data_dir)

    guard.start = start

    def on_connect(*_args):
        marks.append(["db_connect", gc.get_freeze_count()])

    from sqlalchemy import event

    event.listen(database.engine.sync_engine, "connect", on_connect)

    sentinels: list = []

    async def runtime_objects():
        """G-A05: what a running server creates and closes — a task, a session, an HTTP client, an
        MQTT client fed a status push, an engine of its own — each a graph with cycles."""
        import httpx
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from backend.app.services.bambu_mqtt import BambuMQTTClient

        task = asyncio.create_task(asyncio.sleep(0))
        await task
        sentinels.append(weakref.ref(task))
        async with database.async_session() as session:
            await session.execute(text("SELECT 1"))
        sentinels.append(weakref.ref(session))
        async with httpx.AsyncClient() as client:
            pass
        sentinels.append(weakref.ref(client))
        mqtt = BambuMQTTClient("192.0.2.1", "GCPROBE00001", "12345678", model="P1S")
        mqtt._process_message({"print": {"command": "push_status", "gcode_state": "IDLE", "mc_percent": 0}})
        sentinels.append(weakref.ref(mqtt))
        engine = create_async_engine("sqlite+aiosqlite:///" + str(database.settings.base_dir) + "/probe.sqlite")
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        await engine.dispose()
        sentinels.append(weakref.ref(engine))

    @asynccontextmanager
    async def farm(app, *, fail=False, restore=False):
        marks.append(["farm_start", gc.get_freeze_count()])
        await database.init_db()
        sentinels.append(_cycle())
        if scenario == "runtime_objects":
            await runtime_objects()
        if fail:
            raise RuntimeError("start-up failed after the boundary")
        if restore:
            await database.close_all_connections()
            await database.reinitialize_database()
            event.listen(database.engine.sync_engine, "connect", on_connect)
            async with database.async_session() as session:
                from sqlalchemy import text

                await session.execute(text("SELECT 1"))
            sentinels.append(_cycle())
        yield

    async def lifespan(**options):
        main._application_lifespan = lambda app: farm(app, **options)
        async with main.lifespan(main.app):
            pass

    async def scenario_body():
        if scenario == "late":
            async with database.engine.connect() as conn:
                from sqlalchemy import text

                await conn.execute(text("SELECT 1"))
        if scenario == "failed_then_again":
            try:
                await lifespan(fail=True)
            except RuntimeError as exc:
                out["first_start_failed"] = str(exc)
            # A real process exits after a failed start; one that starts again in place must
            # release the owner lock first, as the next process would find it free.
            main.farm_startup_guard.finish(clean=False)
            await lifespan()
        elif scenario == "twice":
            await lifespan()
            await lifespan()
        elif scenario == "restore":
            await lifespan(restore=True)
        else:
            await lifespan()

    try:
        asyncio.run(scenario_body())
    except process_gc.GcPolicyConfigError as exc:
        out["config_error"] = str(exc)
    outcome = process_gc._boundary._outcome
    out["outcome"] = None if outcome is None else outcome.__dict__
    out["freeze_calls"] = len(freezes)
    out["marks"] = marks
    out["records"] = records
    out["after_lifespan"] = _collector()
    gc.collect()
    out["sentinels_alive"] = [ref() is not None for ref in sentinels]
    gc.freeze = real_freeze
    return out


if __name__ == "__main__":
    print("RESULT=" + json.dumps(run(sys.argv[1])), flush=True)
