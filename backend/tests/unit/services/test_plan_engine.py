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
from backend.app.services.filament_cost import cost_of
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


def _cand(
    plate_id,
    product_id,
    yields,
    *,
    secs=100,
    grams=10.0,
    sliced=True,
    materials=("PETG",),
    file_id=1,
    index=1,
    model=None,
    filename="f.3mf",
):
    plate = ProductPlate(product_id=product_id, library_file_id=file_id, plate_index=index)
    plate.id = plate_id
    file = LibraryFile(filename=filename, file_path="", file_type="3mf", file_size=0)
    file.id = file_id
    recipe = PlateRecipe(
        library_file_id=file_id,
        plate_index=index,
        sliced=sliced,
        yield_by_part=dict(yields),
        materials=set(materials),
        print_time_seconds=secs,
        filament_used_grams=grams,
        printer_model=model,
    )
    return plate, file, recipe


def test_worked_case_from_the_spec():
    c = _part(1, 10, "c", 1)
    line = _line(100, 10, 120)
    ctx = _ctx([line], [c])
    figures = {line.id: _figs(line, [c], {1: 120})}
    # The parent spec's "plate A yields 100 in ~10·T": the tilde is load-bearing.
    # At exactly 10·T (999 → 1000 below) both plates score 10/T on the first
    # pick and the tie-break (lower waste — both 0 — then lower TIME) takes B;
    # and it keeps tying, so B wins every round and A is never picked at all,
    # twelve prints instead of three. A hair under 10·T is what makes A the
    # first pick, and after it the 20 that are left cost A 80 of waste per 10·T
    # while B still delivers 10 per T.
    a = _cand(1, 10, {1: 100}, secs=999, grams=100.0)
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
    assert plan.totals.print_time_seconds == 999 + 2 * 100
    assert plan.totals.filament_used_grams == 120.0
    # A row carries the per-print figures; the count is the frontend's multiplier.
    assert (lp.rows[0].cost, lp.rows[1].cost) == (2.0, 0.2)
    assert plan.totals.cost == 2.4
    # The names ride out beside the plan, from the context it was built on, so
    # the route never re-reads rows the request already had.
    assert plan.part_names == {1: "c"}
    assert plan.product_names == {10: "P10"}
    # A plan that ran to the end of its work says so — the flag is only ever
    # about the iteration guard, never about an unsatisfiable part.
    assert plan.truncated is False


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


def test_a_foreign_objects_waste_is_not_this_lines_waste():
    """The one place ``line_yield``'s filter changes a DECISION rather than a count.

    A foreign object contributes 0 useful (it has no outstanding entry) and
    never reaches the surplus (which walks the line's own outstanding parts), so
    dropping the filter is invisible everywhere except ``waste`` — where it
    makes a shared plate look like a spendthrift and hands the pick to a plate
    that ties with it. Both plates below score 10/100; the shared one wins only
    on the plate-id tie-break, which it reaches only if its 50 foreign objects
    were filtered out of its waste first.
    """
    ours = _part(1, 10, "ours", 1)
    theirs = _part(2, 20, "theirs", 1)  # another product's part, riding the same bed
    line = _line(100, 10, 10)
    ctx = _ctx([line], [ours, theirs])
    shared = _cand(1, 10, {1: 10, 2: 50}, secs=100)
    solo = _cand(2, 10, {1: 10}, secs=100)
    lp = plan_lines(ctx, {line.id: _figs(line, [ours, theirs], {1: 10})}, {10: [shared, solo]}, {}, None).lines[0]
    assert [(r.plate_id, r.count) for r in lp.rows] == [(1, 1)]
    assert lp.rows[0].useful == {1: 10}
    assert lp.surplus_after == {}


def test_surplus_is_reported_when_a_plate_overshoots():
    p = _part(1, 10, "a", 1)
    line = _line(100, 10, 3)
    ctx = _ctx([line], [p])
    # Need 3, the plate makes 2: one print leaves 1 outstanding, the second
    # covers it and makes one spare. Nothing smaller exists, so the spare is the
    # honest price of finishing the line — it is reported, not hidden.
    plate = _cand(1, 10, {1: 2}, secs=100)
    lp = plan_lines(ctx, {line.id: _figs(line, [p], {1: 3})}, {10: [plate]}, {}, None).lines[0]
    assert [(r.plate_id, r.count) for r in lp.rows] == [(1, 2)]
    assert lp.rows[0].useful == {1: 3}  # covered outstanding, not parts produced
    assert lp.surplus_after == {1: 1}


def test_an_unknown_time_loses_a_tie_it_did_not_win_on_score():
    p = _part(1, 10, "a", 1)
    line = _line(100, 10, 2)
    ctx = _ctx([line], [p])
    # Both score 1.0 (the timeless plate divides by 1) and both waste nothing,
    # so the time tie-break decides — and an unknown time sorts LAST. The
    # timeless plate also holds the lower plate id, so it would win both a
    # "None first" ordering and the id tie-break: only the None-last rule keeps
    # the plate with a real estimate in front.
    timeless = _cand(1, 10, {1: 1}, secs=None)
    timed = _cand(2, 10, {1: 2}, secs=2)
    lp = plan_lines(ctx, {line.id: _figs(line, [p], {1: 2})}, {10: [timeless, timed]}, {}, None).lines[0]
    assert [(r.plate_id, r.count) for r in lp.rows] == [(2, 1)]
    assert lp.rows[0].time_unknown is False


def test_more_queued_than_remaining_floors_at_zero():
    p = _part(1, 10, "a", 1)
    line = _line(100, 10, 5)
    ctx = _ctx([line], [p])
    # An over-producing plate was queued earlier: 9 parts coming for 5 still
    # owed. Outstanding floors at 0 — a negative would flow into the surplus
    # arithmetic as a phantom and into the pick loop as free useful count.
    plate = _cand(1, 10, {1: 4}, secs=100)
    plan = plan_lines(ctx, {line.id: _figs(line, [p], {1: 5})}, {10: [plate]}, {line.id: {1: 9}}, None)
    lp = plan.lines[0]
    assert lp.outstanding_before == {}
    assert lp.rows == []
    assert lp.surplus_after == {}
    assert lp.unsatisfiable == []
    assert plan.totals.prints == 0


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
    timeless = _cand(1, 10, {1: 5}, secs=None, grams=None)
    timed = _cand(2, 10, {1: 1}, secs=10)
    plan = plan_lines(ctx, figures, {10: [timeless, timed]}, {}, 0.02)
    lp = plan.lines[0]
    assert [(r.plate_id, r.count) for r in lp.rows] == [(1, 1)]
    assert lp.rows[0].time_unknown is True
    assert lp.rows[0].print_time_seconds is None
    assert plan.totals.print_time_seconds is None
    # A rate exists but the only planned plate has no weight: the footer says
    # "unknown", never 0.00, which would read as "this plan is free".
    assert lp.rows[0].cost is None
    assert plan.totals.filament_used_grams == 0.0
    assert plan.totals.cost is None


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
    # ...but the stop is not silent either. Rows, totals and an empty
    # ``unsatisfiable`` look exactly like a finished plan, so without this flag
    # the operator prints all of it believing the order is covered.
    assert plan.truncated is True


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


def test_a_zero_print_time_is_no_estimate_and_never_wins_a_tie():
    """``print_time_seconds = 0`` is a file with no estimate, not an instant plate.

    Before this was normalised the two halves of the ranking disagreed: the
    score divided by ``secs or 1`` (reading the zero as "unknown") while the
    tie-break read the same zero as a real, unbeatable 0 s — so a plate that
    "takes no time" won every tie in the plan while reporting
    ``time_unknown=False``, i.e. claiming an estimate it did not have.
    """
    p = _part(1, 10, "a", 1)
    line = _line(100, 10, 2)
    ctx = _ctx([line], [p])
    # Both score 1.0 and neither wastes anything, so the time tie-break decides
    # — and no estimate must sort LAST, exactly as ``secs=None`` does.
    zero = _cand(1, 10, {1: 1}, secs=0)
    timed = _cand(2, 10, {1: 2}, secs=2)
    plan = plan_lines(ctx, {line.id: _figs(line, [p], {1: 2})}, {10: [zero, timed]}, {}, None)
    lp = plan.lines[0]
    assert [(r.plate_id, r.count) for r in lp.rows] == [(2, 1)]
    assert plan.totals.print_time_seconds == 2

    # And on its own the zero reports what it is: no time on the row, the flag
    # raised, and the footer's time voided rather than summed as 0 s.
    alone = plan_lines(ctx, {line.id: _figs(line, [p], {1: 2})}, {10: [zero]}, {}, None)
    row = alone.lines[0].rows[0]
    assert row.count == 2
    assert row.print_time_seconds is None
    assert row.time_unknown is True
    assert alone.totals.print_time_seconds is None


def test_a_rows_cost_is_the_farms_own_arithmetic():
    """The engine prices a row itself — it is handed a price per GRAM, while
    ``filament_cost.cost_of`` is handed the farm rate per KILOGRAM — so the two
    could drift apart on rounding and nobody would see it: one number is on the
    plan block, the other on every archive.

    Reusing ``cost_of`` here would mean threading the per-kilogram rate through
    ``plan_lines`` / ``cover`` / ``_totals`` in place of the per-gram price the
    whole engine is written against, so the two are PINNED instead. If this
    fails, the plan and the archive have started disagreeing about what a print
    costs and one of them has to move.
    """
    part = _part(1, 10, "a", 1)
    line = _line(100, 10, 1)
    ctx = _ctx([line], [part])
    for grams, rate_per_kg in ((10.0, 20.0), (7.3, 24.99), (0.5, 1.0), (123.456, 18.75), (1000.0, 25.0)):
        plan = plan_lines(
            ctx,
            {line.id: _figs(line, [part], {1: 1})},
            {10: [_cand(1, 10, {1: 1}, grams=grams)]},
            {},
            rate_per_kg / 1000.0,
        )
        assert plan.lines[0].rows[0].cost == cost_of(grams, rate_per_kg), (grams, rate_per_kg)

    # And both spell "unanswerable" the same way: None, never 0.0 — a zero cost
    # is a claim that the print was free.
    no_rate = plan_lines(ctx, {line.id: _figs(line, [part], {1: 1})}, {10: [_cand(1, 10, {1: 1})]}, {}, None)
    assert no_rate.lines[0].rows[0].cost is None and cost_of(10.0, 0.0) is None
    no_grams = plan_lines(
        ctx, {line.id: _figs(line, [part], {1: 1})}, {10: [_cand(1, 10, {1: 1}, grams=None)]}, {}, 0.02
    )
    assert no_grams.lines[0].rows[0].cost is None and cost_of(None, 20.0) is None


# ---- alternatives: the same part, sliced once per printer model (pass 7, decision 6) ----


def test_a_plate_making_the_same_counted_parts_is_an_alternative():
    """The greedy picks ONE plate and hangs every print on it, so the second
    file — the same part sliced for the other printer — used to be invisible in
    the block even though the operator owns both machines (user report,
    2026-09-04). It rides out on the row it duplicates, with its own figures.
    """
    body = _part(1, 10, "body", 1)
    line = _line(100, 10, 10)
    ctx = _ctx([line], [body])
    figures = {line.id: _figs(line, [body], {1: 10})}
    fast = _cand(1, 10, {1: 5}, secs=1000, grams=50.0, file_id=1, model="X1C", filename="body-x1c.3mf")
    slow = _cand(2, 10, {1: 5}, secs=2000, grams=60.0, file_id=2, model="P1S", filename="body-p1s.3mf")

    plan = plan_lines(ctx, figures, {10: [fast, slow]}, {}, 0.02)
    rows = plan.lines[0].rows

    assert [(r.plate_id, r.count) for r in rows] == [(1, 2)]
    assert rows[0].printer_model == "X1C"
    assert [(a.plate_id, a.printer_model, a.filename) for a in rows[0].alternatives] == [(2, "P1S", "body-p1s.3mf")]
    alt = rows[0].alternatives[0]
    # Its OWN figures, per print — the block re-does the arithmetic against them
    # the moment the operator switches files.
    assert (alt.library_file_id, alt.plate_index) == (2, 1)
    assert (alt.print_time_seconds, alt.filament_used_grams, alt.cost) == (2000, 60.0, 1.2)
    assert alt.time_unknown is False


def test_a_plate_with_a_different_counted_yield_is_not_an_alternative():
    """Same product, same material, half the parts per print. Switching to it
    would print half the order — it is a candidate, and the "+ plate" menu is
    where it belongs, not the row's file switch."""
    body = _part(1, 10, "body", 1)
    line = _line(100, 10, 10)
    ctx = _ctx([line], [body])
    figures = {line.id: _figs(line, [body], {1: 10})}
    five = _cand(1, 10, {1: 5}, secs=1000, file_id=1, model="X1C")
    two = _cand(2, 10, {1: 2}, secs=2000, file_id=2, model="P1S")

    plan = plan_lines(ctx, figures, {10: [five, two]}, {}, None)

    assert [r.plate_id for r in plan.lines[0].rows] == [1]
    assert plan.lines[0].rows[0].alternatives == []


def test_an_uncounted_part_beside_the_same_counted_ones_is_still_an_alternative():
    """The key is the COUNTED yield, exactly as ``line_yield`` reads it. A plate
    that also carries a part this line zeroes (``qty_per_unit = 0``, or another
    product's part on a shared bed) makes the same thing for this line — the
    extra is somebody else's surplus, not a different plate."""
    body = _part(1, 10, "body", 1)
    trinket = _part(2, 10, "trinket", 0)
    line = _line(100, 10, 10)
    ctx = _ctx([line], [body, trinket])
    figures = {line.id: _figs(line, [body, trinket], {1: 10})}
    plain = _cand(1, 10, {1: 5}, secs=1000, file_id=1, model="X1C")
    plus = _cand(2, 10, {1: 5, 2: 4}, secs=2000, file_id=2, model="P1S")

    plan = plan_lines(ctx, figures, {10: [plain, plus]}, {}, None)
    rows = plan.lines[0].rows

    assert [r.plate_id for r in rows] == [1]
    assert [a.plate_id for a in rows[0].alternatives] == [2]


def test_alternatives_are_sorted_by_model_and_never_list_the_row_itself():
    body = _part(1, 10, "body", 1)
    line = _line(100, 10, 10)
    ctx = _ctx([line], [body])
    figures = {line.id: _figs(line, [body], {1: 10})}
    picked = _cand(1, 10, {1: 5}, secs=1000, file_id=1, model="X1C")
    p1s = _cand(2, 10, {1: 5}, secs=2000, file_id=2, model="P1S")
    nameless = _cand(3, 10, {1: 5}, secs=3000, file_id=3, model=None)
    a1 = _cand(4, 10, {1: 5}, secs=4000, file_id=4, model="A1")

    plan = plan_lines(ctx, figures, {10: [picked, p1s, nameless, a1]}, {}, None)
    row = plan.lines[0].rows[0]

    assert row.plate_id == 1
    # A model-less plate sorts first (""), then by model name, then by plate id.
    assert [(a.plate_id, a.printer_model) for a in row.alternatives] == [(3, None), (4, "A1"), (2, "P1S")]
