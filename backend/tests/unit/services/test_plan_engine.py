"""The covering engine on in-memory rows — pure, no session anywhere.

⚠️ Attribution (``order_metrics.attribute``) and covering (``plan_engine.cover``)
are two different greedy algorithms that happen to share the word "greedy".
These tests are about the second one only: given what a line still needs, which
plates does the operator print.
"""

from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.services import plan_engine
from backend.app.services.order_metrics import LineFigures, OrderContext, PartFigures
from backend.app.services.plan_engine import plan_lines
from backend.app.services.product_composition import PlateRecipe


def _ctx(lines, parts):
    project = Project(name="O", price=None)
    project.id = 1
    products: dict[int, Product] = {}
    parts_by_product: dict[int, list[ProductPart]] = {}
    for p in parts:
        products.setdefault(p.product_id, Product(name=f"P{p.product_id}"))
        products[p.product_id].id = p.product_id
        parts_by_product.setdefault(p.product_id, []).append(p)
    return OrderContext(
        project=project,
        lines=lines,
        products_by_id=products,
        parts_by_product=parts_by_product,
        plate_product={},
        archives=[],
        archive_parts_by_archive={},
        procurement_by_part={},
    )


def _part(pid, product_id, key, qty, kind="printed"):
    p = ProductPart(product_id=product_id, kind=kind, name=key, name_key=key, qty_per_unit=qty, aliases=[key])
    p.id = pid
    return p


def _line(lid, product_id, qty, material=None, sort=0):
    line = ProjectLine(project_id=1, product_id=product_id, quantity=qty, material=material, sort_order=sort)
    line.id = lid
    return line


def _figs(line, parts, remaining, in_progress=None):
    """``LineFigures`` as ``attribute`` would leave them — one row per counted part."""
    in_progress = in_progress or {}
    figs = LineFigures(line_id=line.id, product_id=line.product_id, quantity=line.quantity, material=line.material)
    for part in parts:
        if part.product_id != line.product_id or part.kind != "printed" or part.qty_per_unit <= 0:
            continue
        need = part.qty_per_unit * line.quantity
        left = remaining.get(part.id, 0)
        figs.parts.append(
            PartFigures(
                part_id=part.id,
                name=part.name,
                kind=part.kind,
                qty_per_unit=part.qty_per_unit,
                need=need,
                usable=max(0, need - left),
                in_progress=in_progress.get(part.id, 0),
                remaining=left,
            )
        )
    return figs


def _cand(plate_id, product_id, yields, *, secs=100, grams=10.0, sliced=True, materials=("PETG",), file_id=1, index=1):
    plate = ProductPlate(product_id=product_id, library_file_id=file_id, plate_index=index)
    plate.id = plate_id
    file = LibraryFile(filename="f.3mf", file_path="", file_type="3mf", file_size=0)
    file.id = file_id
    recipe = PlateRecipe(
        library_file_id=file_id,
        plate_index=index,
        sliced=sliced,
        yield_by_part=dict(yields),
        materials=set(materials),
        print_time_seconds=secs,
        filament_used_grams=grams,
    )
    return plate, file, recipe


def test_worked_case_from_the_spec():
    c = _part(1, 10, "c", 1)
    line = _line(100, 10, 120)
    ctx = _ctx([line], [c])
    figures = {line.id: _figs(line, [c], {1: 120})}
    # The parent spec's "plate A yields 100 in ~10·T": the tilde is load-bearing.
    # At exactly 10·T both plates score 10/T on the first pick and the tie-break
    # (lower waste, then lower TIME) takes B every round — twelve prints, never
    # A. Just under 10·T is what makes A the first pick, and after it the 20 that
    # are left cost A 80 of waste per 10·T while B still delivers 10 per T.
    a = _cand(1, 10, {1: 100}, secs=900, grams=100.0)
    b = _cand(2, 10, {1: 10}, secs=100, grams=10.0)
    plan = plan_lines(ctx, figures, {10: [a, b]}, {}, 0.02)
    lp = plan.lines[0]
    assert lp.outstanding_before == {1: 120}
    assert [(r.plate_id, r.count) for r in lp.rows] == [(1, 1), (2, 2)]
    assert lp.rows[0].useful == {1: 100}
    assert lp.rows[1].useful == {1: 20}
    assert lp.surplus_after == {}
    assert lp.unsatisfiable == []
    assert plan.totals.prints == 3
    assert plan.totals.print_time_seconds == 900 + 2 * 100
    assert plan.totals.filament_used_grams == 120.0
    # A row carries the per-print figures; the count is the frontend's multiplier.
    assert (lp.rows[0].cost, lp.rows[1].cost) == (2.0, 0.2)
    assert plan.totals.cost == 2.4


def test_waste_tie_break_then_time():
    p = _part(1, 10, "a", 1)
    line = _line(100, 10, 10)
    ctx = _ctx([line], [p])
    # Equal useful/time (10/100 both): the lower-waste plate wins even though the
    # wasteful one would win the plate-id tie-break.
    wasteful = _cand(1, 10, {1: 20}, secs=100)
    tight = _cand(2, 10, {1: 10}, secs=100)
    plan = plan_lines(ctx, {line.id: _figs(line, [p], {1: 10})}, {10: [wasteful, tight]}, {}, None)
    assert [(r.plate_id, r.count) for r in plan.lines[0].rows] == [(2, 1)]
    # Equal useful/time (0.05 both) AND equal waste (0 both): the shorter print
    # wins, again against the plate-id order.
    slow = _cand(1, 10, {1: 10}, secs=200)
    quick = _cand(2, 10, {1: 5}, secs=100)
    plan = plan_lines(ctx, {line.id: _figs(line, [p], {1: 10})}, {10: [slow, quick]}, {}, None)
    assert [(r.plate_id, r.count) for r in plan.lines[0].rows] == [(2, 2)]


def test_in_progress_and_queued_work_are_excluded():
    p = _part(1, 10, "a", 1)
    line = _line(100, 10, 10)
    ctx = _ctx([line], [p])
    plate = _cand(1, 10, {1: 1}, secs=100)
    # The map is what ``queued_yield_by_line`` returns: three rows still waiting.
    # A fourth auto-queue row already handed to a printer item (status 'pending'
    # but ``assigned_to_item_id`` set) is not waiting and the loader's filter
    # drops it; the DB proof of that filter is Task 3's integration test.
    plan = plan_lines(ctx, {line.id: _figs(line, [p], {1: 10}, {1: 4})}, {10: [plate]}, {line.id: {1: 3}}, None)
    assert plan.lines[0].outstanding_before == {1: 3}
    assert plan.totals.prints == 3
    # Had the assigned row counted, the plan would be one print short.
    short = plan_lines(ctx, {line.id: _figs(line, [p], {1: 10}, {1: 4})}, {10: [plate]}, {line.id: {1: 4}}, None)
    assert short.totals.prints == 2


def test_material_is_a_hard_filter():
    p = _part(1, 10, "a", 1)
    petg = _line(100, 10, 2, material="PETG")
    free = _line(101, 10, 2, material=None, sort=1)
    ctx = _ctx([petg, free], [p])
    figures = {petg.id: _figs(petg, [p], {1: 2}), free.id: _figs(free, [p], {1: 2})}
    pla = _cand(1, 10, {1: 1}, secs=100, materials=("PLA",))
    plan = plan_lines(ctx, figures, {10: [pla]}, {}, None)
    petg_plan, free_plan = plan.lines
    assert petg_plan.candidates == []
    assert petg_plan.rows == []
    assert petg_plan.unsatisfiable == [1]
    assert free_plan.candidates == [1]
    assert [(r.plate_id, r.count) for r in free_plan.rows] == [(1, 2)]
    # A plate whose materials are unknown is not a match either — same reading as
    # ``order_metrics._line_accepts``, where an empty set matches no material.
    figures = {petg.id: _figs(petg, [p], {1: 2}), free.id: _figs(free, [p], {1: 2})}
    unknown = _cand(2, 10, {1: 1}, secs=100, materials=())
    plan = plan_lines(ctx, figures, {10: [unknown]}, {}, None)
    assert plan.lines[0].candidates == []
    assert plan.lines[1].candidates == [2]


def test_zero_quantity_parts_neither_drive_nor_count():
    a = _part(1, 10, "a", 1)
    z = _part(2, 10, "z", 0)
    line = _line(100, 10, 3)
    ctx = _ctx([line], [a, z])
    figures = {line.id: _figs(line, [a, z], {1: 3})}
    plate = _cand(1, 10, {1: 1, 2: 5}, secs=100)
    lp = plan_lines(ctx, figures, {10: [plate]}, {}, None).lines[0]
    assert lp.outstanding_before == {1: 3}
    assert lp.rows[0].useful == {1: 3}
    assert lp.surplus_after == {}
    assert 2 not in lp.outstanding_before
    assert 2 not in lp.rows[0].useful
    assert 2 not in lp.surplus_after


def test_shared_plate_yields_only_the_lines_counted_parts():
    lid_a = _part(1, 10, "lid_a", 1)
    lid_b = _part(2, 20, "lid_b", 1)  # another product's lid, sharing the bed
    line = _line(100, 10, 4)
    ctx = _ctx([line], [lid_a, lid_b])
    figures = {line.id: _figs(line, [lid_a, lid_b], {1: 4})}
    plate = _cand(1, 10, {1: 4, 2: 4}, secs=100)
    lp = plan_lines(ctx, figures, {10: [plate]}, {}, None).lines[0]
    assert lp.rows[0].count == 1
    assert lp.rows[0].useful == {1: 4}  # four, not eight
    assert lp.surplus_after == {}


def test_unsatisfiable_part_is_reported():
    a = _part(1, 10, "a", 1)
    b = _part(2, 10, "b", 1)
    line = _line(100, 10, 3)
    ctx = _ctx([line], [a, b])
    figures = {line.id: _figs(line, [a, b], {1: 2, 2: 3})}
    plate = _cand(1, 10, {1: 1}, secs=100)
    lp = plan_lines(ctx, figures, {10: [plate]}, {}, None).lines[0]
    assert [(r.plate_id, r.count) for r in lp.rows] == [(1, 2)]
    assert lp.unsatisfiable == [2]


def test_unknown_time_ranks_by_useful_only_and_is_flagged():
    p = _part(1, 10, "a", 1)
    line = _line(100, 10, 5)
    ctx = _ctx([line], [p])
    figures = {line.id: _figs(line, [p], {1: 5})}
    timeless = _cand(1, 10, {1: 5}, secs=None)
    timed = _cand(2, 10, {1: 1}, secs=10)
    plan = plan_lines(ctx, figures, {10: [timeless, timed]}, {}, None)
    lp = plan.lines[0]
    assert [(r.plate_id, r.count) for r in lp.rows] == [(1, 1)]
    assert lp.rows[0].time_unknown is True
    assert lp.rows[0].print_time_seconds is None
    assert plan.totals.print_time_seconds is None


def test_iteration_guard(monkeypatch):
    monkeypatch.setattr(plan_engine, "MAX_ITERATIONS", 5)
    p = _part(1, 10, "a", 1)
    line = _line(100, 10, 1000)
    ctx = _ctx([line], [p])
    figures = {line.id: _figs(line, [p], {1: 1000})}
    plate = _cand(1, 10, {1: 1}, secs=100)
    plan = plan_lines(ctx, figures, {10: [plate]}, {}, None)
    lp = plan.lines[0]
    assert plan.totals.prints == 5
    assert lp.rows[0].useful == {1: 5}
    assert lp.surplus_after == {}
    # The guard is a defence, not a verdict: the part IS coverable, so it must
    # not be reported as having no plate.
    assert lp.unsatisfiable == []


def test_not_sliced_plates_are_listed_not_planned():
    p = _part(1, 10, "a", 1)
    line = _line(100, 10, 2)
    ctx = _ctx([line], [p])
    raw = _cand(1, 10, {1: 10}, secs=None, sliced=False)
    ready = _cand(2, 10, {1: 1}, secs=100)
    lp = plan_lines(ctx, {line.id: _figs(line, [p], {1: 2})}, {10: [raw, ready]}, {}, None).lines[0]
    assert lp.not_sliced == [1]
    assert lp.candidates == [2]
    assert [(r.plate_id, r.count) for r in lp.rows] == [(2, 2)]
    # With nothing but the unsliced plate no work is planned at all and the part
    # reads as having no plate — slicing it is the operator's move, not ours.
    only_raw = plan_lines(ctx, {line.id: _figs(line, [p], {1: 2})}, {10: [raw]}, {}, None).lines[0]
    assert only_raw.rows == []
    assert only_raw.candidates == []
    assert only_raw.not_sliced == [1]
    assert only_raw.unsatisfiable == [1]
