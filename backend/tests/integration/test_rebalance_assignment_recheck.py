"""A move's final check asks the router's assignment again, with fresh data (Codex review r2, T2-R1).

The forecast places a job where ``_assign`` would — the order's choices, the owner's saved profile,
the receiver's swap mode, the source's declaration (``test_rebalance_assignment.py``). The copy of
the target file releases the transaction, so any of those may change before the move is written:
each test commits a change from a session of its own right after the real copy, and the move must
be called off (``eligibility_changed``, nothing converted) — while the same farm, unchanged, moves.
A database file, so the other session is another connection and its commit is real evidence.
"""

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_options_preference import PrintOptionsPreference
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.user import User
from backend.app.services import queue_rebalance
from backend.tests.integration.test_auto_queue_scheduler import _drying_status
from backend.tests.integration.test_rebalance_assignment import TOGGLES, _entry, _preheat_settings, _SwapProfile
from backend.tests.integration.test_rebalance_job_feasibility import (  # noqa: F401 — fixtures by name
    _live,
    petg_farm,
    tick_scheduler,
)
from backend.tests.integration.test_rebalance_moves_one_job_once import (  # noqa: F401 — fixtures by name
    clean_spool,
    test_engine,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CHANGES = pytest.mark.parametrize("change", [False, True], ids=["unchanged", "changed-during-copy"])


def _after_copy(monkeypatch, test_engine, edit):
    """Commit ``edit(session)`` from a session of its own right after the real copy, once."""
    real = queue_rebalance._capture_target
    done = []

    async def capture_then_edit(*args, **kwargs):
        staged = await real(*args, **kwargs)
        if edit is not None and not done:
            async with AsyncSession(test_engine, expire_on_commit=False) as other:
                await edit(other)
                await other.commit()
            done.append(True)
        return staged

    monkeypatch.setattr(queue_rebalance, "_capture_target", capture_then_edit)
    return done


def _swap_preference(user_id, model, *, execute):
    return PrintOptionsPreference(
        user_id=user_id,
        printer_model=model,
        options={
            "print_options": TOGGLES,
            "swap_macros": {"execute": execute, "events": ["swap_mode_start"] if execute else []},
        },
    )


def _ask_for_swap(user_id, model):
    async def edit(session):
        await session.execute(
            update(PrintOptionsPreference)
            .where(PrintOptionsPreference.printer_model == model)
            .where(
                PrintOptionsPreference.user_id.is_(None)
                if user_id is None
                else PrintOptionsPreference.user_id == user_id
            )
            .values(options={"print_options": TOGGLES, "swap_macros": {"execute": True, "events": ["swap_mode_start"]}})
        )

    return edit


def _expect(result, item_id, change, done=None):
    if change:
        assert done in (None, [True])
        assert (result.converted, result.skipped) == (0, [(item_id, "eligibility_changed")])
    else:
        assert (result.converted, result.skipped) == (1, []), result.skipped


@CHANGES
async def test_a_preheat_edit_during_the_copy_calls_the_move_off(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool, change
):
    """Codex r2 case 1: at home 300 s left of a print + preheat 1200 s + the job's 3600 s = 5100 s;
    the P1S plate, preheat off, ends at 4500 s, so the move is right. During the copy the operator
    switches the job's P2S preheat off through the ordinary edit: home is 3900 s now, the move's
    evidence is gone."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, home_trays=("PETG",), targets=1, alt_seconds=4500)
    item_id = farm.item.id
    farm.item.order_print_options = {"P2S": _entry("on"), "P1S": _entry("off")}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await db_session.execute(update(PrinterQueue).where(PrinterQueue.id == farm.home.id).values(status="printing"))
    db_session.add(
        PrintArchive(
            filename="running.gcode.3mf",
            file_path="",
            file_size=0,
            status="printing",
            printer_id=farm.home.id,
            print_time_seconds=3600,
            started_at=now - timedelta(seconds=3300),
        )
    )
    await _preheat_settings(db_session)

    async def preheat_off(session):
        from backend.app.api.routes.auto_queue import _apply_item_update

        _apply_item_update(await session.get(AutoQueueItem, item_id), {"preheat_override": "off"}, source_model="P2S")

    done = _after_copy(monkeypatch, test_engine, preheat_off if change else None)
    p_elig, p_sched = _live(farm, status={**farm.status, farm.home.id: _drying_status(["PETG"])})
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    _expect(result, item_id, change, done)


@CHANGES
async def test_a_receiver_switched_to_swap_mode_during_the_copy_calls_the_move_off(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool, change
):
    """Codex r2 case 2: the only receiver is a plain printer; the profile asks for our swap start
    and the P1S plate's file declares nothing usable. During the copy the receiver is switched to
    swap mode — the router would refuse the job there now (``swap_source_unknown``)."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    item_id, receiver_id = farm.item.id, farm.receivers[0].id
    monkeypatch.setattr(
        "backend.app.services.auto_queue_scheduler.preference_options", AsyncMock(return_value=_SwapProfile())
    )

    async def swap_mode_on(session):
        await session.execute(update(Printer).where(Printer.id == receiver_id).values(swap_mode_enabled=True))

    done = _after_copy(monkeypatch, test_engine, swap_mode_on if change else None)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    _expect(result, item_id, change, done)


@CHANGES
async def test_a_profile_saved_during_the_copy_calls_the_move_off(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool, change
):
    """The receiver is a swap-mode P1S and the job has no creator, so its P1S profile is the system
    row — saved without our swap start, so the receiver takes it. During the copy that row is saved
    again asking for the swap start: with the file's declaration unknown the router refuses now.
    Nothing of the row or the file changed, so only a fresh read of the profile can tell."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    item_id, receiver_id = farm.item.id, farm.receivers[0].id
    await db_session.execute(update(Printer).where(Printer.id == receiver_id).values(swap_mode_enabled=True))
    db_session.add(_swap_preference(None, "P1S", execute=False))
    await db_session.commit()
    done = _after_copy(monkeypatch, test_engine, _ask_for_swap(None, "P1S") if change else None)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    _expect(result, item_id, change, done)


@CHANGES
async def test_the_companions_owner_profile_saved_during_the_copy_calls_the_move_off(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool, change
):
    """k = 2 on two swap-mode P1S. The converted row has no creator (the system profile, saved
    without a swap start); its companion is written by the person who pressed the button, whose own
    P1S profile asks for nothing either — so both prints fit. During the copy only that person's
    profile is saved again asking for the swap start: the companion would be refused on both."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=3)
    item_id = farm.item.id
    await db_session.execute(
        update(Printer).where(Printer.id.in_([p.id for p in farm.receivers])).values(swap_mode_enabled=True)
    )
    user = User(username="planner", password_hash="x", is_active=True)
    db_session.add(user)
    await db_session.flush()
    db_session.add_all([_swap_preference(None, "P1S", execute=False), _swap_preference(user.id, "P1S", execute=False)])
    await db_session.commit()
    done = _after_copy(monkeypatch, test_engine, _ask_for_swap(user.id, "P1S") if change else None)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True, current_user=user)
    _expect(result, item_id, change, done)


@pytest.mark.parametrize("matches", [True, False], ids=["hash-matches-the-bytes", "hash-of-other-bytes"])
async def test_the_declaration_is_the_one_the_copy_carries(
    db_session, test_engine, printer_factory, tmp_path, monkeypatch, clean_spool, matches
):
    """The router assigns the moved row by the declaration its captured bytes carry, confirmed
    against their own hash — not the library row's. The P1S plate's file is declared plain (a
    managed file, False, with its hash) and the receiver is a swap-mode printer the profile asks a
    swap start of: the decision reads False and lets it take the job. When the recorded hash is
    another file's, the copy's declaration is unknown and the router would refuse it."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    item_id, receiver_id = farm.item.id, farm.receivers[0].id
    data = Path(farm.alt_file.file_path).read_bytes()
    digest = hashlib.sha256(data if matches else data + b"another revision").hexdigest()
    await db_session.execute(
        update(LibraryFile)
        .where(LibraryFile.id == farm.alt_file.id)
        .values(file_hash=digest, swap_compatible=False, is_external=False)
    )
    await db_session.execute(update(Printer).where(Printer.id == receiver_id).values(swap_mode_enabled=True))
    await db_session.commit()
    monkeypatch.setattr(
        "backend.app.services.auto_queue_scheduler.preference_options", AsyncMock(return_value=_SwapProfile())
    )
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    _expect(result, item_id, not matches)
