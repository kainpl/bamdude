"""Rebalancing counts the whole tail the router will deal out (spec rebalance-tail-time r2).

The defect: a job the rebalancer may not move — another order's line, an un-filed job, one
aimed at a location, a pinned one, one sourced from an archive, a cooling line's — still
occupies the home printers before the candidate, yet the decision left it out, so the
candidate looked as if it would print at once and stayed. The router sees every one of them
(``_fetch_pending``); only manual-start rows and rows scheduled for later are outside it.

Every scenario runs the real resolver over real 3MFs; only the printers' live state is
patched. The home P2S holds PETG and is ready; the job ahead takes it first, so the
candidate's home finish is after that job's — which a 5000 s alternative on a free P1S beats.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_location import PrinterLocation
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.services import queue_rebalance
from backend.tests.integration.test_rebalance_job_feasibility import (  # noqa: F401 — a fixture
    _live,
    petg_farm,
    tick_scheduler,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

IN_TAIL = ["other_line", "unfiled", "located", "pinned", "archive", "constrained", "due_scheduled", "cooling_line"]
OUTSIDE = ["manual_start", "future_scheduled"]


async def _job_ahead(db_session, farm, kind: str) -> AutoQueueItem:
    """A pending P2S job of the same plate, queued ahead of the candidate (position 0 < 1)."""
    row = AutoQueueItem(
        library_file_id=farm.home_file.id,
        plate_id=1,
        target_model="P2S",
        required_filament_types=json.dumps(["PETG"]),
        print_time_seconds=3600,
        status="pending",
        position=0,
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if kind in ("other_line", "constrained", "cooling_line"):
        project = Project(
            name=f"Ahead {kind}",
            status="active",
            printer_resources={"models": None, "printer_ids": [farm.home.id]} if kind == "constrained" else None,
        )
        db_session.add(project)
        await db_session.flush()
        line = ProjectLine(project_id=project.id, product_id=farm.line.product_id, quantity=1)
        db_session.add(line)
        await db_session.flush()
        row.project_id, row.project_line_id = project.id, line.id
        if kind == "cooling_line":
            row.rebalanced_at = now - timedelta(minutes=1)
            row.rebalanced_from_model = "P1S"
    elif kind == "located":
        place = PrinterLocation(name="Shop", name_key="shop")
        db_session.add(place)
        await db_session.flush()
        await db_session.execute(
            Printer.__table__.update().where(Printer.id == farm.home.id).values(location_id=place.id)
        )
        row.target_location_id = place.id
    elif kind == "pinned":
        row.filament_overrides = json.dumps([{"slot_id": 1, "type": "PETG", "color": "#FFFFFF"}])
    elif kind == "archive":
        archive = PrintArchive(
            filename="pin-home.gcode.3mf",
            file_path=farm.home_file.file_path,
            file_size=farm.home_file.file_size,
            status="completed",
        )
        db_session.add(archive)
        await db_session.flush()
        row.archive_id, row.library_file_id = archive.id, None
    elif kind == "due_scheduled":
        row.scheduled_time = now - timedelta(minutes=5)
    elif kind == "manual_start":
        row.manual_start = True
    elif kind == "future_scheduled":
        row.scheduled_time = now + timedelta(days=1)
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.parametrize("kind", IN_TAIL + OUTSIDE)
async def test_a_job_the_rebalancer_may_not_move_still_holds_the_home_printer(
    db_session, printer_factory, tmp_path, kind
):
    """T-A03 / T-A04 / T-A22: the line button moves only its own line, but the forecast is the
    router's whole tail. A job ahead that the router will route takes the ready home P2S first,
    so the candidate moves to the free P1S; one the router does not see yet (manual start,
    scheduled for tomorrow) leaves the home free, and the candidate stays. The job ahead is
    never touched."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, targets=1, home_trays=("PETG",), alt_seconds=5000)
    ahead = await _job_ahead(db_session, farm, kind)
    before = (ahead.target_model, ahead.library_file_id, ahead.archive_id, ahead.rebalanced_at)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    if kind in IN_TAIL:
        assert (result.converted, result.skipped) == (1, []), kind
    else:
        assert (result.converted, result.skipped) == (0, [(farm.item.id, "home_model_idle")]), kind
    row = (await db_session.execute(select(AutoQueueItem).where(AutoQueueItem.id == ahead.id))).scalar_one()
    assert (row.target_model, row.library_file_id, row.archive_id, row.rebalanced_at) == before


# ---------- T-A25: the forecast's first wave is the router's placement ----------


RED = "#FF0000"


async def _parity_farm(db_session, printer_factory, tmp_path, *, compatible=False):
    """Printers in every state the router ranks or refuses, and pending jobs of every kind.

    P2S: A (PETG, ready), B (PETG, waiting for its plate clear), C (PLA). P1S: D (red PETG,
    ready), E (PETG, busy with a queued print), F (PETG, its queue in error — the router
    places there), G (PETG, behind an HA interlock), H (PETG, queue paused by the operator),
    I (PETG, claimed for a print whose archive does not exist yet), J (PETG, claimed, printing
    past its estimate), K (PETG, an archive printing past its estimate and no claim — free for
    the router), L (PETG, an archive printing within its estimate and no claim — free for the
    router, the snapshot inconsistent). With ``compatible``: X1C M (PETG, ready) and N (PETG,
    waiting for its plate clear), and X1C jobs — one pool the two models share.
    """
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue
    from backend.tests.integration.test_auto_queue_scheduler import _idle_status, _make_printer_with_queue
    from backend.tests.integration.test_rebalance_job_feasibility import _plate_file

    async def printer(name, model):
        return (await _make_printer_with_queue(db_session, printer_factory, name=name, model=model))[0]

    a, b, c = [await printer(f"P2S-{x}", "P2S") for x in "ABC"]
    d, e, f, g, h, i, j, k, fresh = [await printer(f"P1S-{x}", "P1S") for x in "DEFGHIJKL"]
    status = {
        a.id: _idle_status(["PETG"]),
        b.id: _finished_status(["PETG"]),
        c.id: _idle_status(["PLA"]),
        d.id: _idle_status(["PETG"], [RED]),
        **{p.id: _idle_status(["PETG"]) for p in (e, f, g, h, i, j, k, fresh)},
    }
    db_session.add(PrintQueueItem(queue_id=e.id, status="pending", position=1))
    await db_session.execute(PrinterQueue.__table__.update().where(PrinterQueue.id == f.id).values(status="error"))
    await db_session.execute(PrinterQueue.__table__.update().where(PrinterQueue.id == h.id).values(is_paused=True))
    await db_session.execute(
        PrinterQueue.__table__.update().where(PrinterQueue.id.in_((i.id, j.id))).values(status="printing")
    )
    started = datetime.now(timezone.utc).replace(tzinfo=None)
    for holder, elapsed in ((j, 7200), (k, 7200), (fresh, 60)):
        db_session.add(
            PrintArchive(
                filename=f"running-{holder.id}.gcode.3mf",
                file_path="",
                file_size=0,
                status="printing",
                printer_id=holder.id,
                print_time_seconds=3600,
                started_at=started - timedelta(seconds=elapsed),
            )
        )
    files = {
        ("P2S", "PETG"): _plate_file(tmp_path, "p2s-petg.gcode.3mf", "P2S", seconds=3000, hooks=1, filament="PETG"),
        ("P2S", "PLA"): _plate_file(tmp_path, "p2s-pla.gcode.3mf", "P2S", seconds=2000, hooks=1, filament="PLA"),
        ("P1S", "PETG"): _plate_file(tmp_path, "p1s-petg.gcode.3mf", "P1S", seconds=4000, hooks=1, filament="PETG"),
        ("X1C", "PETG"): _plate_file(tmp_path, "x1c-petg.gcode.3mf", "X1C", seconds=2500, hooks=1, filament="PETG"),
    }
    db_session.add_all(files.values())
    place = PrinterLocation(name="Bay", name_key="bay")
    db_session.add(place)
    await db_session.flush()
    await db_session.execute(Printer.__table__.update().where(Printer.id == b.id).values(location_id=place.id))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # (model, filament, position, seconds, extra columns)
    jobs = [
        ("P2S", "PETG", 1, 3000, {}),
        ("P2S", "PETG", 2, 1000, {"target_location_id": place.id}),
        ("P2S", "PETG", 3, 2000, {}),
        ("P2S", "PLA", 4, 2000, {}),
        ("P1S", "PETG", 5, 4000, {}),
        ("P1S", "PETG", 5, 1000, {}),  # the same position: the id decides
        ("P1S", "PETG", 7, None, {}),
        ("P1S", "PETG", 8, 500, {}),
        ("P1S", "PETG", 9, 3000, {}),
        ("P1S", "PETG", 10, 2000, {}),
        ("P1S", "PETG", 11, 3000, {}),
        ("P1S", "PETG", 0, 100, {"manual_start": True}),
        ("P1S", "PETG", 0, 100, {"scheduled_time": now + timedelta(days=1)}),
    ]
    awaiting = {b.id}
    fresh_ids: tuple[int, ...] = ()
    if compatible:
        from backend.app.models.settings import Settings

        m, n = [await printer(f"X1C-{x}", "X1C") for x in "MN"]
        status[m.id] = _idle_status(["PETG"])
        status[n.id] = _finished_status(["PETG"])
        awaiting.add(n.id)
        fresh_ids = (m.id, n.id)
        db_session.add(Settings(key="auto_queue_compatible_models", value="true"))
        jobs += [
            ("X1C", "PETG", 6, 2500, {}),
            ("X1C", "PETG", 3, 6000, {}),
            ("X1C", "PETG", 12, 800, {}),
            ("P1S", "PETG", 2, 7000, {}),
        ]
    for model, filament, position, seconds, extra in jobs:
        db_session.add(
            AutoQueueItem(
                library_file_id=files[(model, filament)].id,
                plate_id=1,
                target_model=model,
                required_filament_types=json.dumps([filament]),
                print_time_seconds=seconds,
                status="pending",
                position=position,
                **extra,
            )
        )
    await db_session.commit()
    # Rewrite the first of the two same-position rows: PostgreSQL stores the new version at the end
    # of its heap, so a router ordering by position alone would hand back the later id first there.
    tied = (
        await db_session.execute(
            select(AutoQueueItem.id).where(AutoQueueItem.position == 5).order_by(AutoQueueItem.id).limit(1)
        )
    ).scalar_one()
    await db_session.execute(
        AutoQueueItem.__table__.update().where(AutoQueueItem.id == tied).values(waiting_reason=None)
    )
    await db_session.commit()
    connected = {p.id for p in (a, b, c, d, e, f, g, h, i, j, k, fresh)} | set(fresh_ids)
    return status, connected, awaiting, g.id


def _finished_status(trays):
    from backend.tests.integration.test_auto_queue_scheduler import _finished_status as finished

    return finished(trays)


async def _first_wave_parity(
    db_session, tick_scheduler, printer_factory, tmp_path, monkeypatch, sjf, compatible, edge=None
):
    """T-A25: on one snapshot, the jobs the forecast deals out now — and the printer each goes
    to — are exactly the ones the router's tick places, with the router's readiness, colour,
    location, queue and compatible-model rules; nothing the router leaves waiting is dealt out
    now. With SJF (T-A26 / T-A07), the jobs the router marks as jumped are the ones the
    forecast's rule marks (``rebalance_replay.jumps``), models sharing one pool included."""
    from unittest.mock import AsyncMock

    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.settings import Settings
    from backend.app.services.ha_sensor_manager import ha_sensor_manager
    from backend.tests.integration.test_auto_queue_scheduler import _patch_printer_manager

    status, connected, awaiting, interlocked = await _parity_farm(
        db_session, printer_factory, tmp_path, compatible=compatible
    )
    if edge is not None:
        # Codex r1 T-R2: a stored 0 sorts before every estimate; a row with no model sorts where
        # the database puts NULL (first on SQLite, last on PostgreSQL).
        row = (
            await db_session.execute(
                select(AutoQueueItem).where(AutoQueueItem.target_model == "P1S", AutoQueueItem.position == 8)
            )
        ).scalar_one()
        if edge == "zero_time":
            row.print_time_seconds = 0
        else:
            row.target_model = None
        await db_session.commit()
    if sjf:
        db_session.add(Settings(key=queue_rebalance.SJF_SETTING_KEY, value="true"))
        await db_session.commit()
    monkeypatch.setattr(ha_sensor_manager, "blocked_printers", AsyncMock(return_value={interlocked: "Door"}))
    p_elig, p_sched = _patch_printer_manager(connected, status, awaiting)
    with p_elig, p_sched:
        ctx = await queue_rebalance._pass_context(
            db_session,
            resources=await queue_rebalance._order_resources(db_session),
            busy=None,
            interlocked=None,
            cache=None,
        )
        forecast = await queue_rebalance._load_forecast(db_session, ctx, [], queue_rebalance._utcnow())
        projection = queue_rebalance.start_projection([], forecast.farm)
        predicted = {}
        for job_id in projection.jobs:
            replayed = projection.result_of(job_id)
            if replayed.routed_at.get(job_id) == 0.0:
                predicted[job_id] = replayed.lane_of[job_id]
        await tick_scheduler.tick()
    rows = (
        await db_session.execute(
            select(PrintQueueItem.source_auto_item_id, PrintQueueItem.queue_id).where(
                PrintQueueItem.source_auto_item_id.is_not(None)
            )
        )
    ).all()
    placed = dict(rows)
    assert len(placed) >= 5, placed  # the farm exercises ranking, not an empty pass
    assert predicted == placed
    if sjf:
        from backend.app.services.rebalance_replay import jumps

        waiting = [job for job_id, job in projection.jobs.items() if job_id not in placed]
        expected = {
            job.job_id
            for job in waiting
            if job.been_jumped or any(jumps(projection.jobs[s], job, peer_jumped=False) for s in placed)
        }
        marked = (
            await db_session.execute(
                select(AutoQueueItem.id).where(
                    AutoQueueItem.id.in_([job.job_id for job in waiting]), AutoQueueItem.been_jumped.is_(True)
                )
            )
        ).scalars()
        assert set(marked) == expected
        assert expected, "the farm exercises the starvation guard"


@pytest.mark.parametrize(
    ("sjf", "compatible"), [(False, False), (True, False), (True, True)], ids=["position", "sjf", "sjf-shared-pool"]
)
async def test_the_forecasts_first_wave_is_the_routers_placement(
    db_session, tick_scheduler, printer_factory, tmp_path, monkeypatch, sjf, compatible
):
    """T-A25 / T-A26 / T-A07 — see :func:`_first_wave_parity`."""
    await _first_wave_parity(db_session, tick_scheduler, printer_factory, tmp_path, monkeypatch, sjf, compatible)


@pytest.mark.parametrize("edge", ["zero_time", "null_model"])
async def test_the_first_wave_holds_on_edge_metadata(
    db_session, tick_scheduler, printer_factory, tmp_path, monkeypatch, edge
):
    """Codex r1 T-R2: a stored 0 sorts before every estimate; a row with no model sorts where
    the database puts NULL — first on SQLite, last on PostgreSQL (bites there)."""
    await _first_wave_parity(db_session, tick_scheduler, printer_factory, tmp_path, monkeypatch, True, True, edge=edge)


# ---------- T-A23 / T-A24: one automatic move per row ----------

#: Six jobs on two exact-model printers, one each, 1:1 plates, (P1S, P2S) minutes per job — a
#: reversal the product's own decision makes without T23 (found by searching ``plan_moves``):
#: pass 1 moves job 2 P1S→P2S (35→29) and job 5 P2S→P1S (65→31); after the cooldown, pass 2
#: would move job 5 back P1S→P2S (51→45), each move strictly better than staying.
REVERSAL_MODELS = ["P1S", "P2S", "P1S", "P1S", "P2S", "P2S"]
REVERSAL_MINUTES = [(20, 2), (4, 9), (15, 20), (9, 9), (20, 20), (2, 16)]
REVERSAL_CANDIDATE_ORDER = [4, 1, 2, 0, 3, 5]


async def _reversal_farm(db_session, printer_factory, tmp_path):
    from backend.app.models.product import Product, ProductPart, ProductPlate
    from backend.app.models.settings import Settings
    from backend.tests.integration.test_auto_queue_scheduler import _idle_status, _make_printer_with_queue
    from backend.tests.integration.test_rebalance_job_feasibility import _plate_file

    p1s = (await _make_printer_with_queue(db_session, printer_factory, name="P1S-1", model="P1S"))[0]
    p2s = (await _make_printer_with_queue(db_session, printer_factory, name="P2S-1", model="P2S"))[0]
    # The model's farm has no upload allowance and no plate service between prints.
    db_session.add_all(
        [Settings(key="forecast_upload_seconds", value="0"), Settings(key="forecast_plate_clear_minutes", value="0")]
    )
    due = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    rows: list[AutoQueueItem] = []
    for job, (model, minutes) in enumerate(zip(REVERSAL_MODELS, REVERSAL_MINUTES, strict=True)):
        files = {
            m: _plate_file(tmp_path, f"job{job}-{m}.gcode.3mf", m, seconds=s * 60, hooks=1, filament="PETG")
            for m, s in zip(("P1S", "P2S"), minutes, strict=True)
        }
        product = Product(name=f"Job {job}")
        db_session.add_all([*files.values(), product])
        await db_session.flush()
        db_session.add(
            ProductPart(
                product_id=product.id, kind="printed", name="hook", name_key="hook", qty_per_unit=1, aliases=["hook"]
            )
        )
        db_session.add_all(
            [ProductPlate(product_id=product.id, library_file_id=f.id, plate_index=0) for f in files.values()]
        )
        # Candidates are taken by order rank: the due dates put them in the reversal's order.
        project = Project(
            name=f"Order {job}",
            status="active",
            due_date=due + timedelta(hours=REVERSAL_CANDIDATE_ORDER.index(job)),
        )
        db_session.add(project)
        await db_session.flush()
        line = ProjectLine(project_id=project.id, product_id=product.id, quantity=1)
        db_session.add(line)
        await db_session.flush()
        row = AutoQueueItem(
            library_file_id=files[model].id,
            plate_id=1,
            project_id=project.id,
            project_line_id=line.id,
            target_model=model,
            required_filament_types=json.dumps(["PETG"]),
            print_time_seconds=minutes[0 if model == "P1S" else 1] * 60,
            status="pending",
            position=job,
        )
        db_session.add(row)
        rows.append(row)
    await db_session.commit()
    status = {p1s.id: _idle_status(["PETG"]), p2s.id: _idle_status(["PETG"])}
    return rows, status, {p1s.id, p2s.id}


async def _models_of(db_session, rows):
    found = (
        await db_session.execute(
            select(AutoQueueItem.id, AutoQueueItem.target_model).where(AutoQueueItem.id.in_([r.id for r in rows]))
        )
    ).all()
    by_id = dict(found)
    return [by_id[row.id] for row in rows]


async def _after_the_cooldown(db_session):
    """Every move of the first pass, dated two days back: no line is cooling any more."""
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
    # Through the session's own objects: one session serves both passes here, where every
    # real pass has its own, and a Core UPDATE would leave the decision reading the old stamp.
    moved = (
        (
            await db_session.execute(
                select(AutoQueueItem)
                .where(AutoQueueItem.rebalanced_at.is_not(None))
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    for row in moved:
        row.rebalanced_at = past
    await db_session.commit()


@pytest.mark.parametrize("second", ["automatic", "manual"])
async def test_an_automatic_move_is_made_once_and_a_manual_one_may_follow(
    db_session, printer_factory, tmp_path, second
):
    """T-A23 / T-A24: after the cooldown, on a farm nothing else changed, the second automatic
    pass converts no row the first one moved — job 5 is not sent back — while every other job
    is still decided as before. A manual pass from the same state may move it back: the
    one-move limit is the automatic pass's, and the manual move keeps its mark."""
    from backend.tests.integration.test_auto_queue_scheduler import _patch_printer_manager

    rows, status, connected = await _reversal_farm(db_session, printer_factory, tmp_path)
    p_elig, p_sched = _patch_printer_manager(connected, status)
    with p_elig, p_sched:
        first = await queue_rebalance.rebalance(db_session)
        assert (first.converted, await _models_of(db_session, rows)) == (
            2,
            ["P1S", "P2S", "P2S", "P1S", "P2S", "P1S"],
        ), first.skipped
        await _after_the_cooldown(db_session)
        if second == "automatic":
            result = await queue_rebalance.rebalance(db_session)
        else:
            result = await queue_rebalance.rebalance(db_session, force=True)
    models = await _models_of(db_session, rows)
    if second == "automatic":
        assert (rows[2].id, "already_rebalanced_auto") in result.skipped
        assert (rows[5].id, "already_rebalanced_auto") in result.skipped
        assert (models[2], models[5]) == ("P2S", "P1S")
    else:
        assert models[5] == "P2S", result.skipped  # sent back: what T23 stops the automatic pass doing
    moved = (await db_session.execute(select(AutoQueueItem).where(AutoQueueItem.id == rows[5].id))).scalar_one()
    await db_session.refresh(moved)
    assert moved.rebalanced_at is not None and moved.rebalanced_from_model


async def test_the_mark_holds_for_companions_legacy_rows_and_a_retry(
    async_client, db_session, printer_factory, tmp_path
):
    """T-A24: the first move marks the original and its companion; a row marked before this
    rule (``rebalanced_from_model`` alone) counts as moved; a Retry clears neither — so the next
    automatic pass, the cooldown long over, converts none of them. A manual pass still may."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=3)  # k = 2 on the P1S
    legacy = AutoQueueItem(
        library_file_id=farm.home_file.id,
        plate_id=1,
        project_id=farm.project.id,
        project_line_id=farm.line.id,
        target_model="P2S",
        required_filament_types=json.dumps(["PETG"]),
        print_time_seconds=3600,
        status="pending",
        position=5,
        rebalanced_from_model="P1S",
    )
    db_session.add(legacy)
    await db_session.commit()
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        first = await queue_rebalance.rebalance(db_session)
        assert (first.converted, first.created) == (1, 1), first.skipped
        assert (legacy.id, "already_rebalanced_auto") in first.skipped
        companion = (
            await db_session.execute(
                select(AutoQueueItem).where(
                    AutoQueueItem.project_line_id == farm.line.id,
                    AutoQueueItem.id.not_in((farm.item.id, legacy.id)),
                )
            )
        ).scalar_one()
        assert companion.rebalanced_at is not None and companion.rebalanced_from_model == "P2S"
        await _after_the_cooldown(db_session)
        await db_session.refresh(farm.item)
        farm.item.status = "failed"
        await db_session.commit()
        r = await async_client.post(f"/api/v1/auto-queue/{farm.item.id}/retry")
        assert r.status_code == 200, r.text
        await db_session.refresh(farm.item)  # the route wrote through the session the test shares
        assert farm.item.status == "pending"
        second = await queue_rebalance.rebalance(db_session)
        assert second.converted == 0
        for row_id in (farm.item.id, companion.id, legacy.id):
            assert (row_id, "already_rebalanced_auto") in second.skipped, second.skipped
        manual = await queue_rebalance.rebalance(db_session, item_ids=[legacy.id], force=True)
    assert manual.converted == 1, manual.skipped
    await db_session.refresh(legacy)
    assert (legacy.target_model, legacy.rebalanced_from_model) == ("P1S", "P2S")
    assert legacy.rebalanced_at is not None


# ---------- T-A16: a move that does not apply ends the pass's plan ----------


async def test_a_move_that_does_not_apply_ends_the_rest_of_the_plan(db_session, printer_factory, tmp_path, monkeypatch):
    """T17: two jobs are planned onto the two P1S; the first one's receiver loses its PETG during
    the copy, so its move is refused. The second was decided in a world where the first had
    moved — it is not made, and says so; nothing of it is written. The next call reads the
    farm again and decides afresh."""
    from backend.tests.integration.test_auto_queue_scheduler import _idle_status
    from backend.tests.integration.test_rebalance_job_feasibility import _during_capture, _job_columns

    farm = await petg_farm(db_session, printer_factory, tmp_path)
    second = AutoQueueItem(
        library_file_id=farm.home_file.id,
        plate_id=1,
        project_id=farm.project.id,
        project_line_id=farm.line.id,
        target_model="P2S",
        required_filament_types=json.dumps(["PETG"]),
        print_time_seconds=3600,
        status="pending",
        position=2,
    )
    db_session.add(second)
    await db_session.commit()
    before = _job_columns(second)
    first_receiver = farm.receivers[0]
    status = dict(farm.status)

    async def take_out():
        status[first_receiver.id] = _idle_status(["PLA"])

    _during_capture(monkeypatch, take_out)
    p_elig, p_sched = _live(farm, status=status)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert result.converted == 0
    assert result.skipped == [
        (farm.item.id, "eligibility_changed"),
        (second.id, "plan_invalidated_after_apply_failure"),
    ]
    await db_session.refresh(second)
    after = _job_columns(second)
    assert after == before and second.rebalanced_at is None
    p_elig, p_sched = _live(farm, status=status)
    with p_elig, p_sched:
        again = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert again.converted == 1, again.skipped


async def test_a_pass_out_of_budget_writes_nothing(db_session, printer_factory, tmp_path, monkeypatch):
    """T-A17 / T20: with no budget left the candidate answers ``forecast_budget`` — never the old
    under-estimate — and nothing is written: not the row, not its queue, not a cooldown."""
    from backend.app.models.printer_queue import PrinterQueue
    from backend.tests.integration.test_rebalance_job_feasibility import _job_columns

    farm = await petg_farm(db_session, printer_factory, tmp_path)
    before = _job_columns(farm.item)
    monkeypatch.setattr(queue_rebalance, "REBALANCE_WORK_BUDGET", 1)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert (result.converted, result.skipped) == (0, [(farm.item.id, "forecast_budget")])
    await db_session.refresh(farm.item)
    assert _job_columns(farm.item) == before and farm.item.rebalanced_at is None
    paused = (await db_session.execute(select(PrinterQueue.is_paused, PrinterQueue.status))).all()
    assert {tuple(row) for row in paused} == {(False, "idle")}


async def test_companions_are_filed_after_every_pending_row_the_replay_does_not_see(
    db_session, printer_factory, tmp_path
):
    """T-A29 / T14: a manual-start row and one scheduled for tomorrow are outside the router's
    view, yet the writer files the companions after them — so does the forecast's trial."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, alt_hooks=3)  # k = 2
    for position, extra in (
        (99, {"scheduled_time": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)}),
        (120, {"manual_start": True}),
    ):
        db_session.add(
            AutoQueueItem(
                library_file_id=farm.home_file.id,
                plate_id=1,
                target_model="P1S",
                required_filament_types=json.dumps(["PETG"]),
                print_time_seconds=600,
                status="pending",
                position=position,
                **extra,
            )
        )
    await db_session.commit()
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        ctx = await queue_rebalance._pass_context(
            db_session,
            resources=await queue_rebalance._order_resources(db_session),
            busy=None,
            interlocked=set(),
            cache=None,
        )
        forecast = await queue_rebalance._load_forecast(
            db_session, ctx, [(farm.item, "p2s")], queue_rebalance._utcnow()
        )
        assert forecast.farm.max_position == 120
        assert {job.item_id for job in forecast.farm.tail} == set()  # neither row is in the router's view
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert (result.converted, result.created) == (1, 1), result.skipped
    companion = (
        await db_session.execute(
            select(AutoQueueItem).where(AutoQueueItem.project_line_id == farm.line.id, AutoQueueItem.id != farm.item.id)
        )
    ).scalar_one()
    assert companion.position > 120


# ---------- T-A11, wired: the farm's staggered start reaches the forecast ----------


@pytest.mark.parametrize("staggered", [False, True], ids=["free-starts", "one-start-per-5-min"])
async def test_the_farms_staggered_start_dates_the_second_target_print(
    db_session, printer_factory, tmp_path, staggered
):
    """The home P2S has PETG and 1000 s left of a running print; the job takes 3600 s there:
    home ends at 4600 s. Moved, it is two 4400 s prints on the two free P1S. Started together
    they end at 4400 s and the job moves; with one start per five minutes for the whole farm
    (Settings → staggered start) the second starts at 300 s, ends at 4700 s, and it stays.
    No upload allowance and no plate service, so the numbers are the prints' own."""
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.models.settings import Settings
    from backend.tests.integration.test_auto_queue_scheduler import _drying_status

    farm = await petg_farm(db_session, printer_factory, tmp_path, home_trays=("PETG",), alt_seconds=4400, alt_hooks=3)
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
            started_at=now - timedelta(seconds=2600),
        )
    )
    settings = [
        Settings(key="forecast_upload_seconds", value="0"),
        Settings(key="forecast_plate_clear_minutes", value="0"),
    ]
    if staggered:
        settings += [
            Settings(key="stagger_enabled", value="true"),
            Settings(key="stagger_concurrent", value="1"),
            Settings(key="stagger_interval_minutes", value="5"),
            Settings(key="stagger_wait_for_bed", value="false"),
        ]
    db_session.add_all(settings)
    await db_session.commit()
    p_elig, p_sched = _live(farm, status={**farm.status, farm.home.id: _drying_status(["PETG"])})
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    if staggered:
        assert (result.converted, result.skipped) == (0, [(farm.item.id, "no_faster_model")])
    else:
        assert (result.converted, result.created, result.skipped) == (1, 1, [])


async def test_the_decision_runs_off_the_loop_and_leaves_the_collector_alone(
    db_session, printer_factory, tmp_path, monkeypatch
):
    """T21 / T22: the replay and the plans run in a worker thread — never on the event loop's — and
    the cyclic collector stays as the process has it (Codex r1 T-R4: pausing it is no pass's call)."""
    import gc
    import threading

    farm = await petg_farm(db_session, printer_factory, tmp_path)
    seen = []
    for name in ("start_projection", "plan_moves"):
        real = getattr(queue_rebalance, name)

        def watched(*args, _real=real, **kwargs):
            seen.append((threading.current_thread() is threading.main_thread(), gc.isenabled()))
            return _real(*args, **kwargs)

        monkeypatch.setattr(queue_rebalance, name, watched)
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert result.converted == 1
    assert seen and all(entry == (False, True) for entry in seen), seen
    assert gc.isenabled()


@pytest.mark.parametrize("sjf", [False, True], ids=["position", "sjf"])
async def test_the_router_breaks_every_tie_by_id(db_session, printer_factory, tmp_path, sjf):
    """T8: rows the router's keys cannot tell apart come in id order — the replay's last key too.
    Before, a tie had no order: PostgreSQL hands back its heap, and rewriting a row moves it to
    the end. (SQLite returns rowid order either way; this test bites on PostgreSQL.)"""
    farm = await petg_farm(db_session, printer_factory, tmp_path)
    await db_session.execute(AutoQueueItem.__table__.delete())
    for _ in range(12):
        db_session.add(
            AutoQueueItem(
                library_file_id=farm.home_file.id,
                plate_id=1,
                target_model="P2S",
                required_filament_types=json.dumps(["PETG"]),
                print_time_seconds=600,
                status="pending",
                position=3,
            )
        )
    await db_session.commit()
    ids = sorted((await db_session.execute(select(AutoQueueItem.id))).scalars())
    await db_session.execute(
        AutoQueueItem.__table__.update().where(AutoQueueItem.id.in_(ids[::2])).values(waiting_reason=None)
    )
    await db_session.commit()
    from backend.app.services.auto_queue_scheduler import AutoQueueScheduler

    fetched = [row.id for row in await AutoQueueScheduler()._fetch_pending(db_session, sjf)]
    assert fetched == ids


async def test_a_printer_mid_dispatch_is_dated_and_does_not_stop_the_farm(db_session, printer_factory, tmp_path):
    """Final review C1/I3: a dispatch takes the printer's claim before its archive exists, so for
    the whole upload the printer is busy with nothing in the forecast's snapshot. Read as an
    unknown release, it made every job that could reach it — and, with one start group, every
    job on the farm — unforecastable. It is its own estimate plus the preparation: a third P1S
    is mid-dispatch, another order's P1S jobs outnumber the free P1S (one waits, and could go to
    it), starts are staggered, and the PETG job still moves to a P1S."""
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.models.settings import Settings
    from backend.tests.integration.test_auto_queue_scheduler import _drying_status, _make_printer_with_queue

    farm = await petg_farm(db_session, printer_factory, tmp_path)
    busy = (await _make_printer_with_queue(db_session, printer_factory, name="P1S-3", model="P1S"))[0]
    await db_session.execute(
        PrinterQueue.__table__.update().where(PrinterQueue.id == busy.id).values(status="printing")
    )
    db_session.add(
        PrintQueueItem(queue_id=busy.id, status="printing", position=0, library_file_id=farm.alt_file.id, plate_id=1)
    )
    other = Project(name="Other", status="active")
    db_session.add(other)
    await db_session.flush()
    # More of them than free P1S: one still waits when the dispatching printer's release is asked.
    db_session.add_all(
        [
            AutoQueueItem(
                library_file_id=farm.alt_file.id,
                plate_id=1,
                project_id=other.id,
                target_model="P1S",
                required_filament_types=json.dumps(["PETG"]),
                print_time_seconds=2000,
                status="pending",
                position=0,
            )
            for _ in range(3)
        ]
    )
    db_session.add_all(
        [
            Settings(key="stagger_enabled", value="true"),
            Settings(key="stagger_concurrent", value="2"),
            Settings(key="stagger_interval_minutes", value="1"),
        ]
    )
    await db_session.commit()
    status = {**farm.status, busy.id: _drying_status(["PETG"])}
    p_elig, p_sched = _live(farm, connected={farm.home.id, busy.id, *(p.id for p in farm.receivers)}, status=status)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert (result.converted, result.skipped) == (1, []), result.skipped


async def test_the_result_carries_every_candidates_home_forecast(db_session, printer_factory, tmp_path):
    """Final review M1, for the explanation (package D): a candidate answered before the catalog —
    here a ready home that takes the job now — still has its home forecast in the result."""
    farm = await petg_farm(db_session, printer_factory, tmp_path, home_trays=("PETG",))
    p_elig, p_sched = _live(farm)
    with p_elig, p_sched:
        result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
    assert result.skipped == [(farm.item.id, "home_model_idle")]
    state, finish = result.home[farm.item.id]
    assert state == "known" and finish > 3600
