from collections import Counter
from contextlib import contextmanager
from datetime import datetime

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import selectinload

from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.services.product_composition import (
    add_alias,
    merge_parts,
    part_index,
    plate_key_counts,
    plate_materials,
    purchased_name_key,
    recipe_for,
    recipes_for_product,
    recipes_for_products,
    remove_alias,
)


@contextmanager
def counting_statements(engine, *, match: str | None = None):
    """Every SQL statement an engine runs inside the block.

    The N+1 these tests exist to pin is invisible to a functional assertion —
    the answers are identical either way — so the number of round trips IS the
    behaviour under test.
    """
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if match is None or match in statement:
            seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


META = {
    "plates": [
        {
            "index": 1,
            "objects": ["bracket.stl", "lid.stl"],
            "printable_objects": {"1": "bracket.stl", "2": "bracket.stl_2", "3": "lid.stl"},
            "print_time_seconds": 3600,
            "filament_used_grams": 40.0,
            "filaments": [
                {"slot_id": 1, "type": "PETG", "color": "#000000"},
                {"slot_id": 2, "type": "petg", "color": "#FFFFFF"},
            ],
        },
        {"index": 2, "objects": ["clip.stl"], "printable_objects": {str(i): f"clip.stl_{i}" for i in range(1, 11)}},
    ]
}


def _part(pid, key, aliases=None, qty=1, kind="printed"):
    p = ProductPart(product_id=1, kind=kind, name=key, name_key=key, qty_per_unit=qty, aliases=aliases or [key])
    p.id = pid
    return p


def test_instances_are_counted_from_printable_objects_not_the_deduplicated_list():
    counts, display = plate_key_counts(META, 1)
    assert counts == Counter({"bracket.stl": 2, "lid.stl": 1})
    assert display["bracket.stl"] == "bracket.stl"
    counts2, _ = plate_key_counts(META, 2)
    assert counts2 == Counter({"clip.stl": 10})


def test_plate_zero_means_the_whole_file():
    counts, _ = plate_key_counts(META, 0)
    assert counts == Counter({"bracket.stl": 2, "lid.stl": 1, "clip.stl": 10})


def test_materials_are_upper_cased_tokens():
    assert plate_materials(META, 1) == {"PETG"}
    assert plate_materials(META, 2) == set()


def test_recipe_resolves_through_aliases_and_reports_unassigned():
    bracket = _part(10, "bracket", aliases=["bracket", "bracket.stl"])
    plate = ProductPlate(product_id=1, library_file_id=5, plate_index=1)
    recipe = recipe_for(plate, META, "gcode", [bracket])
    assert recipe.sliced is True
    assert recipe.yield_by_part == {10: 2}
    assert recipe.unassigned == {"lid.stl": 1}
    assert recipe.print_time_seconds == 3600 and recipe.filament_used_grams == 40.0


def test_unsliced_plate_is_flagged():
    plate = ProductPlate(product_id=1, library_file_id=5, plate_index=2)
    recipe = recipe_for(plate, META, "3mf", [])
    assert recipe.sliced is False and recipe.print_time_seconds is None


def test_merge_unions_aliases_and_keeps_target_qty():
    a, b = _part(1, "bracket", qty=4), _part(2, "bracket_v2", aliases=["bracket_v2", "bracket-old"], qty=9)
    merge_parts(a, b)
    assert a.qty_per_unit == 4 and set(a.aliases) == {"bracket", "bracket_v2", "bracket-old"} and a.auto is False


def test_alias_must_be_unique_within_the_product():
    a, b = _part(1, "bracket"), _part(2, "lid")
    with pytest.raises(ValueError):
        add_alias([a, b], a, "lid")
    add_alias([a, b], a, "bracket.stl")
    assert "bracket.stl" in a.aliases
    with pytest.raises(ValueError):
        remove_alias(a, "bracket")  # a part always keeps its own key
    remove_alias(a, "bracket.stl")
    assert a.aliases == ["bracket"]


def test_removing_the_last_alias_leaves_the_parts_own_key_not_an_empty_list():
    """One spelling of "no aliases but my own", not two.

    ``add_alias`` and ``merge_parts`` write ``aliases or [name_key]``, so a
    printed part that has a list always carries its own key in it. Emptying the
    list writes the same fact in a spelling nothing else produces, and two rows
    that mean the same thing compare as different. A part with no list at all —
    a purchased one, whose aliases are ``None`` by the model's contract — is
    not given one here.
    """
    a = _part(1, "bracket", aliases=["bracket.stl"])  # a list without its own key
    remove_alias(a, "bracket.stl")
    assert a.aliases == ["bracket"]

    screw = _part(2, purchased_name_key("M3 Screw"), kind="purchased")
    screw.aliases = None
    remove_alias(screw, "anything")
    assert screw.aliases == []


def test_part_index_covers_own_key_and_aliases_and_purchased_prefix():
    a = _part(1, "bracket", aliases=["bracket", "bracket.stl"])
    s = _part(2, purchased_name_key("M3 Screw"), kind="purchased")
    idx = part_index([a, s])
    assert idx["bracket.stl"] is a and idx["purchased:m3 screw"] is s


def test_whole_file_totals_sum_only_the_plates_that_carry_a_figure():
    # Top-level keys are ONE plate's snapshot — a half-sliced file must not
    # report plate 1's 3600s as the whole file's time.
    meta = {
        "print_time_seconds": 3600,
        "filament_used_grams": 40.0,
        "plates": [
            {"index": 1, "printable_objects": {"1": "a.stl"}, "print_time_seconds": 3600, "filament_used_grams": 40.0},
            {"index": 2, "printable_objects": {"1": "b.stl"}},
            {"index": 3, "printable_objects": {"1": "c.stl"}, "print_time_seconds": 1800, "filament_used_grams": 10.0},
        ],
    }
    recipe = recipe_for(ProductPlate(product_id=1, library_file_id=5, plate_index=0), meta, "3mf", [])
    assert recipe.print_time_seconds == 5400
    assert recipe.filament_used_grams == 50.0


def test_sliced_asks_the_content_flag_not_the_filename():
    meta = {"plates": [{"index": 1, "printable_objects": {"1": "a.stl"}}]}
    whole = ProductPlate(product_id=1, library_file_id=5, plate_index=0)
    # detect_file_type says "gcode" by filename; a missing flag counts as sliced.
    assert recipe_for(whole, meta, "gcode", []).sliced is True
    assert recipe_for(whole, {**meta, "has_sliced_gcode": False}, "gcode", []).sliced is False
    assert recipe_for(whole, meta, "3mf", []).sliced is False
    # ...and the other half of is_printable(): a "3mf" row the content check
    # found gcode inside IS printable, but only on the strength of the flag.
    assert recipe_for(whole, {**meta, "has_sliced_gcode": True}, "3mf", []).sliced is True
    # One plate of a multi-plate file is decided by its own timing alone.
    one = ProductPlate(product_id=1, library_file_id=5, plate_index=1)
    assert recipe_for(one, meta, "gcode", []).sliced is False
    # The flag is a TRI-STATE whose domain is bools, and anything else reads as
    # "no answer" — the state a row written before m137 is in. It comes out of
    # JSON, so this is not hypothetical; ``is not False`` accepted every value
    # that is not the ``False`` singleton as a yes, and got the same answer here
    # only by accident. Both branches now read an out-of-domain value exactly as
    # they read an absent one.
    for junk in (0, 1, "true", "false", []):
        assert recipe_for(whole, {**meta, "has_sliced_gcode": junk}, "gcode", []).sliced is True
        assert recipe_for(whole, {**meta, "has_sliced_gcode": junk}, "3mf", []).sliced is False


async def test_recipes_for_product_drops_a_trashed_files_plates_and_orders_by_plate_id(db_session):
    """The one loop that turns a product's plates into recipes.

    A trashed file is restorable, so its ``product_plates`` rows stay — but
    nothing may offer them as something to print. Order is ``ProductPlate.id``
    so the engine and the route read the same sequence; the route sorts for
    display itself.
    """
    live = LibraryFile(filename="live.gcode.3mf", file_path="live", file_size=1, file_type="gcode", file_metadata=META)
    trashed = LibraryFile(
        filename="gone.gcode.3mf",
        file_path="gone",
        file_size=1,
        file_type="gcode",
        file_metadata=META,
        deleted_at=datetime(2026, 1, 1),
    )
    product = Product(name="Box")
    db_session.add_all([live, trashed, product])
    await db_session.commit()

    bracket = ProductPart(
        product_id=product.id,
        kind="printed",
        name="bracket",
        name_key="bracket",
        qty_per_unit=1,
        aliases=["bracket", "bracket.stl"],
    )
    # Inserted so that plate id order and (library_file_id, plate_index) order
    # disagree — the assertion below would pass by accident otherwise.
    db_session.add_all(
        [
            bracket,
            ProductPlate(product_id=product.id, library_file_id=live.id, plate_index=2),
            ProductPlate(product_id=product.id, library_file_id=trashed.id, plate_index=1),
            ProductPlate(product_id=product.id, library_file_id=live.id, plate_index=1),
        ]
    )
    await db_session.commit()

    loaded = (
        await db_session.execute(
            select(Product)
            .options(selectinload(Product.parts), selectinload(Product.plates))
            .where(Product.id == product.id)
        )
    ).scalar_one()

    rows = await recipes_for_product(db_session, loaded)

    assert [(f.id, p.plate_index) for p, f, _ in rows] == [(live.id, 2), (live.id, 1)]
    assert [p.id for p, _, _ in rows] == sorted(p.id for p, _, _ in rows)
    for plate, file, recipe in rows:
        expected = recipe_for(plate, file.file_metadata, file.file_type, loaded.parts)
        assert recipe.yield_by_part == expected.yield_by_part
        assert recipe.unassigned == expected.unassigned
        assert recipe.sliced == expected.sliced
    assert rows[1][2].yield_by_part == {bracket.id: 2}  # plate 1 of META: two bracket instances


async def _loaded(db, product_id: int) -> Product:
    return (
        await db.execute(
            select(Product)
            .options(selectinload(Product.parts), selectinload(Product.plates))
            .where(Product.id == product_id)
        )
    ).scalar_one()


async def test_recipes_for_products_reads_every_products_files_in_one_query(db_session, test_engine):
    """The batch is the whole point: an order of N products used to cost N
    SELECTs against ``library_files``, one per product, from the plan engine and
    again from the enqueue handler. The answers were never wrong — only the
    number of round trips — so the count IS the assertion.
    """
    files = [
        LibraryFile(filename=f"f{i}.gcode.3mf", file_path=f"f{i}", file_size=1, file_type="gcode", file_metadata=META)
        for i in range(3)
    ]
    products = [Product(name=f"P{i}") for i in range(3)]
    db_session.add_all([*files, *products])
    await db_session.commit()
    for product, file in zip(products, files, strict=True):
        db_session.add_all(
            [
                ProductPart(
                    product_id=product.id,
                    kind="printed",
                    name="bracket",
                    name_key="bracket",
                    qty_per_unit=1,
                    aliases=["bracket", "bracket.stl"],
                ),
                ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=1),
                ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=2),
            ]
        )
    await db_session.commit()
    loaded = [await _loaded(db_session, p.id) for p in products]

    with counting_statements(test_engine, match="library_files") as batched:
        by_product = await recipes_for_products(db_session, loaded)
    assert len(batched) == 1, batched

    # Every product answered, and answered exactly as the single-product helper
    # would have — that one is now a wrapper over this.
    assert set(by_product) == {p.id for p in products}
    with counting_statements(test_engine, match="library_files") as singly:
        for product in loaded:
            assert [(plate.id, file.id, recipe.yield_by_part) for plate, file, recipe in by_product[product.id]] == [
                (plate.id, file.id, recipe.yield_by_part)
                for plate, file, recipe in await recipes_for_product(db_session, product)
            ]
    assert len(singly) == 3, "one per product — that is the shape the batch replaces"


async def test_recipes_for_products_answers_a_plateless_product_without_a_query(db_session, test_engine):
    """A dict entry for every product asked about, so a caller can index without
    guarding — and NOT ONE statement when none of them has a plate.

    ⚠️ Every statement, not just the ``library_files`` one, and that is the
    whole assertion: the plateless path must return before ``product.parts`` is
    read, so a caller that loaded neither parts nor plates is not punished for
    a question whose answer is empty either way. The single-product wrapper
    goes through the same path and is asserted here too — the test that used to
    cover it separately could only see the empty ANSWER, never the query.
    """
    products = [Product(name="Empty A"), Product(name="Empty B")]
    db_session.add_all(products)
    await db_session.commit()
    ids = [p.id for p in products]
    loaded = [await _loaded(db_session, pid) for pid in ids]

    with counting_statements(test_engine) as seen:
        by_product = await recipes_for_products(db_session, loaded)
        assert await recipes_for_product(db_session, loaded[0]) == []

    assert by_product == {pid: [] for pid in ids}
    assert seen == []
    assert await recipes_for_products(db_session, []) == {}

    # And the half a statement count cannot see: a caller that loaded PLATES but
    # not parts. An unloaded collection in an async session is a
    # ``MissingGreenlet``, not a SELECT, so reading ``product.parts`` on this
    # path would blow up rather than show up above.
    db_session.expunge_all()
    plates_only = (
        await db_session.execute(select(Product).options(selectinload(Product.plates)).where(Product.id == ids[0]))
    ).scalar_one()
    assert await recipes_for_products(db_session, [plates_only]) == {ids[0]: []}
