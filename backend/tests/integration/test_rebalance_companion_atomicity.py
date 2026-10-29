"""A move is ONE captured revision and ONE transaction of rows (spec rebalance-companion-atomicity r2, B1).

Before: the converted row was committed first, then ``add_items_to_auto_queue`` captured the
target file AGAIN for the k − 1 companions and committed them in a transaction of its own, and the
stamps came after. A file that changed between the two captures gave the companions other bytes
(an unknown declaration the router refuses on a swap printer), and a process that died between
the commits left a converted row short of its companions — or companions without the stamp the
automatic once-filter (T23) reads, free to be moved again.

Now the move captures once, publishes that blob, and under its pin writes the conversion, every
companion and every stamp in the rebalance's own session with one commit. These run on a database
file, so another session is another connection and what it reads is real evidence.
"""

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.printer import Printer
from backend.app.models.queue_source import QueueSource
from backend.app.services import auto_queue_add, queue_rebalance, queue_source_capture, swap_compatibility
from backend.app.services.filament_intake import item_descriptor
from backend.tests.integration.test_rebalance_assignment import _SwapProfile
from backend.tests.integration.test_rebalance_job_feasibility import (  # noqa: F401 — fixtures by name
    _live,
    clean_spool,
    petg_farm,
)
from backend.tests.integration.test_rebalance_moves_one_job_once import test_engine  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _rows(test_engine) -> list[AutoQueueItem]:
    async with AsyncSession(test_engine, expire_on_commit=False) as fresh:
        return list((await fresh.execute(select(AutoQueueItem).order_by(AutoQueueItem.id))).scalars().all())


def _count_captures(monkeypatch) -> list:
    """Every capture of a source, whichever module asked for it."""
    calls: list = []
    real = queue_source_capture.capture_staged

    async def counting(plan):
        calls.append(plan)
        return await real(plan)

    for module in (queue_source_capture, queue_rebalance, auto_queue_add):
        monkeypatch.setattr(module, "capture_staged", counting)
    return calls


async def _declared_plain_on_swap_printers(db_session, farm, monkeypatch):
    """The target file declared plain for ITS bytes, the receivers in swap mode and the profile asking
    for our swap start: a row the router assigns from those bytes is accepted; from other bytes it
    is ``source_unknown`` and refused (Codex B1 probe)."""
    path = Path(farm.alt_file.file_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    await db_session.execute(
        update(LibraryFile)
        .where(LibraryFile.id == farm.alt_file.id)
        .values(file_hash=digest, swap_compatible=False, is_external=False)
    )
    await db_session.execute(
        update(Printer).where(Printer.id.in_([p.id for p in farm.receivers])).values(swap_mode_enabled=True)
    )
    await db_session.commit()
    monkeypatch.setattr(
        "backend.app.services.auto_queue_scheduler.preference_options", AsyncMock(return_value=_SwapProfile())
    )
    return path, digest


async def _refusals(db_session, rows):
    refused = []
    for row in rows:
        fact = (await item_descriptor(db_session, row)).swap_fact.value
        try:
            swap_compatibility.resolve_request(fact, execute=True, events=["swap_mode_start"], printer_swap_mode=True)
        except swap_compatibility.SwapRequestRefused as exc:
            refused.append((row.id, exc.reason))
    return refused


@pytest.mark.parametrize("changed", [False, True], ids=["same-bytes", "file-changed-after-the-publication"])
async def test_the_whole_group_is_one_captured_revision(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool, changed
):
    """B-A01: Codex's B1 probe with the barrier where the second capture used to start — right after
    the move's publication — and the library bytes really changed there. One capture, every row on
    the same blob and snapshot, every assignment accepted by the router's own resolver."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=3)
    path, digest = await _declared_plain_on_swap_printers(db_session, farm, monkeypatch)
    captures = _count_captures(monkeypatch)
    real_publish = queue_rebalance.publish_staged

    async def publish_then_change(staged, attach):
        blob = await real_publish(staged, attach)
        if changed:
            with path.open("ab") as stream:
                stream.write(b"\nnext revision\n")
        return blob

    monkeypatch.setattr(queue_rebalance, "publish_staged", publish_then_change)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)

    assert (result.converted, result.created, result.skipped) == (1, 1, [])
    assert len(captures) == 1
    rows = await _rows(test_engine)
    assert len(rows) == 2
    assert len({row.queue_source_id for row in rows}) == 1
    assert len({json.dumps(row.source_snapshot, sort_keys=True) for row in rows}) == 1
    async with AsyncSession(test_engine) as fresh:
        blob = await fresh.get(QueueSource, rows[0].queue_source_id)
    assert blob.sha256 == digest  # the bytes the decision read, not the file's next revision
    assert {row.batch_id for row in rows} != {None} and len({row.batch_id for row in rows}) == 1
    assert all(row.rebalanced_at is not None for row in rows)
    assert await _refusals(db_session, rows) == []


async def test_another_reader_sees_the_old_job_or_the_whole_group(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool
):
    """Plan 1.2: after every commit the move makes, another connection reads the line. It sees the
    job as it was (one unconverted row) or the whole group (k rows, all converted and stamped) —
    never a converted row short of its companions, never an unstamped companion."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=2)  # 6 parts / 2 → k = 3
    seen: list = []
    real_commit = db_session.commit

    async def commit_and_look(*args, **kwargs):
        await real_commit(*args, **kwargs)
        rows = await _rows(test_engine)
        seen.append(
            (
                len(rows),
                sorted(row.library_file_id == farm.alt_file.id for row in rows),
                sorted(row.rebalanced_at is not None for row in rows),
            )
        )

    with monkeypatch.context() as patched:
        patched.setattr(db_session, "commit", commit_and_look)
        p_elig, p_sched = _live(farm)
        with p_elig, p_sched:
            result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)

    assert (result.converted, result.created) == (1, 2)
    old, whole = (1, [False], [False]), (3, [True] * 3, [True] * 3)
    assert seen and all(state in (old, whole) for state in seen), seen
    assert seen[-1] == whole


def _fail(where):
    """A failure at one point of the move's write, before its commit (B-A03)."""
    if where == "before_the_companions":
        return {"build_auto_queue_rows": AsyncMock(side_effect=RuntimeError("the builder is down"))}
    if where == "after_the_companions_were_inserted":
        real = queue_rebalance.build_auto_queue_rows

        async def built_then_fail(*args, **kwargs):
            await real(*args, **kwargs)
            raise RuntimeError("after the insert")

        return {"build_auto_queue_rows": built_then_fail}
    real_write = queue_rebalance._write_group

    async def flushed_then_fail(*args, **kwargs):
        await real_write(*args, **kwargs)
        raise RuntimeError("everything flushed, nothing committed")

    return {"_write_group": flushed_then_fail}


@pytest.mark.parametrize("where", ["before_the_companions", "after_the_companions_were_inserted", "everything_flushed"])
async def test_a_failure_before_the_commit_leaves_the_job_and_its_automatic_move(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool, where
):
    """B-A03: one rollback — another connection reads the job exactly as it was, no companion, no
    stamp; so the automatic move was not spent and the next automatic pass may still make it."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=2)
    item_id, home_file_id = farm.item.id, farm.home_file.id
    with monkeypatch.context() as patched:
        for name, replacement in _fail(where).items():
            patched.setattr(queue_rebalance, name, replacement)
        p_elig, p_sched = _live(farm)
        with p_elig, p_sched:
            result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert (result.converted, result.created, result.moved_parts) == (0, 0, 0)
    assert result.skipped == [(item_id, "creation_failed")]
    rows = await _rows(test_engine)
    assert [(row.id, row.library_file_id, row.rebalanced_at, row.rebalanced_from_model) for row in rows] == [
        (item_id, home_file_id, None, None)
    ]
    # The caller's own object is the job as it was and readable without IO — an expired row would
    # need a lazy load an async session cannot do.
    assert (farm.item.library_file_id, farm.item.rebalanced_at) == (home_file_id, None)

    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        again = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=False)
    assert (again.converted, again.created) == (1, 2), again.skipped


async def test_a_commit_that_landed_without_an_answer_stops_the_pass_and_deletes_nothing(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool
):
    """B-A05: the commit reached the database and its acknowledgement was lost. The move is not
    counted, not undone and not repeated in this call; the group on disk is whole and stamped, so
    an automatic pass leaves it alone."""
    from sqlalchemy.exc import OperationalError

    from backend.app.services import queue_sources

    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=2)
    item_id = farm.item.id
    real_commit = db_session.commit

    async def landed_then_lost(*args, **kwargs):
        await real_commit(*args, **kwargs)
        if queue_sources.pinned_source_ids():
            raise OperationalError("COMMIT", {}, Exception("connection lost after the commit"))

    with monkeypatch.context() as patched:
        patched.setattr(db_session, "commit", landed_then_lost)
        p_elig, p_sched = _live(farm)
        with p_elig, p_sched, pytest.raises(OperationalError):
            await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    await db_session.rollback()

    rows = await _rows(test_engine)
    assert len(rows) == 3
    assert all(row.library_file_id == farm.alt_file.id and row.rebalanced_at is not None for row in rows)
    assert queue_sources.pinned_source_ids() == frozenset()

    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        again = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=False)
    assert (again.converted, again.created) == (0, 0)
    assert {reason for _id, reason in again.skipped} <= {"cooldown", "already_rebalanced_auto"}, again.skipped
    assert len(await _rows(test_engine)) == 3
    assert item_id in {row.id for row in rows}


async def test_the_companions_file_after_every_pending_job_and_keep_the_job_s_batch(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool
):
    """B-A08: positions are read in the move's own transaction — max pending + 1, … — and a job
    already in a batch keeps it; its old batch neighbour is not stamped."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=2)
    neighbour = AutoQueueItem(
        library_file_id=farm.home_file.id,
        plate_id=1,
        target_model="X1E",
        required_filament_types=json.dumps(["PETG"]),
        print_time_seconds=60,
        status="pending",
        position=40,
        batch_id="the-old-batch",
    )
    db_session.add(neighbour)
    await db_session.execute(
        update(AutoQueueItem).where(AutoQueueItem.id == farm.item.id).values(batch_id="the-old-batch")
    )
    await db_session.commit()
    neighbour_id = neighbour.id
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert (result.converted, result.created) == (1, 2), result.skipped
    rows = {row.id: row for row in await _rows(test_engine)}
    companions = sorted(
        (row for row in rows.values() if row.id not in (farm.item.id, neighbour_id)), key=lambda r: r.id
    )
    assert [row.position for row in companions] == [41, 42]
    assert {row.batch_id for row in companions} | {rows[farm.item.id].batch_id} == {"the-old-batch"}
    assert rows[neighbour_id].rebalanced_at is None and rows[neighbour_id].batch_id == "the-old-batch"
    assert rows[farm.item.id].position == 1


@pytest.mark.skipif(
    not __import__("os").environ.get("DISPATCH_TEST_POSTGRES_URL"),
    reason="PostgreSQL only: SQLite serialises the two writers, and that is legal (spec B-A08)",
)
async def test_a_concurrent_add_may_share_a_position_and_the_id_breaks_the_tie(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool
):
    """B-A08 on PostgreSQL: right after the move read ``max(position)``, an unrelated public add on
    another connection reads the same maximum and commits first. Positions may then repeat — no
    lock or sequence is added for that — but no row is lost or doubled, and the router's order is
    total: equal positions fall back to the id."""
    from sqlalchemy.util import await_only

    from backend.app.schemas.auto_queue import AutoQueueItemCreate
    from backend.app.services.auto_queue_add import add_items_to_auto_queue
    from backend.app.services.auto_queue_eligibility import router_order

    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=2)
    home_file_id = farm.home_file.id
    armed: list = []
    raced: list = []

    async def concurrent_add():
        async with AsyncSession(test_engine, expire_on_commit=False) as other:
            created = await add_items_to_auto_queue(
                other, AutoQueueItemCreate(library_file_id=home_file_id, plate_id=1, quantity=1), None
            )
            raced.extend(row.id for row in created)

    def after_execute(conn, cursor, statement, parameters, context, executemany):
        # The builder's own read, once: the concurrent add then runs to its commit before the
        # move's transaction goes on (``await_only`` runs it from inside the move's greenlet).
        if armed and not raced and "max(auto_queue_items.position)" in statement.lower():
            raced.append(None)
            await_only(concurrent_add())

    real_builder = queue_rebalance.build_auto_queue_rows

    async def armed_builder(*args, **kwargs):
        armed.append(True)
        return await real_builder(*args, **kwargs)

    monkeypatch.setattr(queue_rebalance, "build_auto_queue_rows", armed_builder)
    event.listen(test_engine.sync_engine, "after_cursor_execute", after_execute)
    try:
        p_elig, p_sched = _live(farm)
        with p_elig, p_sched:
            result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    finally:
        event.remove(test_engine.sync_engine, "after_cursor_execute", after_execute)
    assert (result.converted, result.created) == (1, 2), result.skipped
    concurrent_id = raced[1]
    rows = await _rows(test_engine)
    assert len(rows) == 4 and len({row.id for row in rows}) == 4
    by_id = {row.id: row for row in rows}
    companions = sorted(row.id for row in rows if row.id not in (farm.item.id, concurrent_id))
    assert by_id[concurrent_id].position == by_id[companions[0]].position  # the same max + 1
    async with AsyncSession(test_engine) as fresh:
        ordered = (await fresh.execute(select(AutoQueueItem.id).order_by(*router_order(False)))).scalars().all()
    tied = [row_id for row_id in ordered if by_id[row_id].position == by_id[concurrent_id].position]
    assert tied == sorted(tied)


async def test_a_restore_before_the_publication_moves_nothing(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool
):
    """B-A10: a restore voids the copy in flight (the storage epoch); its publication refuses and
    the job stays as it was."""
    from backend.app.services import queue_sources

    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=2)
    item_id = farm.item.id
    real_capture = queue_rebalance.capture_staged

    async def captured_then_restored(plan):
        staged = await real_capture(plan)
        queue_sources.invalidate_receipts()
        return staged

    monkeypatch.setattr(queue_rebalance, "capture_staged", captured_then_restored)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert (result.converted, result.created) == (0, 0)
    assert result.skipped == [(item_id, "source_copy_busy")]
    rows = await _rows(test_engine)
    assert [(row.id, row.rebalanced_at) for row in rows] == [(item_id, None)]


async def test_a_failed_move_leaves_its_blob_to_the_collector_s_grace(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool
):
    """B-A10: the blob a failed move published has no owner once the pin goes; the collector marks
    it and releases it only after the grace window — nothing removes it early, nothing keeps it."""
    from datetime import datetime, timedelta, timezone

    from backend.app.services import queue_sources

    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=2)
    monkeypatch.setattr(
        queue_rebalance, "build_auto_queue_rows", AsyncMock(side_effect=RuntimeError("the builder is down"))
    )
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert result.skipped == [(farm.item.id, "creation_failed")]
    assert queue_sources.pinned_source_ids() == frozenset()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    first = await queue_sources.collect(now, force=True)
    assert (first.marked, first.released) == (1, 0)
    later = await queue_sources.collect(now + timedelta(seconds=queue_sources.ORPHAN_GRACE_SECONDS + 60), force=True)
    assert later.released == 1


async def test_a_restore_while_the_move_waits_for_its_locks_writes_the_whole_group_or_nothing(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool
):
    """B-R8: the pin protects the blob from the collector and is no proof of the database; the move
    adds no generation of its own. Whatever a restore then does to this database, what the move
    wrote is one group — here, where the database is not replaced, the whole group."""
    from backend.app.services import order_resources, queue_sources

    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=2)
    real_policy_for = order_resources.policy_for

    async def restored_meanwhile(*args, **kwargs):
        queue_sources.invalidate_receipts()
        return await real_policy_for(*args, **kwargs)

    monkeypatch.setattr(order_resources, "policy_for", restored_meanwhile)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert (result.converted, result.created) == (1, 2), result.skipped
    rows = await _rows(test_engine)
    assert len(rows) == 3 and all(row.rebalanced_at is not None for row in rows)
    assert queue_sources.pinned_source_ids() == frozenset()


@pytest.mark.parametrize("kill", ["before_commit", "after_commit"])
async def test_a_killed_process_leaves_the_old_job_or_the_whole_group(tmp_path, kill):
    """B-A04: the process really dies (``os._exit`` in ``rebalance_kill_child``) just before, or
    just after, the move's first pinned commit. The database file then holds the job as it was, or
    the whole stamped group — never a converted row short of its companions."""
    import asyncio
    import os
    import re
    import sqlite3
    import subprocess
    import sys
    from uuid import uuid4

    repo = Path(__file__).resolve().parents[3]
    database = tmp_path / "killed.sqlite"
    env = {**os.environ, "BAMDUDE_B1_DB": str(database), "BAMDUDE_B1_KILL": kill, "PYTHONIOENCODING": "utf-8"}
    pg_url = os.environ.get("DISPATCH_TEST_POSTGRES_URL")
    schema = "b1kill_" + uuid4().hex
    if pg_url:
        env.update(BAMDUDE_B1_PG_URL=pg_url, BAMDUDE_B1_PG_SCHEMA=schema)
    done = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-X",
            "utf8",
            "-m",
            "pytest",
            "backend/tests/integration/rebalance_kill_child.py",
            "-q",
            "-s",
            "-p",
            "no:cacheprovider",
            "--timeout=300",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    assert done.returncode == 17, done.stdout[-4000:] + done.stderr[-2000:]
    farm = dict(re.findall(r"(\w+)=(\d+)", next(line for line in done.stdout.splitlines() if line.startswith("FARM"))))
    query = "SELECT id, library_file_id, rebalanced_at IS NOT NULL FROM auto_queue_items ORDER BY id"
    if pg_url:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(pg_url, connect_args={"server_settings": {"search_path": schema}})
        try:
            async with engine.connect() as connection:
                rows = [(a, b, int(c)) for a, b, c in (await connection.execute(text(query))).all()]
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await engine.dispose()
    else:
        with sqlite3.connect(database) as connection:
            rows = connection.execute(query).fetchall()
    if kill == "before_commit":
        assert rows == [(int(farm["item"]), int(farm["home_file"]), 0)]
    else:
        assert len(rows) == 3, rows
        assert all(file_id == int(farm["alt_file"]) and stamped for _id, file_id, stamped in rows), rows


async def test_one_row_transaction_under_the_pin(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool
):
    """B-A02: inside the pin's window the move commits once — the conversion, the companions and
    their stamps together — and the publication is the only other transaction it opens."""
    from backend.app.services import queue_sources

    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=2)
    pinned_commits: list = []
    real_commit = db_session.commit

    async def commit(*args, **kwargs):
        if queue_sources.pinned_source_ids():
            pinned_commits.append(1)
        return await real_commit(*args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(db_session, "commit", commit)
        p_elig, p_sched = _live(farm)
        with p_elig, p_sched:
            result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert (result.converted, result.created) == (1, 2)
    assert pinned_commits == [1]
