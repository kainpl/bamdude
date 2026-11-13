"""The forecast over real rows: the snapshot reads what the queues hold, the
batch plans targets and everything ranked ahead, and the routes wrap it."""

from datetime import datetime, timedelta

import pytest

from backend.app.api.routes.settings import set_setting
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.settings import Settings
from backend.app.services import farm_forecast
from backend.tests.unit.services.test_product_composition import counting_statements

pytestmark = pytest.mark.integration

H = 3600
NOW = datetime(2026, 9, 6, 12, 0, 0)


async def test_operator_absence_settings_persist_without_touching_printer_queues(committing_client, db_session, farm):
    from sqlalchemy import select

    before = [(q.id, q.status, q.is_paused) for q in (await db_session.scalars(select(PrinterQueue))).all()]
    response = await committing_client.put(
        "/api/v1/settings/",
        json={
            "forecast_operator_absence_enabled": True,
            "forecast_operator_absence_start": "23:00",
            "forecast_operator_absence_end": "08:00",
            "forecast_operator_timezone": "Europe/Kyiv",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["forecast_operator_absence_enabled"] is True
    reread = await committing_client.get("/api/v1/settings/")
    assert reread.json()["forecast_operator_timezone"] == "Europe/Kyiv"
    snapshot = await farm_forecast.load_snapshot(db_session, NOW)
    assert snapshot.operator_hours.start.isoformat() == "23:00:00"
    assert [(q.id, q.status, q.is_paused) for q in (await db_session.scalars(select(PrinterQueue))).all()] == before
    # A partial update must be checked against the persisted other endpoint.
    refused = await committing_client.put("/api/v1/settings/", json={"forecast_operator_absence_end": "23:00"})
    assert refused.status_code == 422
    assert (await committing_client.get("/api/v1/settings/")).json()["forecast_operator_absence_end"] == "08:00"


async def test_held_auto_eject_uses_existing_owner_proof_in_operator_forecast(db_session, farm, monkeypatch):
    import time
    from types import SimpleNamespace

    printer = farm["printers"][0]
    archive = PrintArchive(
        printer_id=printer.id,
        filename="Product-A-synthetic.gcode.3mf",
        file_path="synthetic-only.gcode.3mf",
        file_size=1,
        status="completed",
        extra_data={"dispatch_intent": {"auto_eject": True, "submission_id": "synthetic-run"}},
    )
    db_session.add(archive)
    await db_session.flush()
    printer.awaiting_plate_clear = True
    printer.awaiting_plate_clear_archive_id = archive.id
    await set_setting(db_session, "forecast_operator_absence_enabled", "true")
    await set_setting(db_session, "forecast_operator_timezone", "UTC")
    await db_session.commit()
    monkeypatch.setattr(
        farm_forecast.printer_manager,
        "peek_status",
        lambda _pid: (
            SimpleNamespace(connected=True, state="FINISH", subtask_id="synthetic-run"),
            time.monotonic(),
            False,
        ),
    )
    snap = await farm_forecast.load_snapshot(db_session, datetime(2026, 10, 15, 23))
    assert not next(m for m in snap.printers if m.printer_id == printer.id).awaiting_operator
    assert printer.awaiting_plate_clear is True  # advisory, never clears a real gate
    archive.status = "failed"
    await db_session.commit()
    snap = await farm_forecast.load_snapshot(db_session, datetime(2026, 10, 15, 23))
    assert next(m for m in snap.printers if m.printer_id == printer.id).awaiting_operator


def _sliced(filename: str, model: str) -> LibraryFile:
    """A single-plate sliced 3MF making one hook, an hour long, for ``model``."""
    return LibraryFile(
        filename=filename,
        file_path=filename,
        file_size=1,
        file_type="gcode",
        file_metadata={
            "sliced_for_model": model,
            "print_time_seconds": H,
            "plates": [
                {
                    "index": 1,
                    "printable_objects": {"1": "hook"},
                    "print_time_seconds": H,
                    "filaments": [{"slot_id": 1, "type": "PETG"}],
                }
            ],
        },
    )


@pytest.fixture
async def farm(db_session, printer_factory):
    """Two P1S and one X1C, each with a queue; the hook sliced once per model; one product with both plates."""
    p1 = await printer_factory(name="P1S-1", model="P1S")
    p2 = await printer_factory(name="P1S-2", model="P1S")
    x1 = await printer_factory(name="X1C-1", model="X1C")
    for p in (p1, p2, x1):
        db_session.add(PrinterQueue(id=p.id, printer_id=p.id, status="idle"))
    hook_p1s, hook_x1c = _sliced("hook-p1s.gcode.3mf", "P1S"), _sliced("hook-x1c.gcode.3mf", "X1C")
    product = Product(name="Hook")
    db_session.add_all([hook_p1s, hook_x1c, product])
    await db_session.flush()
    db_session.add(
        ProductPart(
            product_id=product.id, kind="printed", name="hook", name_key="hook", qty_per_unit=1, aliases=["hook"]
        )
    )
    db_session.add_all(
        [
            ProductPlate(product_id=product.id, library_file_id=hook_p1s.id, plate_index=0),
            ProductPlate(product_id=product.id, library_file_id=hook_x1c.id, plate_index=0),
        ]
    )
    # The two v2 allowances default to 120 s and 10 min. The tests below pin the
    # simulation's bare arithmetic, so the fixture switches both off explicitly;
    # the gate tests set what they need themselves.
    db_session.add_all(
        [
            Settings(key="forecast_upload_seconds", value="0"),
            Settings(key="forecast_plate_clear_minutes", value="0"),
        ]
    )
    await db_session.commit()
    return {"printers": (p1, p2, x1), "files": (hook_p1s, hook_x1c), "product": product}


async def _order(db, product_id, quantity, **kw):
    """An active order with one line; returns ``(project_id, line_id)``."""
    project = Project(name=kw.pop("name", "O"), **kw)
    line = ProjectLine(product_id=product_id, quantity=quantity, sort_order=0)
    project.lines.append(line)
    db.add(project)
    await db.flush()
    ids = (project.id, line.id)
    await db.commit()
    return ids


@pytest.mark.asyncio
async def test_the_snapshot_reads_running_queued_and_staged_work(db_session, farm):
    p1, p2, _x1 = farm["printers"]
    hook_p1s, _ = farm["files"]
    db_session.add(
        PrintArchive(
            printer_id=p1.id,
            filename="r",
            file_path="",
            file_size=0,
            status="printing",
            started_at=NOW - timedelta(minutes=30),
            print_time_seconds=H,
        )
    )
    db_session.add(PrintQueueItem(queue_id=p2.id, library_file_id=hook_p1s.id, status="pending"))
    db_session.add(
        AutoQueueItem(library_file_id=hook_p1s.id, status="pending", target_model="P1S", print_time_seconds=2 * H)
    )
    db_session.add(
        AutoQueueItem(
            library_file_id=hook_p1s.id,
            status="pending",
            target_model="P1S",
            print_time_seconds=H,
            assigned_to_item_id=1,
        )
    )
    await db_session.commit()
    snap = await farm_forecast.load_snapshot(db_session, NOW)
    by_id = {m.printer_id: m for m in snap.printers}
    assert by_id[p1.id].running_seconds == 0 and [r.seconds for r in by_id[p1.id].queued] == [1800]
    assert [r.seconds for r in by_id[p2.id].queued] == [H]
    assert [(s.target_model, s.seconds) for s in snap.staged] == [
        ("P1S", 2 * H)
    ]  # the assigned one is somebody's already


@pytest.mark.asyncio
async def test_archived_printers_are_gone_and_parked_ones_owe_but_take_nothing(db_session, farm, printer_factory):
    """Availability decides who may RECEIVE work; only archiving retires a
    machine. A parked printer stays in the snapshot so what it already holds
    keeps dating its orders (Decision 7)."""
    p1, p2, x1 = farm["printers"]
    gone = await printer_factory(name="gone", model="P1S", archived=True)
    off = await printer_factory(name="off", model="P1S", is_active=False)
    db_session.add_all([PrinterQueue(id=gone.id, printer_id=gone.id), PrinterQueue(id=off.id, printer_id=off.id)])
    (await db_session.get(PrinterQueue, x1.id)).is_paused = True
    (await db_session.get(PrinterQueue, p2.id)).auto_distribute_eligible = False
    await db_session.commit()
    snap = await farm_forecast.load_snapshot(db_session, NOW)
    accepts = {m.printer_id: m.accepts_new_work for m in snap.printers}
    assert gone.id not in accepts
    assert accepts[off.id] is False and accepts[x1.id] is False and accepts[p2.id] is False
    assert accepts[p1.id] is True
    # The parked machines remain in the snapshot so held work is not erased,
    # Draft sizing assumes the queue resumes. Forecast placement still excludes it.
    assert {lane.printer_id for lane in farm_forecast.capacity_from_snapshot(snap).machines} == {p1.id, x1.id}


@pytest.mark.asyncio
async def test_forecast_projects_ranks_ahead_and_splits_across_models(db_session, farm):
    product = farm["product"]
    await _order(db_session, product.id, 4, name="U", priority="urgent")
    mine, _line = await _order(db_session, product.id, 3, name="M", priority="normal")
    farm_free, out = await farm_forecast.forecast_projects(db_session, [mine], NOW)
    f = out[mine]
    assert f.ahead_count == 1
    # Three one-hour hooks over two P1S + one X1C: all three start at once.
    assert f.now_seconds == H and f.now_eta == NOW + timedelta(hours=1)
    # After the urgent order's four hooks: 4 + 3 = 7 prints on 3 machines → 3 h.
    assert f.after_seconds == 3 * H
    assert len(f.lines[0].rows) == 2
    assert all(row.proposed_split is None for row in f.lines[0].rows)
    assert {row["model"]: row["prints"] for row in f.by_model} == {"P1S": 2, "X1C": 1}
    assert f.machine_seconds == 3 * H and farm_free.free_seconds == 0


@pytest.mark.asyncio
async def test_an_id_that_names_no_order_is_absent(db_session, farm):
    _farm, out = await farm_forecast.forecast_projects(db_session, [999_999], NOW)
    assert out == {}


@pytest.mark.asyncio
async def test_the_batch_does_not_grow_with_the_number_of_orders(db_session, farm, test_engine):
    product = farm["product"]
    ids = [(await _order(db_session, product.id, 1, name="O0"))[0]]
    with counting_statements(test_engine, match="SELECT") as one:
        await farm_forecast.forecast_projects(db_session, ids, NOW)
    ids += [(await _order(db_session, product.id, 1, name=f"O{i}"))[0] for i in range(1, 4)]
    with counting_statements(test_engine, match="SELECT") as four:
        await farm_forecast.forecast_projects(db_session, ids, NOW)
    assert len(four) <= len(one)
    with counting_statements(test_engine, match="FROM printers") as printers:
        await farm_forecast.forecast_projects(db_session, ids, NOW)
    assert len(printers) == 1  # the plan engine reads no printers — only the snapshot does


@pytest.mark.asyncio
async def test_a_running_print_without_an_estimate_is_counted_on_its_order(db_session, farm):
    """The window between print start and the 3MF attach: the machine is busy for an unknown time,
    and that unknown lands on the order, never on the clock as zero."""
    p1, _p2, _x1 = farm["printers"]
    mine, _line = await _order(db_session, farm["product"].id, 1, name="M")
    db_session.add(
        PrintArchive(
            printer_id=p1.id,
            project_id=mine,
            filename="r",
            file_path="",
            file_size=0,
            status="printing",
            started_at=NOW - timedelta(minutes=5),
        )
    )
    await db_session.commit()
    snap = await farm_forecast.load_snapshot(db_session, NOW)
    head = {m.printer_id: m for m in snap.printers}[p1.id].queued[0]
    assert head.order_id == mine and head.seconds is None
    _farm, out = await farm_forecast.forecast_projects(db_session, [mine], NOW)
    assert out[mine].unknown_prints == 1


@pytest.mark.asyncio
async def test_the_batch_route_answers_per_order_with_the_farm_header(committing_client, db_session, farm):
    product = farm["product"]
    o, _line = await _order(db_session, product.id, 2, name="R")
    r = await committing_client.get(f"/api/v1/projects/forecast?ids={o},{o}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["farm"]["free_seconds"] == 0 and body["farm"]["free_at"].endswith("Z")
    (row,) = body["orders"]  # the duplicate id answers once
    assert row["project_id"] == o and row["now_seconds"] == H and row["now_eta"].endswith("Z")
    assert row["assumptions"] == []
    assert row["machine_seconds"] == 2 * H and row["unknown_prints"] == 0 and row["unroutable_prints"] == 0
    assert row["eta_complete"] is True


@pytest.mark.asyncio
async def test_the_batch_route_refuses_without_ids_and_skips_unknown_ones(committing_client, farm):
    r = await committing_client.get("/api/v1/projects/forecast")
    assert r.status_code == 400 and r.json()["detail"] == "ids is required"
    r = await committing_client.get("/api/v1/projects/forecast?ids=999999")
    assert r.status_code == 200 and r.json()["orders"] == []


@pytest.mark.asyncio
async def test_the_batch_route_refuses_malformed_ids_and_a_page_over_the_cap(committing_client, farm):
    """Every id malformed is «no ids at all»; too many is its own refusal.

    «²» is `isdigit` but not `isdecimal` — `int()` would raise on it and answer
    500 — and an id past int32 is not an id either.
    """
    r = await committing_client.get("/api/v1/projects/forecast?ids=abc,-1,%C2%B2,99999999999")
    assert r.status_code == 400 and r.json()["detail"] == "ids is required"
    over = ",".join(str(i) for i in range(1, 202))
    r = await committing_client.get(f"/api/v1/projects/forecast?ids={over}")
    assert r.status_code == 400 and r.json()["detail"] == "at most 200 ids"


@pytest.mark.asyncio
async def test_a_closed_order_answers_an_empty_forecast(committing_client, db_session, farm):
    """Closed = nothing is planned. The order still EXISTS, so it is in the
    answer — with no dates rather than dropped, which would read as «unknown id»."""
    done, _line = await _order(db_session, farm["product"].id, 3, name="C", status="completed")
    body = (await committing_client.get(f"/api/v1/projects/forecast?ids={done}")).json()
    (row,) = body["orders"]
    assert row["project_id"] == done and row["now_eta"] is None and row["now_seconds"] is None
    assert (
        row["after_eta"] is None
        and row["machine_seconds"] == 0
        and row["eta_complete"] is True
        and row["ahead_count"] == 0
    )
    one = await committing_client.get(f"/api/v1/projects/{done}/forecast")
    assert one.status_code == 200
    assert (
        one.json()["now_eta"] is None
        and one.json()["machine_seconds"] == 0
        and one.json()["eta_complete"] is True
        and one.json()["lines"] == []
    )


@pytest.mark.asyncio
async def test_the_order_route_carries_lines_and_separate_model_rows(committing_client, db_session, farm):
    product = farm["product"]
    o, line_id = await _order(db_session, product.id, 3, name="D")
    body = (await committing_client.get(f"/api/v1/projects/{o}/forecast")).json()
    (line,) = body["lines"]
    assert line["line_id"] == line_id and line["now_seconds"] == H and line["eta_complete"] is True
    assert len(line["rows"]) == 2
    assert all(row["proposed_split"] is None for row in line["rows"])
    assert {row["model"]: row["prints"] for row in body["by_model"]} == {"P1S": 2, "X1C": 1}
    assert (await committing_client.get("/api/v1/projects/999999/forecast")).status_code == 404


@pytest.mark.asyncio
async def test_the_queue_route_matches_the_batch_farm_header(committing_client, db_session, farm):
    p1, _p2, _x1 = farm["printers"]
    db_session.add(PrintQueueItem(queue_id=p1.id, library_file_id=farm["files"][0].id, status="pending"))
    # A print running with no estimate yet — the 3MF has not been attached. It
    # is the reason a farm can read «0m» while a printer is busy, so the header
    # carries the count.
    db_session.add(
        PrintArchive(printer_id=p1.id, filename="r", file_path="", file_size=0, status="printing", started_at=NOW)
    )
    await db_session.commit()
    queue = (await committing_client.get("/api/v1/queue/forecast")).json()
    assert queue["free_seconds"] == H and queue["free_at"].endswith("Z")
    assert queue["unknown_prints"] == 1
    # One row per machine — the «free at» sorts read these. p1 owes the queued
    # hour and carries the estimate-less running print; the other two are free.
    rows = {row["printer_id"]: row for row in queue["printers"]}
    assert set(rows) == {p.id for p in farm["printers"]}
    assert rows[p1.id]["free_seconds"] == H and rows[p1.id]["unknown_prints"] == 1
    assert rows[p1.id]["free_at"].endswith("Z")
    assert all(rows[p.id]["free_seconds"] == 0 and rows[p.id]["unknown_prints"] == 0 for p in farm["printers"][1:])


@pytest.mark.asyncio
async def test_the_snapshot_carries_the_gates(db_session, farm):
    """Plate gap, what the printer awaits now, per-row and default preparation, and the drying line."""
    p1, p2, x1 = farm["printers"]
    hook_p1s, _ = farm["files"]
    await set_setting(db_session, "forecast_upload_seconds", "120")
    await set_setting(db_session, "forecast_plate_clear_minutes", "10")
    p1.awaiting_plate_clear = True  # require_plate_clear defaults to True
    p2.require_plate_clear = False
    x1.swap_mode_enabled = True  # the change-table macro clears the plate for it
    db_session.add(PrintQueueItem(queue_id=p2.id, library_file_id=hook_p1s.id, status="pending"))
    db_session.add(
        PrintQueueItem(queue_id=p2.id, library_file_id=hook_p1s.id, status="pending", preheat_override="off")
    )
    await db_session.commit()
    snapshot = await farm_forecast.load_snapshot(db_session, NOW)
    machines = {m.printer_id: m for m in snapshot.printers}
    assert machines[p1.id].plate_clear_seconds == 600 and machines[p1.id].waiting_seconds == 600
    assert machines[p2.id].plate_clear_seconds == 0 and machines[p2.id].waiting_seconds == 0
    assert machines[x1.id].plate_clear_seconds == 0
    assert [row.prep_seconds for row in machines[p2.id].queued] == [120, 120]  # preheat is off: upload only
    assert not any(row.started for row in machines[p2.id].queued)
    assert snapshot.prep_seconds == 120 and snapshot.stagger is None and snapshot.assumptions == ()
    # The preheat stage rides on top — from its own settings, and per row.
    await set_setting(db_session, "preheat_enabled", "true")
    await db_session.commit()
    snapshot = await farm_forecast.load_snapshot(db_session, NOW)
    assert snapshot.prep_seconds == 120 + 900 + 300
    assert sorted(row.prep_seconds for row in {m.printer_id: m for m in snapshot.printers}[p2.id].queued) == [120, 1320]
    # Drying is a caveat only while it can block the queue.
    await set_setting(db_session, "queue_drying_enabled", "true")
    await db_session.commit()
    assert (await farm_forecast.load_snapshot(db_session, NOW)).assumptions == ()
    await set_setting(db_session, "queue_drying_block", "true")
    await db_session.commit()
    assert (await farm_forecast.load_snapshot(db_session, NOW)).assumptions == ("drying",)


@pytest.mark.asyncio
async def test_the_running_head_is_marked_started(db_session, farm):
    p1, _p2, _x1 = farm["printers"]
    db_session.add(
        PrintArchive(
            printer_id=p1.id,
            filename="r",
            file_path="",
            file_size=0,
            status="printing",
            started_at=NOW - timedelta(minutes=30),
            print_time_seconds=H,
        )
    )
    db_session.add(PrintQueueItem(queue_id=p1.id, library_file_id=farm["files"][0].id, status="pending"))
    await db_session.commit()
    snapshot = await farm_forecast.load_snapshot(db_session, NOW)
    rows = {m.printer_id: m for m in snapshot.printers}[p1.id].queued
    assert [(row.started, row.prep_seconds) for row in rows] == [(True, 0), (False, 0)]


@pytest.mark.asyncio
async def test_the_snapshot_reads_the_stagger_policy(db_session, farm):
    p1, p2, _x1 = farm["printers"]
    assert (await farm_forecast.load_snapshot(db_session, NOW)).stagger is None
    for key, value in (
        ("stagger_enabled", "true"),
        ("stagger_concurrent", "3"),
        ("stagger_interval_minutes", "4"),
        ("stagger_wait_for_bed", "false"),
    ):
        await set_setting(db_session, key, value)
    p1.stagger_interval_minutes = 2
    await db_session.commit()
    snapshot = await farm_forecast.load_snapshot(db_session, NOW)
    policy = snapshot.stagger
    assert policy is not None
    assert (policy.concurrent, policy.interval_seconds, policy.wait_for_bed, policy.live) == (3, 240, False, {})
    assert policy.resolver.groups_for(p1.id) == {(None, None)}
    machines = {m.printer_id: m for m in snapshot.printers}
    assert machines[p1.id].stagger_interval_seconds == 120 and machines[p2.id].stagger_interval_seconds is None


# ---------- WS-13 E1 OR4–OR6: late, the reasons, machine-hours per model ----------


@pytest.mark.asyncio
async def test_a_covered_order_has_no_reason(db_session, farm):
    mine, _line = await _order(db_session, farm["product"].id, 3, name="Covered")
    _farm, out = await farm_forecast.forecast_projects(db_session, [mine], NOW)
    assert out[mine].incomplete_reasons == []


@pytest.mark.asyncio
async def test_a_part_without_a_plate_is_a_reason_counted_in_units(db_session, farm):
    product = farm["product"]
    db_session.add(ProductPart(product_id=product.id, kind="printed", name="cap", name_key="cap", qty_per_unit=2))
    await db_session.commit()
    mine, _line = await _order(db_session, product.id, 3, name="Capless")
    _farm, out = await farm_forecast.forecast_projects(db_session, [mine], NOW)
    assert out[mine].incomplete_reasons == [("no_plate", 6)]
    assert out[mine].eta_complete is True  # the old field keeps its meaning (Q1)


@pytest.mark.asyncio
async def test_a_stopped_plan_is_a_reason_even_with_nothing_unsatisfiable(db_session, farm, monkeypatch):
    from backend.app.services import plan_engine

    monkeypatch.setattr(plan_engine, "MAX_ITERATIONS", 1)
    mine, _line = await _order(db_session, farm["product"].id, 3, name="Stopped")
    _farm, out = await farm_forecast.forecast_projects(db_session, [mine], NOW)
    assert ("truncated", None) in out[mine].incomplete_reasons


def _file(filename: str, model: str, obj: str, seconds: int | None) -> LibraryFile:
    plate = {"index": 1, "printable_objects": {"1": obj}, "filaments": [{"slot_id": 1, "type": "PETG"}]}
    if seconds is not None:
        plate["print_time_seconds"] = seconds
    return LibraryFile(
        filename=filename,
        file_path=filename,
        file_size=1,
        file_type="gcode",
        file_metadata={"sliced_for_model": model, "plates": [plate]},
    )


async def _kit(db, files: list[LibraryFile], parts: tuple[str, ...]) -> Product:
    kit = Product(name="Kit")
    db.add_all([kit, *[f for f in files if f.id is None]])
    await db.flush()
    for name in parts:
        db.add(ProductPart(product_id=kit.id, kind="printed", name=name, name_key=name, qty_per_unit=1, aliases=[name]))
    for f in files:
        db.add(ProductPlate(product_id=kit.id, library_file_id=f.id, plate_index=0))
    await db.commit()
    return kit


@pytest.mark.asyncio
async def test_machine_hours_per_model_and_a_parked_printer(db_session, farm):
    _p1, p2, _x1 = farm["printers"]
    (await db_session.get(PrinterQueue, p2.id)).status = "paused"
    cap_x1c = _file("cap-x1c.gcode.3mf", "X1C", "cap", 2 * H)
    kit = await _kit(db_session, [farm["files"][0], cap_x1c], ("hook", "cap"))
    mine, _line = await _order(db_session, kit.id, 2, name="Kit order")
    _farm, out = await farm_forecast.forecast_projects(db_session, [mine], NOW)
    # One of the two P1S is parked: it still owes its work but takes no new one.
    assert out[mine].by_model == [
        {"model": "X1C", "prints": 2, "seconds": 4 * H, "accepting_printers": 1},
        {"model": "P1S", "prints": 2, "seconds": 2 * H, "accepting_printers": 1},
    ]


@pytest.mark.asyncio
async def test_a_model_whose_row_has_no_estimate_has_no_machine_hours(db_session, farm):
    kit = await _kit(db_session, [_file("cap-x1c.gcode.3mf", "X1C", "cap", None)], ("cap",))
    mine, _line = await _order(db_session, kit.id, 2, name="Timeless")
    _farm, out = await farm_forecast.forecast_projects(db_session, [mine], NOW)
    assert out[mine].by_model == [{"model": "X1C", "prints": 2, "seconds": None, "accepting_printers": 1}]
    assert ("unknown_time", 2) in out[mine].incomplete_reasons


@pytest.mark.asyncio
async def test_a_part_the_fleet_cannot_place_is_not_a_complete_estimate(db_session, farm):
    """A plate the fleet cannot schedule (no estimate) is the only one making «cap»,
    while «hook» has a scheduled plate: the planner used to drop it, and the estimate
    read complete over an uncovered part (WS-13 E1 T3)."""
    kit = await _kit(db_session, [farm["files"][0], _file("cap-x1c.gcode.3mf", "X1C", "cap", None)], ("hook", "cap"))
    mine, _line = await _order(db_session, kit.id, 2, name="Half placed")
    _farm, out = await farm_forecast.forecast_projects(db_session, [mine], NOW)
    assert out[mine].incomplete_reasons == [("unknown_time", 2)]


@pytest.mark.asyncio
async def test_late_is_the_deadlines_own_rule(committing_client, db_session, farm):
    from backend.app.services.order_deadlines import eta_is_late

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    past, _l1 = await _order(
        db_session, farm["product"].id, 3, name="Due yesterday", due_date=today - timedelta(days=1)
    )
    later, _l2 = await _order(db_session, farm["product"].id, 3, name="Due later", due_date=today + timedelta(days=30))
    body = (await committing_client.get(f"/api/v1/projects/forecast?ids={past},{later}")).json()
    rows = {row["project_id"]: row for row in body["orders"]}
    assert rows[past]["late"] is True and rows[later]["late"] is False
    for pid, due in ((past, today - timedelta(days=1)), (later, today + timedelta(days=30))):
        eta = datetime.fromisoformat(rows[pid]["now_eta"].replace("Z", "+00:00")).replace(tzinfo=None)
        assert rows[pid]["late"] is eta_is_late(eta if rows[pid]["eta_complete"] else None, due)
    detail = (await committing_client.get(f"/api/v1/projects/{past}/forecast")).json()
    assert detail["late"] is True and detail["by_model"] and detail["incomplete_reasons"] == []


@pytest.mark.asyncio
async def test_a_plate_for_a_model_the_farm_lacks_is_unroutable(db_session, farm):
    kit = await _kit(db_session, [_file("cap-h2d.gcode.3mf", "H2D", "cap", 2 * H)], ("cap",))
    mine, _line = await _order(db_session, kit.id, 2, name="Nowhere")
    _farm, out = await farm_forecast.forecast_projects(db_session, [mine], NOW)
    assert out[mine].incomplete_reasons == [("unroutable", 2)]
    assert out[mine].by_model == [{"model": "H2D", "prints": 2, "seconds": 4 * H, "accepting_printers": 0}]
