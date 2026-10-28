"""The forecast places a job where the router's own assignment would (Codex review r1, T-R1/T-R3).

The router's ``_assign`` decides two things the feed check does not: the options a job is written
with — the creator's saved profile for the printer's model, then the order's explicit choices for
the job's own model, which also set its preparation — and whether a swap request the source cannot
honour refuses this printer. The forecast asks the same resolver (``assignment_options``) and the
same swap rule, so a job is dated with the preparation it will really get and is never placed on a
printer that will turn it away.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.settings import Settings
from backend.app.services import queue_rebalance
from backend.app.services.farm_forecast import _load_gates
from backend.tests.integration.test_rebalance_job_feasibility import (  # noqa: F401 — a fixture
    _live,
    petg_farm,
    tick_scheduler,
)
from backend.tests.integration.test_swap_policy_doors import (  # noqa: F401 — fixtures
    clean_spool_state,
    sessions,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: Preparation that matters: no upload allowance or plate service to blur it, preheat 900 + 300 s.
PREHEAT = (
    ("forecast_upload_seconds", "0"),
    ("forecast_plate_clear_minutes", "0"),
    ("preheat_enabled", "true"),
    ("preheat_max_wait_seconds", "900"),
    ("preheat_soak_seconds", "300"),
)
TOGGLES = {
    "bed_levelling": "off",
    "flow_cali": "off",
    "layer_inspect": False,
    "timelapse": False,
    "mesh_mode_fast_check": False,
    "gcode_injection": False,
    "nozzle_offset_cali": "off",
}


def _entry(preheat: str) -> dict:
    return {**TOGGLES, "preheat_override": preheat}


async def _preheat_settings(db_session):
    db_session.add_all([Settings(key=key, value=value) for key, value in PREHEAT])
    await db_session.commit()
    return await _load_gates(db_session)


class _SwapProfile:
    """A saved profile asking for our swap start — what the swap door tests use."""

    def for_printer_queue(self):
        return {"execute_swap_macros": True, "swap_macro_events": ["swap_mode_start"]}


async def _forecast(db_session):
    ctx = await queue_rebalance._pass_context(
        db_session,
        resources=await queue_rebalance._order_resources(db_session),
        busy=None,
        interlocked=set(),
        cache=None,
    )
    return await queue_rebalance._load_forecast(db_session, ctx, [], queue_rebalance._utcnow())


# ---------- T-R1: every job's preparation is the one the router will write ----------


@pytest.mark.parametrize("preheat", ["off", "on", "inherit"])
async def test_each_job_is_prepared_as_the_router_will_write_it(
    db_session, printer_factory, tmp_path, tick_scheduler, preheat
):
    """The order says preheat off / on / inherit for the P2S plate: the forecast dates the job with
    the preparation of exactly what the router writes on the row it places."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, home_trays=("PETG",), targets=1)
    farm.item.order_print_options = {"P2S": _entry(preheat)}
    gates = await _preheat_settings(db_session)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        forecast = await _forecast(db_session)
        [predicted] = [job.prep for job in forecast.farm.tail if job.item_id == farm.item.id]
        await tick_scheduler.tick()
    placed = (await db_session.execute(select(PrintQueueItem))).scalars().one()
    assert placed.preheat_override == preheat
    assert predicted == gates.prep_for(placed.preheat_override)


@pytest.mark.parametrize("target", ["off", "on"])
async def test_the_targets_own_entry_prepares_the_moved_prints(db_session, printer_factory, tmp_path, target):
    """The home P2S has PETG and 300 s left of its print; the job, preheat off for the P2S, ends
    there at 300 + 3600 = 3900 s. The P1S plate takes 3000 s: with the order's P1S entry off the
    move ends at 3000 s and is made; with it on the P1S also pays the preheat and ends later than
    home, so the job stays."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, home_trays=("PETG",), alt_seconds=3000)
    farm.item.order_print_options = {"P2S": _entry("off"), "P1S": _entry(target)}
    gates = await _preheat_settings(db_session)
    assert gates.prep_for("off") == 0 and gates.prep_for("on") > 900
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await db_session.execute(
        PrinterQueue.__table__.update().where(PrinterQueue.id == farm.home.id).values(status="printing")
    )
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
    await db_session.commit()
    from backend.tests.integration.test_auto_queue_scheduler import _drying_status

    p_elig, p_sched = _live(farm, status={**farm.status, farm.home.id: _drying_status(["PETG"])})
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    if target == "off":
        assert (result.converted, result.skipped) == (1, []), result.skipped
    else:
        assert (result.converted, result.skipped) == (0, [(farm.item.id, "no_faster_model")])


# ---------- T-R3: a printer the swap rule refuses this job is no capacity for it ----------


@pytest.mark.parametrize("fact", [False, None], ids=["known-plain-source", "unknown-source"])
async def test_a_swap_refusal_is_no_home_for_the_job(
    db_session, tmp_path, printer_factory, monkeypatch, sessions, fact
):
    """The swap door fixtures: a swap-mode P1P and a plain one, a job whose profile asks for our swap
    start. With a source known to carry no sequence the swap printer takes it; with an unknown one
    it refuses, and the router places the job on the plain printer — the forecast does the same."""
    from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
    from backend.app.services.printer_manager import printer_manager
    from backend.tests.integration import test_swap_policy_doors as doors

    swap, _queue = await doors.a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    plain = await doors.a_second_printer(
        db_session, printer_factory, monkeypatch, printer_manager._clients[swap.id], swap_mode=False
    )
    library = await doors.a_library(db_session, tmp_path, fact=fact)
    auto = await doors.an_auto_job(db_session, library, monkeypatch=monkeypatch)
    item_id, plain_id, swap_id = auto.id, plain.id, swap.id
    forecast = await _forecast(db_session)
    projection = queue_rebalance.start_projection([], forecast.farm)
    predicted = projection.result_of(item_id).lane_of.get(item_id)
    await AutoQueueScheduler().tick()
    placed = (await db_session.execute(select(PrintQueueItem))).scalars().one()
    assert placed.queue_id == (plain_id if fact is None else swap_id)
    assert predicted == placed.queue_id


@pytest.mark.parametrize("swap_mode", [False, True], ids=["plain-receiver", "swap-receiver"])
async def test_a_receiver_that_refuses_the_target_takes_nothing(
    db_session, printer_factory, tmp_path, monkeypatch, swap_mode
):
    """The P1S plate's library row declares nothing usable (no hash), and the owner's profile asks
    for our swap start: a swap-mode P1S would refuse the moved job, so it is no receiver."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    [receiver] = farm.receivers
    await db_session.execute(
        Printer.__table__.update().where(Printer.id == receiver.id).values(swap_mode_enabled=swap_mode)
    )
    await db_session.commit()
    monkeypatch.setattr(
        "backend.app.services.auto_queue_scheduler.preference_options", AsyncMock(return_value=_SwapProfile())
    )
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    if swap_mode:
        assert (result.converted, result.skipped) == (0, [(farm.item.id, "no_eligible_target")])
    else:
        assert (result.converted, result.skipped) == (1, []), result.skipped


async def test_companions_are_judged_by_their_own_creator(db_session, printer_factory, tmp_path, monkeypatch):
    """The moved row keeps its creator (none here), its companions are written by the person who
    pressed the button: only that person's profile asks for the swap start, so the two swap-mode
    P1S would take the converted row and refuse its companion — the move cannot be placed whole."""
    from backend.app.models.user import User

    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=3)  # k = 2
    await db_session.execute(
        Printer.__table__.update().where(Printer.id.in_([p.id for p in farm.receivers])).values(swap_mode_enabled=True)
    )
    user = User(username="planner", password_hash="x", is_active=True)
    db_session.add(user)
    await db_session.commit()

    async def profile(_db, owner, _model):
        return _SwapProfile() if owner is not None else None

    monkeypatch.setattr("backend.app.services.auto_queue_scheduler.preference_options", profile)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True, current_user=user)
    assert (result.converted, result.skipped) == (0, [(farm.item.id, "no_eligible_target")])


async def test_preheat_off_on_both_models_keeps_the_job_home(db_session, printer_factory, tmp_path):
    """Codex r1 T-R1, the reviewer's numbers through the real loader: the global preheat is 1200 s,
    the order says off for both models. Two 100 s jobs on one ready P2S: the first goes now, the
    second ends at 200 s. The P1S plate takes 1000 s — slower, so the job stays. Read with the global
    preheat instead, home looked like 2600 s against 2200 s and the job moved."""
    farm = await petg_farm(
        db_session, printer_factory, tmp_path, home_trays=("PETG",), home_seconds=100, alt_seconds=1000
    )
    choices = {"P2S": _entry("off"), "P1S": _entry("off")}
    farm.item.order_print_options = choices
    farm.item.position = 2
    ahead = AutoQueueItem(
        library_file_id=farm.home_file.id,
        plate_id=1,
        project_id=farm.project.id,
        project_line_id=farm.line.id,
        target_model="P2S",
        required_filament_types=json.dumps(["PETG"]),
        print_time_seconds=100,
        status="pending",
        position=1,
        order_print_options=choices,
    )
    db_session.add(ahead)
    await _preheat_settings(db_session)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert result.converted == 0
    assert result.skipped == [(ahead.id, "home_model_idle"), (farm.item.id, "no_faster_model")]
    assert result.home[farm.item.id] == ("known", 200.0)
