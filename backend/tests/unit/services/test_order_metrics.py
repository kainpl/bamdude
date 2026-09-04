"""Formulas and the attribution rule, on in-memory rows."""

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.customer import Customer
from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.services.order_metrics import (
    OrderContext,
    archive_material_set,
    attribute,
    grouped_figures,
    load_order_context,
    procurement_figures,
    project_figures,
    units_delivered,
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
    # A plate names a SET of products, so the index holds a list. A bare int is
    # accepted as the one-product shorthand, which is all most tests need.
    plate_product = {key: [v] if isinstance(v, int) else list(v) for key, v in plate_product.items()}
    # Derived exactly as ``load_order_context`` derives it, so a test that
    # writes a 0-plate gets the same wildcard production would.
    whole_file = {file_id: pids for (file_id, plate), pids in plate_product.items() if plate == 0}
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


def test_a_shared_part_fills_lines_in_sort_order_then_spills():
    """One FILE in two products of the same order — a flask shared by two
    products. The plate names BOTH products, so both lines are candidates and
    the part row is handed out across them in sort order. While the plate
    indexes held one product id per key, whichever product loaded last silently
    took every print of the file and its sibling reported nothing.
    """
    parts = [_part(1, 10, "flask", 1), _part(2, 20, "flask", 1)]
    lines = [_line(100, 10, 2, sort=0), _line(101, 20, 3, sort=1)]
    archives = [_archive(i, file_id=5, plate=1) for i in (1, 2, 3)]
    archives.append(_archive(4, file_id=5, plate=1, status="printing"))
    ap = {i: [_ap(i, "flask", 2)] for i in (1, 2, 3)} | {4: [_ap(4, "flask", 1)]}
    figs, other = attribute(_ctx(lines, parts, archives, ap, {(5, 0): [10, 20]}))
    first, second = figs[100].parts[0], figs[101].parts[0]
    assert first.usable == 3 and first.need == 2 and first.surplus == 1  # 2 from #1, then the spill of #3
    assert second.usable == 3 and second.remaining == 0  # 2 from #2, 1 from #3
    # #4 is still printing and every line's room is used up, so it spills to the
    # first candidate that counts the part rather than being dropped.
    assert first.in_progress == 1 and second.in_progress == 0
    assert figs[100].archive_ids == [1, 3, 4] and figs[101].archive_ids == [2, 3]
    assert other == []


def test_one_plate_feeds_two_products_part_by_part():
    """One PLATE carrying parts of two products — lid A and lid B on one bed.
    Each product zeroes the object it does not use, so each part row has exactly
    one taker. Handing the whole archive to one line lost the other product's
    lids entirely.
    """
    parts = [_part(1, 10, "lid_a", 1), _part(2, 10, "lid_b", 0), _part(3, 20, "lid_b", 1), _part(4, 20, "lid_a", 0)]
    lines = [_line(100, 10, 4), _line(101, 20, 4, sort=1)]
    archives = [_archive(1, file_id=7, plate=1)]
    ap = {1: [_ap(1, "lid_a", 4), _ap(1, "lid_b", 4, defective=1)]}
    figs, other = attribute(_ctx(lines, parts, archives, ap, {(7, 1): [10, 20]}))
    assert figs[100].parts[0].usable == 4 and figs[100].units_printed == 4
    assert "lid_b" not in {p.name for p in figs[100].parts}  # qty_per_unit 0 is not measured
    assert figs[101].parts[0].usable == 3 and figs[101].parts[0].remaining == 1
    assert figs[100].archive_ids == [1] == figs[101].archive_ids
    assert other == []


def test_an_explicit_filing_keeps_its_own_parts_and_passes_foreign_ones_on():
    """The home line takes every row its product counts, in full, need or no
    need — an operator's filing is never second-guessed. The rows the home
    product does not count fall through to the other candidates.
    """
    parts = [_part(1, 10, "lid_a", 1), _part(2, 10, "lid_b", 0), _part(3, 20, "lid_b", 1), _part(4, 20, "lid_a", 0)]
    lines = [_line(100, 10, 2, sort=0), _line(101, 20, 4, sort=1)]
    archives = [_archive(1, file_id=7, plate=1, line_id=100)]
    ap = {1: [_ap(1, "lid_a", 4), _ap(1, "lid_b", 4)]}
    figs, other = attribute(_ctx(lines, parts, archives, ap, {(7, 1): [10, 20]}))
    home = figs[100].parts[0]
    assert home.usable == 4 and home.need == 2 and home.surplus == 2  # nothing spills: product 20 zeroes lid_a
    assert figs[101].parts[0].usable == 4
    assert figs[100].archive_ids == [1] and figs[101].archive_ids == [1]
    assert other == []


def test_material_excludes_a_line_part_by_part():
    parts = [_part(1, 10, "flask", 1), _part(2, 20, "flask", 1)]
    ap = {1: [_ap(1, "flask", 2)]}
    lines = [_line(100, 10, 2, material="PLA", sort=0), _line(101, 20, 2, sort=1)]
    archives = [_archive(1, file_id=5, plate=0)]  # PETG
    figs, other = attribute(_ctx(lines, parts, archives, ap, {(5, 0): [10, 20]}))
    # The PLA line never becomes a candidate, so the whole row goes to the other one.
    assert figs[100].parts[0].usable == 0 and figs[101].parts[0].usable == 2
    assert other == []

    lines = [_line(100, 10, 2, material="PLA", sort=0), _line(101, 20, 2, material="PETG", sort=1)]
    archives = [_archive(1, file_id=5, plate=0, material="ABS")]
    figs, other = attribute(_ctx(lines, parts, archives, ap, {(5, 0): [10, 20]}))
    assert other == archives  # the plate names both products, but neither line takes ABS
    assert figs[100].parts[0].usable == 0 and figs[101].parts[0].usable == 0


def test_an_archive_with_candidates_but_no_counted_part_is_listed_not_binned():
    """A failed print and a plate of objects nobody counts still belong to the
    line whose plate they carry: listed under it, contributing nothing. Binning
    them into "other prints" would report the order's own work as a stranger's.
    """
    parts = [_part(1, 10, "flask", 1)]
    lines = [_line(100, 10, 1)]
    archives = [_archive(1, file_id=5, plate=0, status="failed"), _archive(2, file_id=5, plate=0)]
    ap = {1: [_ap(1, "flask", 1)], 2: [_ap(2, "cube", 1)]}
    figs, other = attribute(_ctx(lines, parts, archives, ap, {(5, 0): [10]}))
    assert figs[100].archive_ids == [1, 2]
    assert figs[100].parts[0].usable == 0 and other == []


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


def test_a_scrapped_plate_is_listed_under_the_line_whose_product_counts_it():
    """An archive that credited nothing is still the order's work, and WHICH
    line's work is readable off its part rows.

    Two products share a bed, each zeroing the other's object (the modelling
    convention every plate-sharing test here uses). A plate of lid_b that came
    out entirely defective credits nobody — and used to be listed under
    whichever line happened to sort first, so a scrapped batch of lids showed up
    against the product that never had a lid on that bed. The row keys say whose
    it was; only the counting failed.
    """
    parts = [_part(1, 10, "lid_a", 1), _part(2, 10, "lid_b", 0), _part(3, 20, "lid_b", 1), _part(4, 20, "lid_a", 0)]
    lines = [_line(100, 10, 4, sort=0), _line(101, 20, 4, sort=1)]
    archives = [_archive(1, file_id=7, plate=1)]
    ap = {1: [_ap(1, "lid_b", 4, defective=4)]}
    figs, other = attribute(_ctx(lines, parts, archives, ap, {(7, 1): [10, 20]}))
    assert figs[101].archive_ids == [1]
    assert figs[100].archive_ids == []  # sorts first, counts no lid_b: not its plate
    assert figs[101].parts[0].usable == 0 and other == []


def test_a_print_whose_rows_no_candidate_counts_falls_back_to_the_first():
    """No row key resolves anywhere — a failed print that never produced rows, or
    a plate of test pieces. There is nothing to read, so the oldest rule stands:
    the first candidate lists it, uncounted. Binning it would report the order's
    own work as a stranger's."""
    parts = [_part(1, 10, "lid_a", 1), _part(2, 10, "lid_b", 0), _part(3, 20, "lid_b", 1), _part(4, 20, "lid_a", 0)]
    lines = [_line(100, 10, 4, sort=0), _line(101, 20, 4, sort=1)]
    archives = [_archive(1, file_id=7, plate=1, status="failed"), _archive(2, file_id=7, plate=1)]
    ap = {2: [_ap(2, "sacrificial_raft", 1)]}
    figs, other = attribute(_ctx(lines, parts, archives, ap, {(7, 1): [10, 20]}))
    assert figs[100].archive_ids == [1, 2]  # a failed print keeps its rows, and they credit nothing
    assert figs[101].archive_ids == []
    assert other == []


def test_progress_never_exceeds_one_however_far_a_line_is_overprinted():
    """The surplus is what reports the excess (``surplus``, ``units_printed``);
    ``progress`` is a bar's fill and a bar cannot be 300% full. It was, on the
    wire, and every consumer had to remember to clamp it."""
    parts = [_part(1, 10, "a", 1)]
    lines = [_line(100, 10, 1)]
    archives = [_archive(i, file_id=5, plate=0) for i in (1, 2, 3)]
    ap = {i: [_ap(i, "a", 1)] for i in (1, 2, 3)}
    ctx = _ctx(lines, parts, archives, ap, {(5, 0): 10})
    figs, other = attribute(ctx)
    assert figs[100].units_printed == 3 and figs[100].parts[0].surplus == 2  # the excess still shows
    assert figs[100].progress == 1.0
    pf = project_figures(ctx, figs, other)
    assert pf.printed == 3 and pf.ordered == 1 and pf.progress == 1.0


# ---------- the batched figures (pass 6): one loader, the same arithmetic ----------
#
# ``grouped_figures`` answers for MANY orders what ``load_order_context`` +
# ``attribute`` answer for one. The fixture below is the shared subject: two
# orders on one customer, one of them cancelled, a line overprinted past its
# quantity, and one plate that two products both claim. Every number it produces
# is written out by hand in the parity test, and the products / orders /
# customers suites import the builder so all four screens are pinned to the same
# arithmetic.


async def build_parity_fixture(db) -> dict:
    """Two orders, a cancelled one, an overprint and a shared plate.

    Returns the ids the consumer tests need. Nothing here is derived from the
    code under test - the plate links, the part rows and the archive rows are
    written out, and the figures they imply are asserted literally.
    """
    shared = LibraryFile(
        filename="shared.gcode.3mf",
        file_path="shared",
        file_size=1,
        file_type="gcode",
        file_metadata={
            "plates": [
                {
                    "index": 1,
                    "printable_objects": {"1": "shade", "2": "hook"},
                    "print_time_seconds": 10,
                    "filaments": [{"slot_id": 1, "type": "PETG"}],
                }
            ]
        },
    )
    bases = LibraryFile(
        filename="bases.gcode.3mf",
        file_path="bases",
        file_size=1,
        file_type="gcode",
        file_metadata={
            "plates": [
                {
                    "index": 1,
                    "printable_objects": {"1": "base"},
                    "print_time_seconds": 20,
                    "filaments": [{"slot_id": 1, "type": "PETG"}],
                }
            ]
        },
    )
    lamp, hook_product = Product(name="Parity Lamp"), Product(name="Parity Hook")
    customer = Customer(name="Parity Co")
    db.add_all([shared, bases, lamp, hook_product, customer])
    await db.flush()
    db.add_all(
        [
            ProductPart(
                product_id=lamp.id, kind="printed", name="shade", name_key="shade", qty_per_unit=1, aliases=["shade"]
            ),
            ProductPart(
                product_id=lamp.id, kind="printed", name="base", name_key="base", qty_per_unit=2, aliases=["base"]
            ),
            ProductPart(
                product_id=hook_product.id,
                kind="printed",
                name="hook",
                name_key="hook",
                qty_per_unit=1,
                aliases=["hook"],
            ),
            # The shared plate: ONE bed carrying two products' parts.
            ProductPlate(product_id=lamp.id, library_file_id=shared.id, plate_index=1),
            ProductPlate(product_id=hook_product.id, library_file_id=shared.id, plate_index=1),
            ProductPlate(product_id=lamp.id, library_file_id=bases.id, plate_index=1),
        ]
    )
    live = Project(name="Parity live order", customer_id=customer.id, status="active", price=100.0)
    dead = Project(name="Parity cancelled order", customer_id=customer.id, status="cancelled", price=50.0)
    db.add_all([live, dead])
    await db.flush()
    lines = [
        ProjectLine(project_id=live.id, product_id=lamp.id, quantity=2, sort_order=0),
        ProjectLine(project_id=live.id, product_id=hook_product.id, quantity=1, sort_order=1),
        ProjectLine(project_id=dead.id, product_id=lamp.id, quantity=1, sort_order=0),
    ]
    db.add_all(lines)
    await db.flush()

    async def _print(project_id, file_id, *, cost, energy, rows):
        archive = PrintArchive(
            project_id=project_id,
            library_file_id=file_id,
            plate_index=1,
            filename="p",
            file_path="",
            file_size=0,
            status="completed",
            filament_type="PETG",
            quantity=1,
            cost=cost,
            energy_cost=energy,
            defective_count=sum(defective for _, _, defective in rows),
        )
        db.add(archive)
        await db.flush()
        db.add_all(
            [
                PrintArchivePart(archive_id=archive.id, name=key, name_key=key, quantity=qty, defective=defective)
                for key, qty, defective in rows
            ]
        )

    # 3 shades on a 2-shade line (the overprint) plus the hook line's one hook.
    await _print(live.id, shared.id, cost=1.0, energy=0.5, rows=[("shade", 3, 0), ("hook", 1, 0)])
    # 10 bases against a need of 4 - the surplus that carries the overprint.
    await _print(live.id, bases.id, cost=2.0, energy=None, rows=[("base", 10, 0)])
    # The cancelled order: one usable shade (the other was scrapped), no bases at
    # all, and a hook no line of THIS order counts.
    await _print(dead.id, shared.id, cost=3.0, energy=None, rows=[("shade", 2, 1), ("hook", 1, 0)])
    await db.commit()
    return {
        "customer": customer.id,
        "live": live.id,
        "dead": dead.id,
        "lamp": lamp.id,
        "hook_product": hook_product.id,
        "l1": lines[0].id,
        "l2": lines[1].id,
        "l3": lines[2].id,
    }


async def test_grouped_figures_reproduce_the_per_order_arithmetic(db_session):
    """The batched loader is the only new thing; the arithmetic must be the old one.

    Hand-computed from ``build_parity_fixture``:

    * live order, line 1 (Lamp x2): shade need 2, awarded 2 and the 3rd as
      surplus -> usable 3; base need 4, awarded 4 and 6 as surplus -> usable 10.
      ``units_printed`` = min(3 // 1, 10 // 2) = 3, over the 2 ordered.
    * live order, line 2 (Hook x1): hook usable 1 -> 1 unit.
      Order: ordered 3, printed 4, progress capped at 1.0, cost 1.0+0.5+2.0 = 3.5.
    * cancelled order, line 3 (Lamp x1): shade 2 printed - 1 scrapped = 1 usable;
      no base printed at all -> min(1 // 1, 0 // 2) = 0 units.
      Order: ordered 1, printed 0, progress 0.0, cost 3.0.
    * the product totals: Lamp 3 + 0 = 3 units, Hook 1.
    """
    ids = await build_parity_fixture(db_session)
    orders = {o.project_id: o for o in await grouped_figures(db_session, project_ids=[ids["live"], ids["dead"]])}

    live, dead = orders[ids["live"]], orders[ids["dead"]]
    assert (live.ordered, live.printed, live.progress, live.total_cost) == (3, 4, 1.0, 3.5)
    assert (dead.ordered, dead.printed, dead.progress, dead.total_cost) == (1, 0, 0.0, 3.0)

    by_line = {line.line_id: line for line in [*live.lines, *dead.lines]}
    assert (by_line[ids["l1"]].need, by_line[ids["l1"]].usable_units, by_line[ids["l1"]].printed_units) == (2, 3, 2)
    assert (by_line[ids["l2"]].need, by_line[ids["l2"]].usable_units, by_line[ids["l2"]].printed_units) == (1, 1, 1)
    assert (by_line[ids["l3"]].need, by_line[ids["l3"]].usable_units, by_line[ids["l3"]].printed_units) == (1, 0, 0)

    assert units_delivered(orders.values(), ids["lamp"]) == 3
    assert units_delivered(orders.values(), ids["hook_product"]) == 1

    # ...and field by field against what one order at a time still answers.
    for project_id, grouped in orders.items():
        ctx = await load_order_context(db_session, project_id)
        figs, other = attribute(ctx)
        pf = project_figures(ctx, figs, other)
        assert (grouped.ordered, grouped.printed, grouped.progress, grouped.total_cost) == (
            pf.ordered,
            pf.printed,
            pf.progress,
            pf.total_cost,
        )
        assert {line.line_id: line.usable_units for line in grouped.lines} == {
            line_id: f.units_printed for line_id, f in figs.items()
        }
        assert {line.line_id: line.need for line in grouped.lines} == {
            line_id: f.quantity for line_id, f in figs.items()
        }


async def test_grouped_figures_can_be_asked_by_product(db_session):
    """The product page knows a product, not the orders it sits in - so the
    loader resolves them itself, and finds the cancelled one too."""
    ids = await build_parity_fixture(db_session)
    orders = await grouped_figures(db_session, product_ids=[ids["lamp"]])
    assert sorted(o.project_id for o in orders) == sorted([ids["live"], ids["dead"]])
    assert units_delivered(orders, ids["lamp"]) == 3
