"""Rebalancing asks whether a printer can print THIS job, not whether its model is free.

Spec: vault ``60-specs/rebalance-job-feasibility-spec.md`` (r2 + C3-1). The
defect it fixes: a free home printer that cannot take the job — a P2S holding
PLA and ABS in front of a PETG plate — answered ``home_model_idle`` for the
whole model, and its "free now" stood in for the home's finish time, so fifteen
idle P1S with PETG and a sliced P1S plate never got the work.

Every scenario runs the real resolver over real 3MFs; only the printers' live
state (their trays, connection and idleness) is patched, through the same
``printer_manager`` the router and the rebalancer both read.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.settings import Settings
from backend.app.services import queue_rebalance
from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
from backend.app.services.farm_forecast import model_key
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.integration.test_auto_queue_scheduler import (
    _idle_status,
    _make_printer_with_queue,
    _patch_printer_manager,
)

pytestmark = pytest.mark.integration

WHITE = "#FFFFFF"


@pytest.fixture
async def tick_scheduler(monkeypatch, db_session):
    """An AutoQueueScheduler whose tick runs on the test session."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.services.auto_queue_scheduler.async_session", _session_ctx)
    return AutoQueueScheduler()


def _plate_file(tmp_path, filename: str, model: str, *, seconds: int, hooks: int, filament: str) -> LibraryFile:
    """A sliced single-plate file for ``model``: ``hooks`` hooks in ``seconds``, one ``filament`` channel."""
    path = write_routing_3mf(
        tmp_path / filename,
        {1: [{"id": 1, "type": filament, "color": WHITE, "used_g": "1"}]},
        model=model,
        prediction=seconds,
    )
    return LibraryFile(
        filename=filename,
        file_path=str(path),
        file_size=path.stat().st_size,
        file_type="gcode",
        file_metadata={
            "sliced_for_model": model,
            "print_time_seconds": seconds,
            "plates": [
                {
                    "index": 1,
                    "printable_objects": {str(i + 1): ("hook" if i == 0 else f"hook_{i + 1}") for i in range(hooks)},
                    "print_time_seconds": seconds,
                    "filaments": [{"slot_id": 1, "type": filament}],
                }
            ],
        },
    )


async def petg_farm(
    db_session,
    printer_factory,
    tmp_path,
    *,
    home_trays: tuple[str, ...] = ("PLA", "ABS"),
    target_trays: tuple[str, ...] = ("PETG",),
    targets: int = 2,
    home_seconds: int = 3600,
    alt_seconds: int = 2000,
    alt_hooks: int = 6,
    with_alternative: bool = True,
    home_model: str = "P2S",
    target_model: str = "P1S",
):
    """The reported farm, small: one idle P2S holding ``home_trays``, ``targets`` idle P1S
    holding ``target_trays``, and a 6-hook order line with one pending PETG print sliced
    for the P2S — plus, unless told otherwise, the same hooks sliced for the P1S. The two
    models can be renamed (``home_model`` / ``target_model``) for a compatibility case.

    Returns a namespace: home, receivers, status (printer id → live status), home_file,
    alt_file, project, line, item.
    """
    home, _home_q = await _make_printer_with_queue(db_session, printer_factory, name="P2S_C_33", model=home_model)
    receivers = [
        (await _make_printer_with_queue(db_session, printer_factory, name=f"P1S-{i + 1}", model=target_model))[0]
        for i in range(targets)
    ]
    home_file = _plate_file(tmp_path, "pin-home.gcode.3mf", home_model, seconds=home_seconds, hooks=6, filament="PETG")
    files = [home_file]
    alt_file = None
    if with_alternative:
        alt_file = _plate_file(
            tmp_path, "pin-alt.gcode.3mf", target_model, seconds=alt_seconds, hooks=alt_hooks, filament="PETG"
        )
        files.append(alt_file)
    product = Product(name="Pin")
    db_session.add_all([*files, product])
    await db_session.flush()
    db_session.add(
        ProductPart(
            product_id=product.id, kind="printed", name="hook", name_key="hook", qty_per_unit=1, aliases=["hook"]
        )
    )
    db_session.add_all([ProductPlate(product_id=product.id, library_file_id=f.id, plate_index=0) for f in files])
    project = Project(name="Pin Bumblebee", status="active")
    db_session.add(project)
    await db_session.flush()
    line = ProjectLine(project_id=project.id, product_id=product.id, quantity=6)
    db_session.add(line)
    await db_session.flush()
    item = AutoQueueItem(
        library_file_id=home_file.id,
        plate_id=1,
        project_id=project.id,
        project_line_id=line.id,
        target_model=home_model,
        required_filament_types=json.dumps(["PETG"]),
        print_time_seconds=home_seconds,
        status="pending",
        position=1,
    )
    db_session.add(item)
    await db_session.commit()
    status = {home.id: _idle_status(list(home_trays), [WHITE] * len(home_trays))}
    for printer in receivers:
        status[printer.id] = _idle_status(list(target_trays), [WHITE] * len(target_trays))
    return SimpleNamespace(
        home=home,
        receivers=receivers,
        status=status,
        home_file=home_file,
        alt_file=alt_file,
        project=project,
        line=line,
        item=item,
    )


def _live(farm, *, connected: set[int] | None = None, status: dict | None = None, awaiting: set[int] | None = None):
    ids = connected if connected is not None else {farm.home.id, *(p.id for p in farm.receivers)}
    return _patch_printer_manager(ids, status if status is not None else farm.status, awaiting)


# ---------- §1 RED: the cause, on every door ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("alt_seconds", [2000, 4500], ids=["target-faster", "target-slower-than-home-print"])
async def test_a_free_home_that_cannot_print_the_job_does_not_hold_it(
    db_session, printer_factory, tmp_path, alt_seconds
):
    """A01 + the ETA trap. The idle P2S holds PLA/ABS, the job needs PETG: it is no home
    for this job, neither as «a machine of its model is free» nor as «home is free now».
    With the P1S plate SLOWER than the home print (4500 s > 3600 s) only a home ETA that
    ignores the unfit P2S lets the move happen — removing the idle veto alone is not enough."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_seconds=alt_seconds)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert result.skipped == []
    assert (result.converted, result.moved_parts) == (1, 6)
    await db_session.refresh(farm.item)
    assert model_key(farm.item.target_model) == model_key("P1S")
    assert farm.item.library_file_id == farm.alt_file.id
    assert farm.item.rebalanced_from_model == "P2S"


@pytest.mark.asyncio
async def test_the_tick_moves_the_job_the_router_could_not_place(db_session, tick_scheduler, printer_factory, tmp_path):
    """A02, the tick: the router refuses the PETG job on the PLA/ABS P2S (it stays pending),
    and the rebalancing pass after it moves the job onto the P1S plate."""
    farm = await petg_farm(db_session, printer_factory, tmp_path)
    db_session.add(Settings(key="auto_queue_rebalance_models", value="true"))
    await db_session.commit()
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        await tick_scheduler.tick()
    await db_session.refresh(farm.item)
    assert farm.item.status == "pending"
    assert model_key(farm.item.target_model) == model_key("P1S")
    assert farm.item.rebalanced_from_model == "P2S"


@pytest.mark.asyncio
async def test_the_line_button_moves_the_job(committing_client, db_session, printer_factory, tmp_path):
    """A02, an order line's button: the same rule, only the cooldown and the setting set aside."""
    farm = await petg_farm(db_session, printer_factory, tmp_path)
    project_id, line_id, item_id = farm.project.id, farm.line.id, farm.item.id
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        r = await committing_client.post(f"/api/v1/projects/{project_id}/lines/{line_id}/rebalance")
    assert r.status_code == 200, r.text
    assert r.json()["skipped"] == [] and r.json()["converted"] == 1
    moved = (await db_session.execute(select(AutoQueueItem).where(AutoQueueItem.id == item_id))).scalar_one()
    await db_session.refresh(moved)
    assert model_key(moved.target_model) == model_key("P1S")


@pytest.mark.asyncio
async def test_the_panel_button_moves_the_job(async_client, db_session, printer_factory, tmp_path):
    """A02, the AutoQueue panel's per-item action."""
    farm = await petg_farm(db_session, printer_factory, tmp_path)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        r = await async_client.post("/api/v1/auto-queue/rebalance", json={"item_ids": [farm.item.id]})
    assert r.status_code == 200, r.text
    assert r.json()["skipped"] == [] and r.json()["converted"] == 1
    await db_session.refresh(farm.item)
    assert model_key(farm.item.target_model) == model_key("P1S")


# ---------- §1 negative controls ----------


@pytest.mark.asyncio
async def test_a_free_home_that_can_print_the_job_keeps_it(db_session, printer_factory, tmp_path):
    """A03: give the idle P2S PETG and it is the home again — the router is about to place it."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, home_trays=("PETG",))
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert result.skipped == [(farm.item.id, "home_model_idle")]
    await db_session.refresh(farm.item)
    assert farm.item.target_model == "P2S"


@pytest.mark.asyncio
async def test_a_receiver_that_cannot_print_the_job_takes_nothing(db_session, printer_factory, tmp_path):
    """A05: the P1S hold PLA too — nobody can print PETG, so nothing moves."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, target_trays=("PLA",))
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert result.converted == 0
    assert result.skipped == [(farm.item.id, "no_eligible_target")]
    await db_session.refresh(farm.item)
    assert farm.item.target_model == "P2S"


@pytest.mark.asyncio
async def test_without_an_alternative_plate_nothing_moves(db_session, printer_factory, tmp_path):
    """A11: no P1S plate of the line — the job and its bytes stay."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, with_alternative=False)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert result.converted == 0
    assert result.skipped == [(farm.item.id, "no_eligible_target")]
    await db_session.refresh(farm.item)
    assert (farm.item.target_model, farm.item.library_file_id) == ("P2S", farm.home_file.id)


# ---------- §2: the three answers, offline and the organisation's rules (A10) ----------


def _unread_status() -> SimpleNamespace:
    """A connected, idle printer whose first full report has not arrived: its feed is unknown."""
    return SimpleNamespace(state="IDLE", raw_data={})


def _finished(trays: tuple[str, ...]) -> SimpleNamespace:
    status = _idle_status(list(trays), [WHITE] * len(trays))
    status.state = "FINISH"
    return status


def _job_columns(item: AutoQueueItem) -> dict:
    return {column.key: getattr(item, column.key) for column in AutoQueueItem.__table__.columns}


@pytest.mark.asyncio
async def test_an_unreadable_job_source_stays_without_a_cooldown(db_session, printer_factory, tmp_path):
    """R7: the job's own source cannot be read — this job alone stays, and nothing is stamped."""
    farm = await petg_farm(db_session, printer_factory, tmp_path)
    Path(farm.home_file.file_path).unlink()
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id])
    assert result.skipped == [(farm.item.id, "source_unreadable")]
    await db_session.refresh(farm.item)
    assert (farm.item.target_model, farm.item.rebalanced_at) == ("P2S", None)


@pytest.mark.asyncio
async def test_a_ready_home_with_an_unread_feed_holds_the_job_until_it_reports(db_session, printer_factory, tmp_path):
    """R8: the P2S is free but has not reported its trays — the router may place the job there in
    seconds, so it is not moved away on a guess; once the feed is read (PLA/ABS) it moves, with no
    cooldown in between (both passes run WITHOUT force)."""
    farm = await petg_farm(db_session, printer_factory, tmp_path)
    p_elig, p_sched = _live(farm, status={**farm.status, farm.home.id: _unread_status()})
    with p_elig, p_sched:
        first = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id])
    assert first.skipped == [(farm.item.id, "eligibility_unknown")]
    await db_session.refresh(farm.item)
    assert farm.item.rebalanced_at is None
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        second = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id])
    assert (second.converted, second.skipped) == (1, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["awaiting_plate_clear", "busy"])
async def test_an_unread_home_that_is_not_ready_holds_nothing(db_session, printer_factory, tmp_path, why):
    """R8: unknown on a P2S that is not free now — waiting for its plate clear, or printing — gives
    no veto, no home time and no skip: the fit P1S takes the job."""
    farm = await petg_farm(db_session, printer_factory, tmp_path)
    awaiting: set[int] = set()
    if why == "awaiting_plate_clear":
        awaiting = {farm.home.id}
        status = {**farm.status, farm.home.id: SimpleNamespace(state="FINISH", raw_data={})}
    else:
        queue = await db_session.get(PrinterQueue, farm.home.id)
        queue.status = "printing"
        await db_session.commit()
        status = {**farm.status, farm.home.id: _unread_status()}
    p_elig, p_sched = _live(farm, status=status, awaiting=awaiting)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert (result.converted, result.skipped) == (1, [])


@pytest.mark.asyncio
async def test_an_offline_home_neither_holds_nor_dates_the_job(db_session, printer_factory, tmp_path):
    """R7/O4: the P2S holds PETG but is off — it is not waited for; even the slower P1S plate wins."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, home_trays=("PETG",), alt_seconds=4500)
    p_elig, p_sched = _live(farm, connected={p.id for p in farm.receivers})
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert (result.converted, result.skipped) == (1, [])


@pytest.mark.asyncio
async def test_an_offline_or_unready_receiver_gives_no_capacity(db_session, printer_factory, tmp_path):
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    [p1s] = farm.receivers
    p_elig, p_sched = _live(farm, connected={farm.home.id})
    with p_elig, p_sched:
        offline = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert offline.skipped == [(farm.item.id, "no_eligible_target")]
    p_elig, p_sched = _live(farm, awaiting={p1s.id}, status={**farm.status, p1s.id: _finished(("PETG",))})
    with p_elig, p_sched:
        gated = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert gated.skipped == [(farm.item.id, "no_eligible_target")]


@pytest.mark.asyncio
async def test_a_receiver_whose_last_print_failed_takes_no_job_that_asks_for_success(
    db_session, printer_factory, tmp_path
):
    """R6: ``require_previous_success`` is the job's own condition, asked of every receiver."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    [p1s] = farm.receivers
    db_session.add(
        PrintQueueItem(
            queue_id=p1s.id,
            status="failed",
            position=1,
            completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    farm.item.require_previous_success = True
    await db_session.commit()
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert result.skipped == [(farm.item.id, "no_eligible_target")]


# ---------- A08 / A21: the decision is the shared resolver's, under each job's own policy ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("feed_policy", "use_ams", "external", "moves"),
    [
        ("external_only", False, None, False),  # PETG only in the AMS
        ("external_only", False, "PETG", True),
        ("ams_only", True, None, True),
    ],
)
async def test_the_feed_policy_is_asked_of_every_receiver(
    db_session, printer_factory, tmp_path, feed_policy, use_ams, external, moves
):
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    [p1s] = farm.receivers
    farm.item.feed_policy = feed_policy
    farm.item.use_ams = use_ams
    await db_session.commit()
    status = dict(farm.status)
    if external:
        status[p1s.id] = _idle_status(["PETG"], [WHITE])
        status[p1s.id].raw_data["vt_tray"] = [
            {"id": 254, "tray_type": external, "tray_color": WHITE, "tray_info_idx": ""}
        ]
    p_elig, p_sched = _live(farm, status=status)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert (result.converted == 1) is moves, result.skipped


@pytest.mark.asyncio
@pytest.mark.parametrize("strict_first", [True, False])
async def test_a_strict_colour_is_never_answered_by_a_loose_copys_verdict(
    db_session, printer_factory, tmp_path, strict_first
):
    """A05 + A21: the P1S hold BLACK PETG. A copy of the job with a forced white colour cannot go
    there; the loose job can — in ONE pass over the same snapshots, whichever is asked first."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=2)
    black = {p.id: _idle_status(["PETG"], ["#000000"]) for p in farm.receivers}
    loose = farm.item
    strict = AutoQueueItem(
        library_file_id=farm.home_file.id,
        plate_id=1,
        project_id=farm.project.id,
        project_line_id=farm.line.id,
        target_model="P2S",
        required_filament_types=json.dumps(["PETG"]),
        print_time_seconds=3600,
        status="pending",
        position=0 if strict_first else 2,
        force_color_match=True,
    )
    db_session.add(strict)
    await db_session.commit()
    p_elig, p_sched = _live(farm, status={**farm.status, **black})
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, item_ids=[loose.id, strict.id], force=True)
    assert result.converted == 1
    assert result.skipped == [(strict.id, "no_eligible_target")]
    await db_session.refresh(loose)
    assert model_key(loose.target_model) == model_key("P1S")


@pytest.mark.asyncio
async def test_one_jobs_previous_success_condition_is_not_another_jobs(db_session, printer_factory, tmp_path):
    """A21: the receiver's last print failed. The job that asks for success cannot go there; its
    copy that does not ask can — the printer's verdict is shared, the job's condition is not."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=2)
    for printer in farm.receivers:
        db_session.add(
            PrintQueueItem(
                queue_id=printer.id,
                status="failed",
                position=1,
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
    picky = AutoQueueItem(
        library_file_id=farm.home_file.id,
        plate_id=1,
        project_id=farm.project.id,
        project_line_id=farm.line.id,
        target_model="P2S",
        required_filament_types=json.dumps(["PETG"]),
        print_time_seconds=3600,
        status="pending",
        position=0,
        require_previous_success=True,
    )
    db_session.add(picky)
    await db_session.commit()
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, item_ids=[farm.item.id, picky.id], force=True)
    assert result.converted == 1
    assert result.skipped == [(picky.id, "no_eligible_target")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("setting", "stays"), [("false", False), ("true", True)])
async def test_a_compatible_printer_is_a_home_only_with_the_farm_setting(
    db_session, printer_factory, tmp_path, setting, stays
):
    """A09: a P1S job, the P1S holds PLA; an idle P1P holds PETG. With compatible models OFF the P1P
    is nobody's home and the job moves to the A1 mini plate; ON, the P1P keeps it."""
    farm = await petg_farm(
        db_session, printer_factory, tmp_path, home_model="P1S", target_model="A1MINI", home_trays=("PLA",)
    )
    p1p, _queue = await _make_printer_with_queue(db_session, printer_factory, name="P1P-1", model="P1P")
    db_session.add(Settings(key="auto_queue_compatible_models", value=setting))
    await db_session.commit()
    status = {**farm.status, p1p.id: _idle_status(["PETG"], [WHITE])}
    connected = {farm.home.id, p1p.id, *(p.id for p in farm.receivers)}
    p_elig, p_sched = _live(farm, connected=connected, status=status)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    if stays:
        assert result.skipped == [(farm.item.id, "home_model_idle")]
    else:
        assert (result.converted, result.skipped) == (1, [])


# ---------- A16: deciding touches nothing ----------


def _forbidden(name):
    def call(*_args, **_kwargs):
        raise AssertionError(f"rebalancing called printer_manager.{name}")

    return call


@pytest.mark.asyncio
async def test_a_refusal_writes_nothing_and_commands_nothing(db_session, printer_factory, tmp_path, monkeypatch):
    from backend.app.models.archive import PrintArchive
    from backend.app.services.printer_manager import printer_manager

    farm = await petg_farm(db_session, printer_factory, tmp_path, target_trays=("PLA",))
    before = _job_columns(farm.item)
    for name in ("start_print", "stop_print"):
        monkeypatch.setattr(printer_manager, name, _forbidden(name))
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert result.skipped == [(farm.item.id, "no_eligible_target")]
    await db_session.refresh(farm.item)
    assert _job_columns(farm.item) == before
    assert (await db_session.execute(select(PrintQueueItem))).scalars().all() == []
    assert (await db_session.execute(select(PrintArchive))).scalars().all() == []
    queues = (await db_session.execute(select(PrinterQueue))).scalars().all()
    assert all(not q.is_paused and q.status != "paused" for q in queues)


# ---------- A19 / C3-1: a Home Assistant interlock is not readiness ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(("alt_seconds", "moves"), [(2000, True), (4500, False)])
async def test_an_interlocked_home_that_can_print_the_job_does_not_hold_it(
    db_session, printer_factory, tmp_path, alt_seconds, moves
):
    """The P2S holds PETG but a sensor holds the printer: no ``home_model_idle``. Its forecast still
    dates the home (like a plate clear), so a faster P1S plate wins and a slower one does not."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, home_trays=("PETG",), alt_seconds=alt_seconds)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(
            db_session, line_ids=[farm.line.id], force=True, interlocked_printers={farm.home.id}
        )
    if moves:
        assert (result.converted, result.skipped) == (1, [])
    else:
        assert result.skipped == [(farm.item.id, "no_faster_model")]


@pytest.mark.asyncio
async def test_an_interlocked_receiver_gives_no_capacity(db_session, printer_factory, tmp_path):
    farm = await petg_farm(db_session, printer_factory, tmp_path)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(
            db_session, line_ids=[farm.line.id], force=True, interlocked_printers={p.id for p in farm.receivers}
        )
    assert result.skipped == [(farm.item.id, "no_eligible_target")]


@pytest.mark.asyncio
async def test_an_interlocked_home_with_an_unread_feed_does_not_hold_the_job(db_session, printer_factory, tmp_path):
    """R8 is for a home the router could use now; an interlocked one is not that."""
    farm = await petg_farm(db_session, printer_factory, tmp_path)
    p_elig, p_sched = _live(farm, status={**farm.status, farm.home.id: _unread_status()})
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(
            db_session, line_ids=[farm.line.id], force=True, interlocked_printers={farm.home.id}
        )
    assert (result.converted, result.skipped) == (1, [])


@pytest.mark.asyncio
async def test_a_manual_door_reads_the_interlocks_once_and_a_failed_read_is_not_an_empty_one(
    db_session, printer_factory, tmp_path, monkeypatch
):
    from backend.app.services.ha_sensor_manager import ha_sensor_manager

    farm = await petg_farm(db_session, printer_factory, tmp_path, home_trays=("PETG",))
    reads = AsyncMock(return_value={farm.home.id: "Door"})
    monkeypatch.setattr(ha_sensor_manager, "blocked_printers", reads)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert reads.await_count == 1
    assert (result.converted, result.skipped) == (1, []), "the Door interlock made the P2S not ready"
    monkeypatch.setattr(ha_sensor_manager, "blocked_printers", AsyncMock(side_effect=RuntimeError("db down")))
    with p_elig, p_sched, pytest.raises(RuntimeError):
        await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [False, True], ids=["empty-set", "home-blocked"])
async def test_the_tick_hands_its_own_interlock_snapshot_to_the_rebalance(
    db_session, tick_scheduler, printer_factory, tmp_path, monkeypatch, blocked
):
    """One HA read per tick: the router's. An empty set is a snapshot, never a reason to read again."""
    from backend.app.services.ha_sensor_manager import ha_sensor_manager

    farm = await petg_farm(db_session, printer_factory, tmp_path)
    db_session.add(Settings(key="auto_queue_rebalance_models", value="true"))
    await db_session.commit()
    reads = AsyncMock(return_value={farm.home.id: "Door"} if blocked else {})
    monkeypatch.setattr(ha_sensor_manager, "blocked_printers", reads)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        await tick_scheduler.tick()
    assert reads.await_count == 1
    await db_session.refresh(farm.item)
    assert model_key(farm.item.target_model) == model_key("P1S")


# ---------- A14: what the move was decided on moves while the target file is copied ----------


@pytest.fixture
def clean_spool():
    from backend.app.services import queue_sources

    queue_sources._reset_state()
    yield queue_sources
    queue_sources._reset_state()


def _during_capture(monkeypatch, change):
    """Run ``change()`` after the plan, inside the copy — the gap the final check exists for."""
    real = queue_rebalance.capture_staged

    async def capture(request):
        await change()
        return await real(request)

    monkeypatch.setattr(queue_rebalance, "capture_staged", capture)


def _staging_files(queue_sources) -> list:
    root = queue_sources.staging_root()
    return list(root.glob("*")) if root.exists() else []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["feed", "busy", "policy", "assigned", "source", "queue_paused", "queue_error", "auto_off", "inactive"]
)
async def test_evidence_that_moves_during_the_copy_stops_the_move_before_any_write(
    db_session, printer_factory, tmp_path, monkeypatch, clean_spool, change
):
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    [p1s] = farm.receivers
    status = dict(farm.status)
    before = _job_columns(farm.item)

    async def apply_change():
        if change == "feed":
            status[p1s.id] = _idle_status(["PLA"], [WHITE])  # the PETG spool was taken out
        elif change == "busy":
            db_session.add(PrintQueueItem(queue_id=p1s.id, status="pending", position=1))
            await db_session.commit()
        elif change == "policy":
            await db_session.execute(
                AutoQueueItem.__table__.update()
                .where(AutoQueueItem.id == farm.item.id)
                .values(allow_base_material_match=False)
            )
            await db_session.commit()
        elif change == "assigned":
            await db_session.execute(
                AutoQueueItem.__table__.update().where(AutoQueueItem.id == farm.item.id).values(status="assigned")
            )
            await db_session.commit()
        elif change in ("queue_paused", "queue_error", "auto_off"):
            values = {
                "queue_paused": {"is_paused": True},
                "queue_error": {"status": "error"},
                "auto_off": {"auto_distribute_eligible": False},
            }[change]
            await db_session.execute(PrinterQueue.__table__.update().where(PrinterQueue.id == p1s.id).values(**values))
            await db_session.commit()
        elif change == "inactive":
            from backend.app.models.printer import Printer

            await db_session.execute(Printer.__table__.update().where(Printer.id == p1s.id).values(is_active=False))
            await db_session.commit()
        else:  # the alternative plate was re-sliced for PLA while it was being copied
            write_routing_3mf(
                Path(farm.alt_file.file_path),
                {1: [{"id": 1, "type": "PLA", "color": WHITE, "used_g": "1"}]},
                model="P1S",
                prediction=2000,
            )

    _during_capture(monkeypatch, apply_change)
    p_elig, p_sched = _live(farm, status=status)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    expected = "already_assigned" if change == "assigned" else "eligibility_changed"
    assert (result.converted, result.skipped) == (0, [(farm.item.id, expected)])
    await db_session.refresh(farm.item)
    after = _job_columns(farm.item)
    for column in ("status", "allow_base_material_match"):
        after.pop(column)
        before.pop(column, None)
    assert after == {k: v for k, v in before.items() if k in after}, "the row changed before the check"
    assert farm.item.rebalanced_at is None, "a refused move must not start the cooldown"
    assert _staging_files(clean_spool) == [], "the copy that was not used is given back"


@pytest.mark.asyncio
async def test_the_next_pass_reads_the_farm_again(db_session, printer_factory, tmp_path, monkeypatch, clean_spool):
    """A14: refused because the spool was taken out mid-copy; put back, the next pass moves the job."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    [p1s] = farm.receivers
    status = dict(farm.status)

    async def take_out():
        status[p1s.id] = _idle_status(["PLA"], [WHITE])

    real = queue_rebalance.capture_staged
    _during_capture(monkeypatch, take_out)
    p_elig, p_sched = _live(farm, status=status)
    with p_elig, p_sched:
        first = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id])
    assert first.skipped == [(farm.item.id, "eligibility_changed")]
    monkeypatch.setattr(queue_rebalance, "capture_staged", real)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        second = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id])
    assert (second.converted, second.skipped) == (1, [])


# ---------- R14: one read per distinct source, not per copy ----------


@pytest.mark.asyncio
async def test_copies_of_one_job_read_their_source_once(db_session, printer_factory, tmp_path, monkeypatch):
    """358 copies of one job must not read the same bytes 358 times (spec R14): one read per
    distinct accepted source and plate, whatever the number of rows."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1, with_alternative=False)
    for position in range(2, 6):
        db_session.add(
            AutoQueueItem(
                library_file_id=farm.home_file.id,
                plate_id=1,
                project_id=farm.project.id,
                project_line_id=farm.line.id,
                target_model="P2S",
                required_filament_types=json.dumps(["PETG"]),
                print_time_seconds=3600,
                status="pending",
                position=position,
            )
        )
    await db_session.commit()
    real = queue_rebalance.read_item_requirements
    reads = []

    async def counting(db, item, cache=None):
        reads.append(item.id)
        return await real(db, item, cache)

    monkeypatch.setattr(queue_rebalance, "read_item_requirements", counting)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert len(result.skipped) == 5
    assert len(reads) == 1, reads


# ---------- A04 / A06 on the real resolver (both reproduce the defect on the base) ----------


async def _busy_home(db_session, printer_factory, *, trays=("PETG",), running_seconds=7200):
    """A second P2S, printing for ``running_seconds`` and holding ``trays``."""
    from backend.app.models.archive import PrintArchive

    printer, queue = await _make_printer_with_queue(db_session, printer_factory, name="P2S_43", model="P2S")
    queue.status = "printing"
    db_session.add(
        PrintArchive(
            printer_id=printer.id,
            status="printing",
            print_time_seconds=running_seconds,
            filename="running",
            file_path="",
            file_size=0,
        )
    )
    await db_session.commit()
    return printer, _idle_status(list(trays), [WHITE] * len(trays))


@pytest.mark.asyncio
@pytest.mark.parametrize(("alt_seconds", "moves"), [(2000, True), (20000, False)])
async def test_an_unfit_free_home_and_a_fit_busy_one_date_the_home_by_the_busy_one(
    db_session, printer_factory, tmp_path, alt_seconds, moves
):
    """A04: the free P2S holds PLA/ABS, the busy P2S holds PETG and frees in 2 h. Home is the busy
    one's 2 h + 1 h: a 2000 s P1S plate wins, a 20000 s one loses — the unfit free P2S decides
    neither (on the base it answered ``home_model_idle`` for both)."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_seconds=alt_seconds)
    busy, busy_status = await _busy_home(db_session, printer_factory)
    status = {**farm.status, busy.id: busy_status}
    connected = {farm.home.id, busy.id, *(p.id for p in farm.receivers)}
    p_elig, p_sched = _live(farm, connected=connected, status=status)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    if moves:
        assert (result.converted, result.skipped) == (1, [])
    else:
        assert result.skipped == [(farm.item.id, "no_faster_model")]


@pytest.mark.asyncio
async def test_an_unprintable_favourite_plate_does_not_hide_a_printable_one(db_session, printer_factory, tmp_path):
    """A06: the line has two P1S plates; the plan engine's favourite (faster) needs ASA, which no
    P1S holds. The job moves onto the slower PETG plate — on the base it was converted onto the
    ASA plate nobody can print. (The PLA/ABS P2S is off here, so the base's model-wide veto does
    not hide the plate choice.)"""
    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_seconds=2000)
    busy, busy_status = await _busy_home(db_session, printer_factory, trays=("PLA",))
    asa = _plate_file(tmp_path, "pin-asa.gcode.3mf", "P1S", seconds=1000, hooks=6, filament="ASA")
    db_session.add(asa)
    await db_session.flush()
    product_id = (
        await db_session.execute(select(ProjectLine.product_id).where(ProjectLine.id == farm.line.id))
    ).scalar_one()
    db_session.add(ProductPlate(product_id=product_id, library_file_id=asa.id, plate_index=0))
    await db_session.commit()
    connected = {busy.id, *(p.id for p in farm.receivers)}
    p_elig, p_sched = _live(farm, connected=connected, status={**farm.status, busy.id: busy_status})
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert (result.converted, result.skipped) == (1, [])
    await db_session.refresh(farm.item)
    assert farm.item.library_file_id == farm.alt_file.id


# ---------- A05 / A08: channels, nozzle and profile are the resolver's ----------


def _status_with(trays, *, nozzle=None):
    """A live status with explicit trays ``(type, colour, profile id)`` and, optionally, the nozzle fitted."""
    status = SimpleNamespace(
        state="IDLE",
        raw_data={
            "ams": [
                {
                    "id": 0,
                    "tray": [
                        {"id": i, "tray_type": t, "tray_color": c, "tray_info_idx": idx}
                        for i, (t, c, idx) in enumerate(trays)
                    ],
                }
            ],
            "vt_tray": [],
            "ams_extruder_map": {},
        },
    )
    if nozzle is not None:
        status.raw_data["device"] = {"nozzle": {"info": [{"id": 0, "diameter": nozzle}]}}
    return status


async def _rewrite_plates(farm, filaments, *, settings=None):
    """Slice both of the farm's plates again with ``filaments`` (and project ``settings``)."""
    for file in (farm.home_file, farm.alt_file):
        write_routing_3mf(
            Path(file.file_path),
            {1: filaments},
            model="P2S" if file is farm.home_file else "P1S",
            prediction=3600 if file is farm.home_file else 2000,
            settings=settings,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("receiver_trays", "moves"), [(("PETG",), False), (("PETG", "PLA"), True)])
async def test_a_two_filament_job_needs_both_on_the_receiver(
    db_session, printer_factory, tmp_path, receiver_trays, moves
):
    """A05/A08: the plate uses PETG and PLA; a P1S holding only PETG has too few channels."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    await _rewrite_plates(
        farm,
        [
            {"id": 1, "type": "PETG", "color": WHITE, "used_g": "1"},
            {"id": 2, "type": "PLA", "color": WHITE, "used_g": "1"},
        ],
    )
    [p1s] = farm.receivers
    status = {**farm.status, p1s.id: _status_with([(t, WHITE, "") for t in receiver_trays])}
    p_elig, p_sched = _live(farm, status=status)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    if moves:
        assert (result.converted, result.skipped) == (1, [])
    else:
        assert result.skipped == [(farm.item.id, "no_eligible_target")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("fitted", "moves"), [(0.6, False), (0.4, True)])
async def test_the_receivers_nozzle_must_be_the_plates(db_session, printer_factory, tmp_path, fitted, moves):
    """A08: the plates are sliced for a 0.4 mm nozzle; a P1S with 0.6 mm fitted takes nothing."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    await _rewrite_plates(
        farm, [{"id": 1, "type": "PETG", "color": WHITE, "used_g": "1"}], settings={"nozzle_diameter": ["0.4"]}
    )
    [p1s] = farm.receivers
    status = {
        farm.home.id: _status_with([("PLA", WHITE, ""), ("ABS", WHITE, "")], nozzle=0.4),
        p1s.id: _status_with([("PETG", WHITE, "")], nozzle=fitted),
    }
    p_elig, p_sched = _live(farm, status=status)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    if moves:
        assert (result.converted, result.skipped) == (1, [])
    else:
        assert result.skipped == [(farm.item.id, "no_eligible_target")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("allow_base", "moves"), [(True, True), (False, False)])
async def test_another_profile_of_the_material_follows_the_jobs_base_material_choice(
    db_session, printer_factory, tmp_path, allow_base, moves
):
    """A08: the plate names one PETG profile, the P1S hold another. «Any PETG» moves; a job that
    asked for its exact profile does not."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1)
    await _rewrite_plates(farm, [{"id": 1, "type": "PETG", "color": WHITE, "used_g": "1", "tray_info_idx": "GFG02"}])
    farm.item.allow_base_material_match = allow_base
    await db_session.commit()
    [p1s] = farm.receivers
    status = {**farm.status, p1s.id: _status_with([("PETG", WHITE, "GFG00")])}
    p_elig, p_sched = _live(farm, status=status)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    if moves:
        assert (result.converted, result.skipped) == (1, [])
    else:
        assert result.skipped == [(farm.item.id, "no_eligible_target")]


@pytest.mark.asyncio
async def test_a_job_another_run_moved_during_the_copy_is_not_moved_twice(
    db_session, printer_factory, tmp_path, monkeypatch, clean_spool
):
    """R15 (review I1): the tick is copying the target file while a button's run moves the same job.
    The second conversion must not happen — it would create another k − 1 companions and overwrite
    where the job came from."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1, alt_hooks=2)
    stamp = datetime(2026, 10, 28, 9, 0)

    async def the_other_run():
        await db_session.execute(
            AutoQueueItem.__table__.update()
            .where(AutoQueueItem.id == farm.item.id)
            .values(
                library_file_id=farm.alt_file.id,
                target_model="P1S",
                rebalanced_at=stamp,
                rebalanced_from_model="P2S",
            )
        )
        await db_session.commit()

    _during_capture(monkeypatch, the_other_run)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert (result.converted, result.created, result.skipped) == (0, 0, [(farm.item.id, "eligibility_changed")])
    rows = (await db_session.execute(select(AutoQueueItem))).scalars().all()
    assert len(rows) == 1
    await db_session.refresh(farm.item)
    assert (farm.item.rebalanced_at, farm.item.rebalanced_from_model) == (stamp, "P2S")


@pytest.mark.asyncio
async def test_plates_of_the_jobs_own_model_are_never_read_as_alternatives(
    db_session, printer_factory, tmp_path, monkeypatch
):
    """Review m4: the decision never asks about a plate of the job's home model, so its file is not
    parsed for it either."""
    farm = await petg_farm(db_session, printer_factory, tmp_path)
    home_plate = (
        await db_session.execute(select(ProductPlate.id).where(ProductPlate.library_file_id == farm.home_file.id))
    ).scalar_one()
    asked = []
    real = queue_rebalance._FitOracle.add_plate

    def add_plate(self, plate_id, requirements):
        asked.append(plate_id)
        return real(self, plate_id, requirements)

    monkeypatch.setattr(queue_rebalance._FitOracle, "add_plate", add_plate)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert result.converted == 1
    assert home_plate not in asked and asked
