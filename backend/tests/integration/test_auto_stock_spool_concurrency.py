"""Separate transactions compete for one synthetic spool, on SQLite and PostgreSQL.

PostgreSQL uses its own temporary schema on an explicitly supplied test server;
no application database is wiped. AUTO_STOCK_TEST_POSTGRES_URL is opt-in.
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import event as sql_event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.database import Base, configure_sqlite_connection
from backend.app.models.printer import Printer
from backend.app.models.spool_assignment import SpoolAssignment
from backend.tests.unit.services.test_auto_stock_spool import GROUP, event, manager, spool


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
@pytest.mark.parametrize("added_full", [True, None])
async def test_two_printers_cannot_claim_the_same_spool(test_engine, tmp_path, monkeypatch, dialect, added_full):
    from backend.app.services.auto_stock_spool import available_groups, claim_on_insertion

    admin = None
    schema = "auto_stock_test_" + uuid.uuid4().hex
    if dialect == "postgres":
        url = os.environ.get("AUTO_STOCK_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("AUTO_STOCK_TEST_POSTGRES_URL not set; requires a disposable test server")
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "stock-race.sqlite"))
        sql_event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    first_chosen, release = asyncio.Event(), asyncio.Event()

    async def hold_claim(*args, **kwargs):
        first_chosen.set()
        await release.wait()

    monkeypatch.setattr("backend.app.services.print_usage_journal.note_assignment_change", hold_claim)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            printers = [
                Printer(
                    name=f"Synthetic {n}",
                    serial_number=f"SYNTHETIC{n}",
                    ip_address=f"192.0.2.{n}",
                    access_code="x",
                    is_active=True,
                    ams_policies={"auto_stock_spool": {"enabled": True, "group": GROUP}},
                )
                for n in (1, 2)
            ]
            db.add_all(printers)
            await db.commit()
            ids = [p.id for p in printers]
            await spool(db, added_full=added_full)
            # PostgreSQL requires the ORDER BY coalesce expression to share
            # its bound parameter with GROUP BY; SQLite silently accepts a
            # separately constructed expression. Exercise the live selector.
            assert await available_groups(db) == [GROUP | {"available_count": 1}]

        async def claim(pid):
            pm, _, _ = manager()
            async with sessions() as db:
                return await claim_on_insertion(db, printer_id=pid, event=event(), manager=pm)

        first = asyncio.create_task(claim(ids[0]))
        await asyncio.wait_for(first_chosen.wait(), 10)
        second = asyncio.create_task(claim(ids[1]))
        try:
            if dialect == "postgres":
                # SKIP LOCKED refuses the held candidate before the winner commits.
                assert (await asyncio.wait_for(asyncio.shield(second), 10))["reason"] == "no_full_stock"
            else:
                await asyncio.sleep(0.1)
                assert not second.done()  # SQLite waits at the existing write lock.
        finally:
            release.set()
        a, b = await asyncio.wait_for(asyncio.gather(first, second), 15)
        assert a["reason"] == "assigned" and b["reason"] == "no_full_stock"
        async with sessions() as db:
            assert len((await db.execute(select(SpoolAssignment))).scalars().all()) == 1
            assert await available_groups(db) == []
    finally:
        release.set()
        await engine.dispose()
        if admin:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()
