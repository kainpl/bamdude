"""Negative controls: the pre-change lock shapes deadlock on PostgreSQL (G03 B; the "negative control" of TA17, TA20,
TA22, TA25, TA26, TA30, TA32 F1, TA35, TA37 and TA39).

Each case plays the two transactions the reviews traced, statement by statement, in the order that made them meet:
both take their first lock, then each asks for the other's. PostgreSQL's detector ends one with ``40P01``. The
writers no longer have these shapes -- the lane first, parents before the rows their holders wait for, router rows
ascending, ``NO KEY UPDATE`` for every explicit queue-row lock; the positive pairs beside this file (and the writer
guard) pin that. This file pins that the hazard the protocol removes is real, not a story. PostgreSQL only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.user import User
from backend.app.services.printer_queues import ensure_printer_queue
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

Step = Callable[[object], Awaitable[object]]


@pytest.fixture
def postgres_only(test_engine):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL's deadlock detector; SQLite serialises on its writer")


def sql(statement: str, **params) -> Step:
    async def run(db):
        return await db.execute(text(statement), params)

    return run


async def one_deadlock(factory, first: tuple[Step, Step], second: tuple[Step, Step]) -> None:
    """A takes its first lock, B takes its first; then A asks for B's and B for A's. Exactly one ends in 40P01."""
    took = [asyncio.Event(), asyncio.Event()]

    async def play(index, steps):
        async with factory() as db:
            await steps[0](db)
            took[index].set()
            await asyncio.wait_for(took[1 - index].wait(), 10)
            try:
                await steps[1](db)
            except DBAPIError as error:
                await db.rollback()
                return getattr(getattr(error, "orig", None), "sqlstate", None) or str(error)
            await db.commit()
            return None

    outcomes = await asyncio.wait_for(asyncio.gather(play(0, first), play(1, second)), 30)
    failures = [outcome for outcome in outcomes if outcome is not None]
    assert len(failures) == 1, outcomes
    assert "40P01" in failures[0] or "deadlock" in failures[0].lower(), failures


@pytest.fixture
async def farm(test_engine, db_session, printer_factory, archive_factory):
    printer = await printer_factory()
    await ensure_printer_queue(db_session, printer.id)
    archive = await archive_factory(printer.id, status="completed")
    library = LibraryFile(filename="f.3mf", file_path="library/f.3mf", file_type="3mf", file_size=1)
    user = User(username="guarded", password_hash="unused", role="admin", is_active=True)
    db_session.add_all([library, user])
    await db_session.flush()
    rows = [PrintQueueItem(queue_id=printer.id, position=i, status="pending") for i in (1, 2)]
    autos = [AutoQueueItem(status="pending", position=i, library_file_id=library.id) for i in (1, 2)]
    db_session.add_all([*rows, *autos])
    await db_session.commit()
    return {
        "factory": async_sessionmaker(test_engine, expire_on_commit=False),
        "queue": printer.id,
        "archive": archive.id,
        "library": library.id,
        "user": user.id,
        "rows": sorted(row.id for row in rows),
        "autos": sorted(auto.id for auto in autos),
    }


async def test_a_lane_less_writer_and_a_claim_cross_on_row_and_header(postgres_only, farm):
    """TA17 / TA20 / TA22 / TA26: a lane-less writer locks its job row, then recounts / halts the header; a claim
    locks the header, then the job row."""
    q, r = farm["queue"], farm["rows"][0]
    await one_deadlock(
        farm["factory"],
        (
            sql("SELECT id FROM print_queue WHERE id = :r FOR UPDATE", r=r),
            sql("UPDATE printer_queues SET status = 'idle' WHERE id = :q", q=q),
        ),
        (
            sql("SELECT id FROM printer_queues WHERE id = :q FOR UPDATE", q=q),
            sql("UPDATE print_queue SET status = 'printing' WHERE id = :r", r=r),
        ),
    )


async def test_router_rows_locked_in_two_orders_cross(postgres_only, farm):
    """TA25: the pre-change batch cancel deleted its rows unordered ({2, 1}); the waiting batch writes them
    ascending ({1, 2})."""
    low, high = farm["autos"]
    await one_deadlock(
        farm["factory"],
        (
            sql("SELECT id FROM auto_queue_items WHERE id = :i FOR UPDATE", i=high),
            sql("DELETE FROM auto_queue_items WHERE id = :i", i=low),
        ),
        (
            sql("SELECT id FROM auto_queue_items WHERE id = :i FOR UPDATE", i=low),
            sql("UPDATE auto_queue_items SET waiting_reason = 'x' WHERE id = :i", i=high),
        ),
    )


async def test_a_job_row_before_its_archive_crosses_an_archive_writer(postgres_only, farm):
    """TA30 / TA37 / TA40: r3's attempt end locked the job row and then the archive its link names (the link's own
    FK check); a Workshop or completion writer locks the archive and then the job row."""
    a, r = farm["archive"], farm["rows"][0]
    await one_deadlock(
        farm["factory"],
        (
            sql("SELECT id FROM print_queue WHERE id = :r FOR NO KEY UPDATE", r=r),
            sql("SELECT id FROM print_archives WHERE id = :a FOR KEY SHARE", a=a),
        ),
        (
            sql("SELECT id FROM print_archives WHERE id = :a FOR UPDATE", a=a),
            sql("SELECT id FROM print_queue WHERE id = :r FOR NO KEY UPDATE", r=r),
        ),
    )


async def test_a_router_row_before_its_library_parent_crosses_the_swap_refresh(postgres_only, farm):
    """TA35: r5's rebalance held the router row and let the conversion's FK check take the target file after it; the
    swap refresh takes that file FOR UPDATE, then the router row."""
    f, auto = farm["library"], farm["autos"][0]
    await one_deadlock(
        farm["factory"],
        (
            sql("SELECT id FROM auto_queue_items WHERE id = :i FOR NO KEY UPDATE", i=auto),
            sql("SELECT id FROM library_files WHERE id = :f FOR KEY SHARE", f=f),
        ),
        (
            sql("SELECT id FROM library_files WHERE id = :f FOR UPDATE", f=f),
            sql("SELECT id FROM auto_queue_items WHERE id = :i FOR NO KEY UPDATE", i=auto),
        ),
    )


async def test_a_for_update_header_crosses_the_runner_s_archive_insert(postgres_only, farm):
    """TA32 F1: r4's halt held the header FOR UPDATE and then wanted the job row; the runner held the job row and
    inserted its execution archive naming the header (FK check: KEY SHARE)."""
    q, r = farm["queue"], farm["rows"][0]
    await one_deadlock(
        farm["factory"],
        (
            sql("SELECT id FROM printer_queues WHERE id = :q FOR UPDATE", q=q),
            sql("SELECT id FROM print_queue WHERE id = :r FOR NO KEY UPDATE", r=r),
        ),
        (
            sql("SELECT id FROM print_queue WHERE id = :r FOR NO KEY UPDATE", r=r),
            sql("SELECT id FROM printer_queues WHERE id = :q FOR KEY SHARE", q=q),
        ),
    )


async def test_a_for_update_account_guard_crosses_a_lane_holder_naming_the_account(postgres_only, farm):
    """TA39: r5's lifetime guard held the account FOR UPDATE and then asked for the printer's lane; the tick held that
    lane and inserted a copy naming the account (FK check: KEY SHARE). An advisory lock is part of the detector's
    graph."""
    q, u = farm["queue"], farm["user"]
    await one_deadlock(
        farm["factory"],
        (
            sql("SELECT id FROM users WHERE id = :u FOR UPDATE", u=u),
            sql("SELECT pg_advisory_xact_lock(1625, :q)", q=q),
        ),
        (
            sql("SELECT pg_advisory_xact_lock(1625, :q)", q=q),
            sql("SELECT id FROM users WHERE id = :u FOR KEY SHARE", u=u),
        ),
    )


def test_the_shapes_name_the_tables_the_models_use():
    # Raw SQL above names tables by hand; a rename would silently test nothing.
    assert {PrintQueueItem.__tablename__, AutoQueueItem.__tablename__, PrintArchive.__tablename__} == {
        "print_queue",
        "auto_queue_items",
        "print_archives",
    }
    assert (LibraryFile.__tablename__, User.__tablename__) == ("library_files", "users")
