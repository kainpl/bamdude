"""Filament needs over real rows: the plan's plates, the order's queue, the shelf of either backend."""

import pytest

from backend.app.models.color_catalog import ColorCatalogEntry
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.schemas.auto_queue import AutoQueueItemCreate
from backend.app.services import filament_needs
from backend.app.services.auto_queue_add import add_items_to_auto_queue
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.unit.services.test_product_composition import counting_statements

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _forget_the_spoolman_shelf():
    """The Spoolman shelf is memoised MODULE-WIDE for 30 s, so it outlives a test.

    Without this, the first test to reach Spoolman decides what every later one
    sees — including the dead-Spoolman test, which would read the live answer
    the previous test cached and never observe its own failure.
    """
    filament_needs.reset_spoolman_shelf_cache()
    yield
    filament_needs.reset_spoolman_shelf_cache()


def _sliced(filename: str, filaments: list[dict]) -> LibraryFile:
    return LibraryFile(
        filename=filename,
        file_path=filename,
        file_size=1,
        file_type="gcode",
        file_metadata={
            "sliced_for_model": "P1S",
            "print_time_seconds": 3600,
            "plates": [
                {"index": 1, "printable_objects": {"1": "hook"}, "print_time_seconds": 3600, "filaments": filaments}
            ],
        },
    )


@pytest.fixture
async def shelf(db_session, tmp_path):
    """A hook of 10 g PETG + 2 g PLA support per print; one product; spools of both."""
    hook = _sliced(
        "hook.gcode.3mf",
        [
            {"slot_id": 1, "type": "PETG", "color": "#000000", "used_g": 10.0},
            {"slot_id": 2, "type": "PLA", "color": "#ffffff", "used_g": 2.0},
        ],
    )
    hook.file_path = str(
        write_routing_3mf(
            tmp_path / hook.filename,
            {
                1: [
                    {"id": f["slot_id"], "type": f["type"], "color": f.get("color"), "used_g": f.get("used_g", 1)}
                    for f in hook.file_metadata["plates"][0]["filaments"]
                ]
            },
            model="P1S",
        )
    )
    product = Product(name="Hook")
    db_session.add_all([hook, product])
    await db_session.flush()
    db_session.add(
        ProductPart(
            product_id=product.id, kind="printed", name="hook", name_key="hook", qty_per_unit=1, aliases=["hook"]
        )
    )
    db_session.add(ProductPlate(product_id=product.id, library_file_id=hook.id, plate_index=0))
    db_session.add_all(
        [
            Spool(material="PETG", color_name="Black", rgba="000000FF", label_weight=1000, weight_used=200),
            Spool(material="PETG", color_name="White", rgba="FFFFFFFF", label_weight=1000, weight_used=0),
            Spool(material="PLA", color_name=None, rgba="FFFFFFFF", label_weight=500, weight_used=100),
            Spool(
                material="PETG",
                color_name="Black",
                label_weight=1000,
                weight_used=0,
                archived_at=__import__("datetime").datetime(2026, 1, 1),
            ),
        ]
    )
    db_session.add(ColorCatalogEntry(manufacturer="Bambu Lab", color_name="White", hex_color="#FFFFFF"))
    await db_session.commit()
    return {"file": hook, "product": product}


async def _order(db, product_id, quantity, *, colour=None, material=None, status="active", name="O"):
    project = Project(name=name, status=status)
    line = ProjectLine(product_id=product_id, quantity=quantity, sort_order=0, color=colour, material=material)
    project.lines.append(line)
    db.add(project)
    await db.flush()
    ids = (project.id, line.id)
    await db.commit()
    return ids


@pytest.mark.asyncio
async def test_need_per_key_with_the_line_colour_and_the_shelf_beside(db_session, shelf):
    pid, _ = await _order(db_session, shelf["product"].id, 5, colour="black")
    out = await filament_needs.needs_of_orders(db_session, [pid])
    rows = {(r.material, r.colour): r for r in out[pid].rows}
    petg = rows[("PETG", "black")]
    assert (petg.need_g, petg.have_g, petg.have_type_g, petg.short_g) == (
        50.0,
        800.0,
        1800.0,
        0.0,
    )  # the archived spool is not stock
    # The support is the lighter filament: the colour is not its, and the shelf
    # answers by the type (spec 2026-09-16 §4) - no shortage of "black PLA".
    pla = rows[("PLA", None)]
    assert (pla.need_g, pla.have_g, pla.have_type_g, pla.short_g) == (10.0, 400.0, 400.0, 0.0)
    assert out[pid].unknown_prints == 0 and out[pid].stock_unavailable is False


@pytest.mark.asyncio
async def test_a_nameless_spool_counts_by_hex_through_the_catalogue(db_session, shelf):
    pid, _ = await _order(db_session, shelf["product"].id, 1, colour="white", material="PLA")
    out = await filament_needs.needs_of_orders(db_session, [pid])
    rows = {(r.material, r.colour): r for r in out[pid].rows}
    assert rows[("PLA", "white")].have_g == 400.0  # PLA spool has no name; FFFFFF → «White» in the catalogue


@pytest.mark.asyncio
async def test_the_lines_material_aims_the_colour_at_its_own_filament(db_session, shelf):
    """PLA ordered in black: the 2 g support is the line's filament, the 10 g PETG body is not."""
    pid, _ = await _order(db_session, shelf["product"].id, 5, colour="black", material="PLA")
    out = await filament_needs.needs_of_orders(db_session, [pid])
    rows = {(r.material, r.colour): r for r in out[pid].rows}
    assert set(rows) == {("PLA", "black"), ("PETG", None)}
    assert (rows[("PLA", "black")].need_g, rows[("PLA", "black")].have_g, rows[("PLA", "black")].short_g) == (
        10.0,
        0.0,
        10.0,
    )
    assert (rows[("PETG", None)].need_g, rows[("PETG", None)].have_g) == (50.0, 1800.0)


@pytest.mark.asyncio
async def test_a_queued_row_reads_the_lines_material_too(db_session, shelf, printer_factory):
    """The queue path carries the material the same way the plan path does: two
    ordered and one queued, so the plan's print and the queue's row each add 2 g
    of PLA under the colour and 10 g of PETG by type."""
    p = await printer_factory(name="P1", model="P1S")
    db_session.add(PrinterQueue(id=p.id, printer_id=p.id, status="idle"))
    pid, line_id = await _order(db_session, shelf["product"].id, 2, colour="black", material="PLA")
    db_session.add(
        PrintQueueItem(
            queue_id=p.id, library_file_id=shelf["file"].id, status="pending", project_id=pid, project_line_id=line_id
        )
    )
    await db_session.commit()
    out = await filament_needs.needs_of_orders(db_session, [pid])
    rows = {(r.material, r.colour): r for r in out[pid].rows}
    assert set(rows) == {("PLA", "black"), ("PETG", None)}
    assert rows[("PLA", "black")].need_g == 4.0 and rows[("PETG", None)].need_g == 20.0


@pytest.mark.asyncio
async def test_pending_queue_rows_of_the_order_are_need_too(db_session, shelf, printer_factory):
    p = await printer_factory(name="P1", model="P1S")
    db_session.add(PrinterQueue(id=p.id, printer_id=p.id, status="idle"))
    pid, line_id = await _order(db_session, shelf["product"].id, 2)
    db_session.add(
        PrintQueueItem(
            queue_id=p.id, library_file_id=shelf["file"].id, status="pending", project_id=pid, project_line_id=line_id
        )
    )
    await db_session.commit()
    out = await filament_needs.needs_of_orders(db_session, [pid])
    rows = {(r.material, r.colour): r for r in out[pid].rows}
    # The plan subtracts the queued print (1 left to plan) and the queue row adds its own grams back: 2 × 10 g.
    assert rows[("PETG", None)].need_g == 20.0


@pytest.mark.asyncio
async def test_auto_queue_rows_of_the_order_are_need_too(db_session, shelf):
    """C1: «whole plan to queue» with no printer picked writes AUTO rows, and they are still need.

    The plan engine subtracts a pending, unassigned ``auto_queue_items`` row
    exactly as it subtracts a ``print_queue`` one, so a loader that read only
    the per-printer tier watched the need collapse to the plan's remainder the
    moment the button was pressed — with the spools untouched and no flag.
    """
    pid, line_id = await _order(db_session, shelf["product"].id, 2)
    await add_items_to_auto_queue(
        db_session,
        AutoQueueItemCreate(library_file_id=shelf["file"].id, project_id=pid, project_line_id=line_id, quantity=1),
        None,
    )
    out = await filament_needs.needs_of_orders(db_session, [pid])
    rows = {(r.material, r.colour): r for r in out[pid].rows}
    # 1 print still to plan (10 g) + the auto row's own 10 g — not the 10 g the
    # plan alone would report after the engine took the auto row off it.
    assert rows[("PETG", None)].need_g == 20.0
    assert rows[("PLA", None)].need_g == 4.0  # the support filament rides along, both prints


@pytest.mark.asyncio
async def test_a_queue_row_with_a_line_but_no_order_id_counts_for_its_order(db_session, shelf, printer_factory):
    """I4: the engine's LINE branch filters on ``project_line_id`` ALONE — so must the loader.

    A row may carry a line without an order id (the engine's own docstring says
    naming the order there would drop real work). Filtering the loader by order
    id subtracted such a row from the plan and never added it back.
    """
    p = await printer_factory(name="P1", model="P1S")
    db_session.add(PrinterQueue(id=p.id, printer_id=p.id, status="idle"))
    pid, line_id = await _order(db_session, shelf["product"].id, 2)
    db_session.add(
        PrintQueueItem(
            queue_id=p.id, library_file_id=shelf["file"].id, status="pending", project_id=None, project_line_id=line_id
        )
    )
    await db_session.commit()
    out = await filament_needs.needs_of_orders(db_session, [pid])
    rows = {(r.material, r.colour): r for r in out[pid].rows}
    assert rows[("PETG", None)].need_g == 20.0


@pytest.mark.asyncio
async def test_needs_grams_of_farm_is_the_same_arithmetic_with_no_shelf_read(db_session, shelf, monkeypatch):
    """The forecast engine's input: every active order's grams, merged, and NOT a Spoolman round trip."""
    await _order(db_session, shelf["product"].id, 3, colour="black")
    await _order(db_session, shelf["product"].id, 2)  # no colour -> material-only key
    await _order(db_session, shelf["product"].id, 9, status="completed")  # closed orders need nothing

    async def _no_shelf(db):  # pragma: no cover - the assertion is that this is never reached
        raise AssertionError("needs_grams_of_farm must not read the shelf")

    monkeypatch.setattr(filament_needs, "load_stock", _no_shelf)
    needs = await filament_needs.needs_grams_of_farm(db_session)

    assert needs.grams[filament_needs.NeedKey("PETG", "black")] == pytest.approx(30.0)
    assert needs.grams[filament_needs.NeedKey("PETG", None)] == pytest.approx(20.0)
    # The PLA support never takes the line colour (spec 2026-09-16 4), so both orders PLA
    # lands on one colourless key: 3 x 2 g under the black line + 2 x 2 g under the other.
    assert needs.grams[filament_needs.NeedKey("PLA", None)] == pytest.approx(10.0)
    assert filament_needs.NeedKey("PLA", "black") not in needs.grams

    # And it is the very arithmetic the order pages show.
    monkeypatch.undo()
    farm = await filament_needs.needs_of_farm(db_session)
    by_key = {filament_needs.NeedKey(r.material, r.colour): r.need_g for r in farm.rows}
    for key, grams in needs.grams.items():
        assert by_key[key] == pytest.approx(round(grams, 1))


@pytest.mark.asyncio
async def test_a_closed_order_answers_no_rows_and_an_unknown_id_is_absent(db_session, shelf):
    pid, _ = await _order(db_session, shelf["product"].id, 3, status="completed")
    out = await filament_needs.needs_of_orders(db_session, [pid, 999_999])
    assert out[pid].rows == [] and 999_999 not in out


@pytest.mark.asyncio
async def test_spoolman_answers_and_a_dead_spoolman_says_so(db_session, shelf, monkeypatch):
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    await db_session.commit()
    pid, _ = await _order(db_session, shelf["product"].id, 1, colour="black")

    class _Client:
        async def get_all_spools(self):
            return [
                {
                    "id": 1,
                    "remaining_weight": 350.0,
                    "archived": False,
                    "filament": {"material": "PETG", "name": "PETG Black", "color_hex": "000000"},
                },
                {
                    "id": 2,
                    "remaining_weight": 100.0,
                    "archived": True,
                    "filament": {"material": "PETG", "name": "PETG Black", "color_hex": "000000"},
                },
            ]

    async def _client():
        return _Client()

    monkeypatch.setattr(filament_needs, "get_spoolman_client", _client)
    out = await filament_needs.needs_of_orders(db_session, [pid])
    petg = {(r.material, r.colour): r for r in out[pid].rows}[("PETG", "black")]
    assert (petg.have_g, petg.have_type_g) == (350.0, 350.0)  # the archived Spoolman spool is not stock

    class _Dead:
        async def get_all_spools(self):
            raise RuntimeError("connection refused")

    async def _dead():
        return _Dead()

    monkeypatch.setattr(filament_needs, "get_spoolman_client", _dead)
    # The answer above is memoised for 30 s; forget it, or this half reads it.
    filament_needs.reset_spoolman_shelf_cache()
    out = await filament_needs.needs_of_orders(db_session, [pid])
    assert out[pid].stock_unavailable is True
    petg = {(r.material, r.colour): r for r in out[pid].rows}[("PETG", "black")]
    assert petg.need_g == 10.0 and petg.have_g is None and petg.short_g is None


@pytest.mark.asyncio
async def test_the_spoolman_shelf_is_read_once_per_ttl(db_session, shelf, monkeypatch):
    """I6: a dead Spoolman costs three 5 s connects — that price is paid once per 30 s, not per request."""
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    await db_session.commit()
    pid, _ = await _order(db_session, shelf["product"].id, 1, colour="black")

    calls = []

    class _Counting:
        async def get_all_spools(self):
            calls.append(1)
            return []

    async def _client():
        return _Counting()

    monkeypatch.setattr(filament_needs, "get_spoolman_client", _client)
    await filament_needs.needs_of_orders(db_session, [pid])
    await filament_needs.needs_of_orders(db_session, [pid])
    assert len(calls) == 1  # the second request read the memo, not the network

    filament_needs.reset_spoolman_shelf_cache()
    await filament_needs.needs_of_orders(db_session, [pid])
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_no_active_order_reads_no_shelf_at_all(db_session, shelf, monkeypatch):
    """I6: a closed order's empty rows need no spools — and must not pay for a dead Spoolman to say so."""
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    await db_session.commit()
    pid, _ = await _order(db_session, shelf["product"].id, 3, status="completed")

    async def _never():
        raise AssertionError("the shelf was read with nothing to compare it against")

    monkeypatch.setattr(filament_needs, "get_spoolman_client", _never)
    out = await filament_needs.needs_of_orders(db_session, [pid])
    assert out[pid].rows == [] and out[pid].stock_unavailable is False


@pytest.mark.asyncio
async def test_the_farm_sums_active_orders_and_does_not_grow_with_them(db_session, shelf, test_engine):
    ids = [(await _order(db_session, shelf["product"].id, 1, colour="black", name="O0"))[0]]
    with counting_statements(test_engine, match="SELECT") as one:
        farm = await filament_needs.needs_of_farm(db_session)
    assert farm.orders_count == 1
    ids += [(await _order(db_session, shelf["product"].id, 1, colour="black", name=f"O{i}"))[0] for i in range(1, 4)]
    await _order(db_session, shelf["product"].id, 9, colour="black", status="completed", name="closed")
    with counting_statements(test_engine, match="SELECT") as four:
        farm = await filament_needs.needs_of_farm(db_session)
    assert farm.orders_count == 4  # the closed one is not summed
    petg = {(r.material, r.colour): r for r in farm.rows}[("PETG", "black")]
    assert (petg.need_g, petg.orders_count, petg.have_g) == (40.0, 4, 800.0)
    assert len(four) <= len(one)


@pytest.mark.asyncio
async def test_the_order_route_answers_rows_and_assumptions(committing_client, db_session, shelf):
    pid, _ = await _order(db_session, shelf["product"].id, 2, colour="black")
    r = await committing_client.get(f"/api/v1/projects/{pid}/filament")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["project_id"] == pid and body["assumptions"] == ["slicer_estimate"] and body["stock_unavailable"] is False
    )
    petg = next(row for row in body["rows"] if row["material"] == "PETG")
    assert (petg["colour"], petg["need_g"], petg["have_g"], petg["have_type_g"], petg["short_g"]) == (
        "black",
        20.0,
        800.0,
        1800.0,
        0.0,
    )
    assert (await committing_client.get("/api/v1/projects/999999/filament")).status_code == 404


@pytest.mark.asyncio
async def test_the_farm_route_sums_active_orders(committing_client, db_session, shelf):
    await _order(db_session, shelf["product"].id, 1, colour="black", name="A")
    await _order(db_session, shelf["product"].id, 2, colour="black", name="B")
    await _order(db_session, shelf["product"].id, 7, colour="black", status="cancelled", name="C")
    body = (await committing_client.get("/api/v1/projects/filament")).json()
    assert body["orders_count"] == 2 and body["assumptions"] == ["slicer_estimate"]
    petg = next(row for row in body["rows"] if row["material"] == "PETG")
    assert (petg["need_g"], petg["orders_count"]) == (30.0, 2)
