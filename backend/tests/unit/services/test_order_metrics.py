"""Formulas and the attribution rule, on in-memory rows."""

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.product import Product, ProductPart
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.services.order_metrics import (
    OrderContext,
    archive_material_set,
    attribute,
    procurement_figures,
    project_figures,
)


def _ctx(lines, parts, archives, archive_parts, plate_product, procurement=None):
    project = Project(name="O", price=100.0)
    project.id = 1
    products = {}
    for p in parts:
        products.setdefault(p.product_id, Product(name=f"P{p.product_id}"))
        products[p.product_id].id = p.product_id
    parts_by_product = {}
    for p in parts:
        parts_by_product.setdefault(p.product_id, []).append(p)
    # Derived exactly as ``load_order_context`` derives it, so a test that
    # writes a 0-plate gets the same wildcard production would.
    whole_file = {file_id: pid for (file_id, plate), pid in plate_product.items() if plate == 0}
    return OrderContext(
        project=project,
        lines=lines,
        products_by_id=products,
        parts_by_product=parts_by_product,
        plate_product=plate_product,
        archives=archives,
        archive_parts_by_archive=dict(archive_parts),
        procurement_by_part=procurement or {},
        whole_file_product=whole_file,
    )


def _part(pid, product_id, key, qty, kind="printed"):
    p = ProductPart(product_id=product_id, kind=kind, name=key, name_key=key, qty_per_unit=qty, aliases=[key])
    p.id = pid
    return p


def _line(lid, product_id, qty, material=None, sort=0):
    line = ProjectLine(project_id=1, product_id=product_id, quantity=qty, material=material, sort_order=sort)
    line.id = lid
    return line


def _archive(aid, *, file_id, plate, status="completed", material="PETG", line_id=None, cost=1.0, secs=100, grams=10.0):
    a = PrintArchive(
        project_id=1,
        library_file_id=file_id,
        plate_index=plate,
        status=status,
        filament_type=material,
        project_line_id=line_id,
        cost=cost,
        actual_time_seconds=secs,
        filament_used_grams=grams,
        quantity=1,
        filename="f",
        file_path="",
        file_size=0,
    )
    a.id = aid
    return a


def _ap(aid, key, qty, defective=0):
    return PrintArchivePart(archive_id=aid, name=key, name_key=key, quantity=qty, defective=defective)


def test_material_set_splits_the_joined_string():
    assert archive_material_set("PLA, PETG") == {"PLA", "PETG"}
    assert archive_material_set(None) == set()


def test_units_printed_is_the_bottleneck_part():
    parts = [_part(1, 10, "a", 1), _part(2, 10, "b", 2), _part(3, 10, "c", 88), _part(9, 10, "cube", 0)]
    lines = [_line(100, 10, 2)]
    # one plate: 1 a + 2 b + 88 c + a cube nobody counts; printed twice, one c scrapped
    archives = [_archive(1, file_id=5, plate=1), _archive(2, file_id=5, plate=1)]
    ap = {
        1: [_ap(1, "a", 1), _ap(1, "b", 2), _ap(1, "c", 88), _ap(1, "cube", 1)],
        2: [_ap(2, "a", 1), _ap(2, "b", 2), _ap(2, "c", 88, defective=1), _ap(2, "cube", 1)],
    }
    figs, other = attribute(_ctx(lines, parts, archives, ap, {(5, 1): 10}))
    line = figs[100]
    by_key = {p.name: p for p in line.parts}
    assert by_key["c"].usable == 175 and by_key["c"].need == 176 and by_key["c"].remaining == 1
    assert "cube" not in by_key  # qty_per_unit 0 is not measured
    assert line.units_printed == 1  # 175 // 88
    assert line.progress == 0.5 and other == []


def test_a_whole_file_plate_claims_every_plate_index_an_archive_can_carry():
    """``plate_index = 0`` on a product plate means THE WHOLE FILE, and the two
    sides of this join count plates differently: ``product_sync`` gives a
    single-plate file one 0-row, while its archives carry the slicer's own
    index — which is **1**, not 0, for essentially every single-plate 3MF ever
    printed (553 of 558 archives on the developer's own farm). An exact
    ``(file, plate)`` tuple therefore matches nothing, and every print of a
    single-plate product falls into "other prints" while the order it belongs
    to reports zero progress against a full shelf of parts.
    """
    parts = [_part(1, 10, "a", 1)]
    lines = [_line(100, 10, 2)]
    archives = [_archive(1, file_id=5, plate=1), _archive(2, file_id=5, plate=7)]
    ap = {1: [_ap(1, "a", 1)], 2: [_ap(2, "a", 1)]}
    figs, other = attribute(_ctx(lines, parts, archives, ap, {(5, 0): 10}))
    assert figs[100].archive_ids == [1, 2] and other == []
    assert figs[100].parts[0].usable == 2 and figs[100].units_printed == 2


def test_material_is_a_hard_filter_and_unmatched_prints_go_to_other():
    parts = [_part(1, 10, "a", 1)]
    lines = [_line(100, 10, 5, material="PETG")]
    archives = [_archive(1, file_id=5, plate=0, material="PLA"), _archive(2, file_id=5, plate=0, material="PETG")]
    ap = {1: [_ap(1, "a", 1)], 2: [_ap(2, "a", 1)]}
    figs, other = attribute(_ctx(lines, parts, archives, ap, {(5, 0): 10}))
    assert figs[100].units_printed == 1 and [a.id for a in other] == [1]


def test_explicit_line_wins_and_first_unmet_line_takes_the_rest():
    parts = [_part(1, 10, "a", 1)]
    lines = [_line(100, 10, 1, sort=0), _line(101, 10, 1, sort=1)]
    # The explicit filing is the NEWEST archive on purpose: applying it before any
    # loose print is what separates the explicit-first pass from a single
    # interleaved pass in created_at order, which would give 100:[1] 101:[2, 3].
    archives = [
        _archive(1, file_id=5, plate=0),
        _archive(2, file_id=5, plate=0),
        _archive(3, file_id=5, plate=0, line_id=101),
    ]
    ap = {i: [_ap(i, "a", 1)] for i in (1, 2, 3)}
    figs, other = attribute(_ctx(lines, parts, archives, ap, {(5, 0): 10}))
    assert figs[101].archive_ids == [3]  # the explicit filing, and only that
    # 1 fills the first unmet line; 2 is surplus once both are met, and overflow
    # goes to the first matching line (spec §Line resolution for an archive).
    assert figs[100].archive_ids == [1, 2]
    assert figs[100].units_printed == 2 and figs[101].units_printed == 1
    assert other == []


def test_in_progress_and_project_totals():
    parts = [_part(1, 10, "a", 2), _part(2, 10, "screw", 4, kind="purchased")]
    lines = [_line(100, 10, 3)]
    archives = [
        _archive(1, file_id=5, plate=0, cost=2.5, secs=60, grams=5.0),
        _archive(2, file_id=5, plate=0, status="printing", cost=None, secs=None, grams=None),
        _archive(3, file_id=5, plate=0, cost=0.5, secs=40, grams=2.0),
    ]
    ap = {1: [_ap(1, "a", 2)], 2: [_ap(2, "a", 2)], 3: [_ap(3, "a", 2)]}
    ctx = _ctx(lines, parts, archives, ap, {(5, 0): 10}, procurement={2: 6})
    figs, other = attribute(ctx)
    a = figs[100].parts[0]
    assert a.usable == 4 and a.in_progress == 2 and a.need == 6 and a.remaining == 2
    proc = procurement_figures(ctx)
    assert proc[0].need == 12 and proc[0].acquired == 6 and proc[0].remaining == 6
    pf = project_figures(ctx, figs, other)
    assert pf.ordered == 3 and pf.printed == 2 and pf.remaining == 1
    # Procurement is the BINDING side of the min here: two units are printed but
    # only 6 of the 8 screws they need are in, so the kit count is what the
    # screws allow, not what the printer produced. Dropping the purchased-part
    # loop from _units_complete yields 2 and fails this line.
    assert pf.complete == 1  # min(2 printed, 6 // 4 = 1 kit of screws)
    assert pf.total_cost == 3.0 and pf.margin == 97.0 and pf.total_time_seconds == 100 and pf.all_printed is False
