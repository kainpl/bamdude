"""Runs one PostgreSQL scenario in a fresh interpreter and reports JSON.

``core.database`` builds its engine at import time from ``settings``, so the
target database cannot be switched inside a running process. Each scenario
therefore executes as a subprocess with ``DATABASE_URL`` / ``DATA_DIR`` set —
which is also exactly how the application itself starts, so the test exercises
the real path rather than a reassembled imitation.

Modes:
    seed     build a SQLite database at DATA_DIR and put a row in a few tables
    fresh    run init_db() against an empty PostgreSQL, report the schema
    migrate  run init_db() with a SQLite alongside, triggering auto-migration

Usage: python -m backend.tests.integration.postgres_scenario_runner <mode>
"""

import asyncio
import json
import sys


async def _init() -> None:
    from backend.app.core.database import init_db

    await init_db()


async def _report() -> dict:
    from sqlalchemy import text

    from backend.app.core.database import Base, engine

    async with engine.begin() as conn:
        tables = [
            r[0]
            for r in (
                await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"))
            ).all()
        ]
        migrations = [
            r[0] for r in (await conn.execute(text("SELECT version FROM _migrations ORDER BY version"))).all()
        ]
        groups = [
            {"name": r[0], "permissions": len(r[1] or [])}
            for r in (await conn.execute(text("SELECT name, permissions FROM groups ORDER BY id"))).all()
        ]
        counts = {}
        for t in ("users", "printers", "print_archives", "spool", "projects"):
            if t in tables:
                counts[t] = (await conn.execute(text(f'SELECT COUNT(*) FROM "{t}"'))).scalar()  # noqa: S608

        # A sequence left behind MAX(id) is invisible until the next insert.
        lagging = []
        for t in tables:
            has_id = (
                await conn.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=:t AND column_name='id'"
                    ),
                    {"t": t},
                )
            ).scalar()
            if not has_id:
                continue
            seq = (await conn.execute(text("SELECT pg_get_serial_sequence(:t, 'id')"), {"t": t})).scalar()
            if not seq:
                continue
            max_id = (await conn.execute(text(f'SELECT MAX(id) FROM "{t}"'))).scalar()  # noqa: S608
            if max_id is None:
                continue
            last = (await conn.execute(text(f"SELECT last_value FROM {seq}"))).scalar()  # noqa: S608
            if last < max_id:
                lagging.append({"table": t, "max_id": max_id, "sequence": last})

    await engine.dispose()
    return {
        "tables": tables,
        "declared": sorted(Base.metadata.tables.keys()),
        "migrations": migrations,
        "groups": groups,
        "counts": counts,
        "lagging_sequences": lagging,
    }


async def _seed() -> dict:
    """Put one row in a couple of tables so the migration has data to carry.

    Written through the ORM rather than raw SQL: several NOT NULL columns take
    their value from a Python-side ``default=``, which the database never
    supplies, so a hand-written INSERT trips over columns the model considers
    optional. Going through the session is also how the application writes.
    """
    from sqlalchemy import func, select

    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from backend.app.models.project import Project

    async with async_session() as db:
        db.add(
            Printer(
                name="scenario-printer",
                ip_address="10.0.0.1",
                access_code="00000000",
                serial_number="SCENARIO1",
                model="X1C",
            )
        )
        db.add(Project(name="scenario-project"))
        await db.commit()

        written = {}
        for table, model in (("printers", Printer), ("projects", Project)):
            written[table] = (await db.execute(select(func.count()).select_from(model))).scalar()
    return written


async def _main(mode: str) -> dict:
    # One event loop for the whole run. The engine is a module-level singleton
    # holding connections bound to whichever loop created them, so a second
    # ``asyncio.run`` finds them attached to a closed loop and dies inside the
    # proactor with a bare ``AttributeError: 'NoneType' has no attribute 'send'``.
    await _init()
    if mode == "seed":
        return {"seeded": await _seed()}
    return await _report()


def main() -> None:
    print(json.dumps(asyncio.run(_main(sys.argv[1]))))


if __name__ == "__main__":
    main()
