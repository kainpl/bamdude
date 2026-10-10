"""part_images: the effective picture, the editor's state, the candidates (spec §10; plan E4, task 28)."""

import pytest
from sqlalchemy import event, select

from backend.app.models.library import LibraryFile
from backend.app.models.plate_render import PlateRenderObject
from backend.app.models.product import Product, ProductPlate
from backend.app.schemas.part_image import PartImageRef
from backend.app.services import part_images, part_renders
from backend.app.services.product_composition import plate_instance_names, plate_objects
from backend.tests.fixtures.part_images import add_render, metadata, part, png, rendered_farm


def _v(render_id: int, identify_id: int, result_dir: str = "f" * 32) -> str:
    import hashlib

    return hashlib.sha256(f"{render_id}:{result_dir}:{identify_id}".encode()).hexdigest()[:12]


def test_plate_objects_names_the_same_instances_as_plate_instance_names():
    meta = metadata({3: "Body", 1: "Body", 2: "Lid"})
    assert sorted(plate_objects(meta, 1).values()) == sorted(plate_instance_names(meta, 1))
    assert plate_objects(meta, 1) == {3: "Body", 1: "Body", 2: "Lid"}


def test_plate_objects_skips_names_without_ids_and_ids_past_u32():
    assert plate_objects({"plates": [{"index": 1, "objects": ["Body"]}]}, 1) == {}
    assert plate_objects(metadata({4294967296: "Big", 5: "Ok"}), 1) == {5: "Ok"}
    assert plate_objects({"printable_objects": {"7": "Whole"}}, 0) == {7: "Whole"}


@pytest.mark.parametrize("single_plate", [False, True], ids=["two-plate-file", "single-plate-file"])
async def test_the_farm_links_what_the_product_sync_links(db_session, single_plate):
    """Consilium E4.2-R1: the fixture's plates are the sync's own answer, so an import lands on them."""
    from backend.app.services.product_sync import wanted_plate_indices

    farm = await rendered_farm(db_session, single_plate=single_plate)
    linked = set(
        (
            await db_session.execute(select(ProductPlate.plate_index).where(ProductPlate.product_id == farm.product.id))
        ).scalars()
    )
    assert linked == wanted_plate_indices(farm.file.file_metadata) == ({0} if single_plate else {1, 2})
    assert farm.plate == (0 if single_plate else 1) and farm.render.plate_index == farm.plate


async def test_a_single_plate_file_resolves_through_plate_zero(db_session):
    """The commonest real file: one numbered plate, linked as plate 0 (the whole file), rendered as plate 0."""
    farm = await rendered_farm(db_session, single_plate=True)
    got = await part_images.resolve(db_session, [farm.body.id, farm.lid.id])
    assert got[farm.body.id].v == _v(farm.render.id, 11)
    assert got[farm.lid.id].v == _v(farm.render.id, 13)
    refs = await part_images.instance_refs(db_session, [(farm.file.id, 0, 14)])
    assert refs[(farm.file.id, 0, 14)].status == "ready"


async def test_auto_takes_the_first_ready_instance_of_the_part(db_session):
    farm = await rendered_farm(db_session)
    got = await part_images.resolve(db_session, [farm.body.id])
    assert got == {farm.body.id: PartImageRef(kind="render", status="ready", v=_v(farm.render.id, 11))}


async def test_toolpath_wins_over_top_mask_across_sources(db_session):
    farm = await rendered_farm(db_session, methods={11: "top_mask", 12: "top_mask", 13: "top_mask", 14: "toolpath"})
    second = LibraryFile(
        filename="b.gcode.3mf",
        file_path="library/b.gcode.3mf",
        file_type="gcode",
        file_size=1,
        file_hash="b" * 64,
        file_metadata=metadata({21: "Body"}, empty_plates=(2,)),
    )
    db_session.add(second)
    await db_session.flush()
    db_session.add_all(  # the two plates the sync would link for this two-plate file
        ProductPlate(product_id=farm.product.id, library_file_id=second.id, plate_index=p) for p in (1, 2)
    )
    other = await add_render(db_session, sha="b" * 64, methods={21: "toolpath"})
    await db_session.commit()
    got = await part_images.resolve(db_session, [farm.body.id])
    assert got[farm.body.id].v == _v(other.id, 21)


async def test_pending_when_nothing_is_ready_but_a_source_waits(db_session):
    farm = await rendered_farm(db_session, status="pending")
    assert await part_images.resolve(db_session, [farm.body.id]) == {
        farm.body.id: PartImageRef(kind="render", status="pending", v=None)
    }


@pytest.mark.parametrize("status", ["failed", "unavailable", None])
async def test_none_when_no_picture_will_come(db_session, status):
    farm = await rendered_farm(db_session, status=status)
    assert await part_images.resolve(db_session, [farm.body.id]) == {farm.body.id: None}


async def test_missing_and_skipped_instances_are_passed_over(db_session):
    farm = await rendered_farm(db_session, methods={11: "missing", 12: "skipped", 13: "top_mask", 14: "toolpath"})
    assert await part_images.resolve(db_session, [farm.body.id]) == {farm.body.id: None}


async def test_a_trashed_file_is_no_source(db_session):
    farm = await rendered_farm(db_session)
    farm.file.deleted_at = part_renders.utcnow()
    await db_session.commit()
    assert await part_images.resolve(db_session, [farm.body.id]) == {farm.body.id: None}


async def test_an_alias_moves_an_instance_without_a_write(db_session):
    """Review Focus 2, through the real alias doors (consilium E4-R4): an alias that is NOT either
    part's own key moves from Lid to Body. A collision is not a state the doors can make."""
    from backend.app.services.product_composition import add_alias, remove_alias

    farm = await rendered_farm(db_session)
    farm.lid.aliases = ["lid", "cube"]  # Lid owns the Cube object (14) through an alias
    farm.lid.image_source, farm.lid.image_file_id = "instance", farm.file.id
    farm.lid.image_plate_index, farm.lid.image_identify_id = 1, 14
    await db_session.commit()
    before = (await part_images.editor_states(db_session, [farm.lid.id]))[farm.lid.id]
    assert before.pin.valid  # 14 is Lid's while the alias is
    remove_alias(farm.lid, "cube")
    add_alias([farm.body, farm.lid], farm.body, "cube")  # the route's own door: a taken key raises AliasTaken
    await db_session.commit()
    after = (await part_images.editor_states(db_session, [farm.lid.id]))[farm.lid.id]
    assert (after.pin.valid, after.pin.reason) == (False, "not_this_part")
    assert (farm.lid.image_source, farm.lid.image_identify_id) == ("instance", 14)  # nothing wrote the choice
    body = await part_images.candidates(db_session, farm.body.id, lambda f: True)
    assert [c.identify_id for c in body if c.v is not None] == [11, 12, 14]  # the new owner resolves it
    lid = (await part_images.resolve(db_session, [farm.lid.id]))[farm.lid.id]
    assert lid.v == _v(farm.render.id, 13)  # Lid falls back to auto: its own top_mask instance


async def test_a_valid_pin_wins_over_auto(db_session):
    farm = await rendered_farm(db_session)
    farm.body.image_source, farm.body.image_file_id = "instance", farm.file.id
    farm.body.image_plate_index, farm.body.image_identify_id = 1, 12
    await db_session.commit()
    got = await part_images.resolve(db_session, [farm.body.id])
    assert got[farm.body.id].v == _v(farm.render.id, 12)
    state = (await part_images.editor_states(db_session, [farm.body.id]))[farm.body.id]
    assert state.source == "instance" and state.pin.valid and state.pin.reason is None


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("unlink", "file_unlinked"),
        ("trash", "file_trashed"),
        ("gone", "object_gone"),
        ("other_part", "not_this_part"),
        ("skipped", "not_rendered"),
    ],
)
async def test_every_invalid_pin_explains_itself_and_falls_back_to_auto(db_session, setup, reason):
    methods = {11: "toolpath", 12: "skipped", 13: "top_mask", 14: "toolpath"} if setup == "skipped" else None
    farm = await rendered_farm(db_session, methods=methods)
    identify_id = {"gone": 99, "other_part": 13, "skipped": 12}.get(setup, 11)
    farm.body.image_source, farm.body.image_file_id = "instance", farm.file.id
    farm.body.image_plate_index, farm.body.image_identify_id = 1, identify_id
    if setup == "unlink":
        await db_session.execute(ProductPlate.__table__.delete().where(ProductPlate.product_id == farm.product.id))
    if setup == "trash":
        farm.file.deleted_at = part_renders.utcnow()
    await db_session.commit()
    state = (await part_images.editor_states(db_session, [farm.body.id]))[farm.body.id]
    assert (state.pin.valid, state.pin.reason) == (False, reason)
    got = (await part_images.resolve(db_session, [farm.body.id]))[farm.body.id]
    assert got is None or got.v == _v(farm.render.id, 11)  # auto, never the invalid pin


async def test_a_pin_is_judged_per_product(db_session):
    farm = await rendered_farm(db_session)
    twin = Product(name="Twin", library_files=[farm.file])
    db_session.add(twin)
    await db_session.flush()
    twin_body = part(twin.id, "Body")
    db_session.add_all([twin_body, ProductPlate(product_id=twin.id, library_file_id=farm.file.id, plate_index=1)])
    farm.body.image_source, farm.body.image_file_id = "instance", farm.file.id
    farm.body.image_plate_index, farm.body.image_identify_id = 1, 12
    await db_session.commit()
    await db_session.execute(ProductPlate.__table__.delete().where(ProductPlate.product_id == farm.product.id))
    await db_session.commit()
    states = await part_images.editor_states(db_session, [farm.body.id])
    assert states[farm.body.id].pin.reason == "file_unlinked"
    got = await part_images.resolve(db_session, [twin_body.id])
    assert got[twin_body.id].v == _v(farm.render.id, 11)


async def test_a_reslice_keeps_the_pin_and_waits_for_the_new_render(db_session):
    farm = await rendered_farm(db_session)
    farm.body.image_source, farm.body.image_file_id = "instance", farm.file.id
    farm.body.image_plate_index, farm.body.image_identify_id = 1, 12
    farm.file.file_hash = "c" * 64  # the same row, new bytes; the old hash's render stays for GC
    fresh = await add_render(db_session, sha="c" * 64, status="pending")
    await db_session.commit()
    state = (await part_images.editor_states(db_session, [farm.body.id]))[farm.body.id]
    assert state.pin.reason == "not_rendered"
    assert (await part_images.resolve(db_session, [farm.body.id]))[farm.body.id].status == "pending"
    fresh.status, fresh.result_dir = "ready", "e" * 32  # published
    db_session.add(
        PlateRenderObject(render_id=fresh.id, identify_id=12, method="toolpath", width=8, height=8, tools=[])
    )
    await db_session.commit()
    state = (await part_images.editor_states(db_session, [farm.body.id]))[farm.body.id]
    assert state.pin.valid
    got = (await part_images.resolve(db_session, [farm.body.id]))[farm.body.id]
    assert got.v == _v(fresh.id, 12, "e" * 32)


async def test_a_photo_wins_and_a_vanished_photo_falls_back(db_session):
    from backend.app.services.product_files import product_part_images_dir

    farm = await rendered_farm(db_session)
    name = "1" * 32 + ".png"
    farm.body.image_source, farm.body.image_photo = "photo", name
    await db_session.commit()
    png(product_part_images_dir(farm.product.id) / name)
    assert (await part_images.resolve(db_session, [farm.body.id]))[farm.body.id] == PartImageRef(
        kind="photo", status="ready", v=name
    )
    (product_part_images_dir(farm.product.id) / name).unlink()
    assert (await part_images.resolve(db_session, [farm.body.id]))[farm.body.id].kind == "render"
    state = (await part_images.editor_states(db_session, [farm.body.id]))[farm.body.id]
    assert state.photo.present is False


async def test_an_older_renderer_version_is_not_read(db_session):
    farm = await rendered_farm(db_session, status=None)
    old = await add_render(db_session)
    old.renderer_version = part_renders.RENDERER_VERSION - 1
    await db_session.commit()
    assert await part_images.resolve(db_session, [farm.body.id]) == {farm.body.id: None}


async def test_candidates_list_every_instance_of_the_part_and_hide_a_hidden_name(db_session):
    farm = await rendered_farm(db_session, methods={11: "toolpath", 12: "skipped", 13: "top_mask", 14: "toolpath"})
    shown = await part_images.candidates(db_session, farm.body.id, lambda f: True)
    assert [(c.identify_id, c.method, c.plate_status, c.v is not None) for c in shown] == [
        (11, "toolpath", "ready", True),
        (12, "skipped", "ready", False),
    ]
    hidden = await part_images.candidates(db_session, farm.body.id, lambda f: False)
    assert {(c.filename, c.hidden) for c in hidden} == {(None, True)}
    assert await part_images.candidates(db_session, 999_999, lambda f: True) is None


async def test_instance_refs_answer_unassigned_objects(db_session):
    farm = await rendered_farm(db_session)
    got = await part_images.instance_refs(db_session, [(farm.file.id, 1, 14), (farm.file.id, 1, 99)])
    assert got[(farm.file.id, 1, 14)].status == "ready"
    assert got[(farm.file.id, 1, 99)].status == "missing"


async def test_resolve_reads_a_fixed_number_of_statements(db_session, test_engine):
    farm = await rendered_farm(db_session)
    many = [part(farm.product.id, f"Extra {n}") for n in range(40)]
    db_session.add_all(many)
    await db_session.commit()
    counted: list[str] = []
    listener = lambda *args: counted.append(args[2])  # noqa: E731 -- before_cursor_execute(conn, cursor, statement, ...)
    event.listen(test_engine.sync_engine, "before_cursor_execute", listener)
    try:
        await part_images.resolve(db_session, [farm.body.id])
        few = len(counted)
        counted.clear()
        await part_images.resolve(db_session, [farm.body.id, farm.lid.id, *(p.id for p in many)])
        assert len(counted) == few  # 42 parts, the same statements as one
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", listener)


async def test_resolve_crosses_sql_chunk_without_n_plus_one(db_session, test_engine):
    """Review Focus 5 (consilium note 2): more parts than one SQL_CHUNK, over two products, each with its
    own picture. Growth across the chunk boundary is bounded per chunk -- not identical, and not per row."""
    from backend.app.services.product_facets import SQL_CHUNK

    per_product = SQL_CHUNK // 2 + 55
    expected: dict[int, str] = {}
    for n in range(2):
        # "P000x", not "Part 000": a trailing " N" beside its siblings folds into one part (part_names rule 3)
        names = {1000 + i: f"P{i:03d}x" for i in range(per_product)}
        farm = await rendered_farm(
            db_session, objects=names, methods=dict.fromkeys(names, "toolpath"), sha=f"{n + 1}" * 64, name=f"Big{n}"
        )
        parts = {identify_id: part(farm.product.id, name) for identify_id, name in names.items()}
        db_session.add_all(parts.values())
        await db_session.flush()
        expected |= {p.id: _v(farm.render.id, identify_id) for identify_id, p in parts.items()}
    await db_session.commit()
    assert len(expected) > SQL_CHUNK
    counted: list[str] = []
    listener = lambda *args: counted.append(args[2])  # noqa: E731
    event.listen(test_engine.sync_engine, "before_cursor_execute", listener)
    try:
        await part_images.resolve(db_session, list(expected)[:3])
        few = len(counted)
        counted.clear()
        got = await part_images.resolve(db_session, list(expected))
        many = len(counted)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", listener)
    assert {pid: ref.v for pid, ref in got.items()} == expected
    assert many <= 2 * few  # one more round of chunked statements at most; an N+1 would be hundreds


async def test_fill_walks_a_response_once_and_fills_parts_and_instances(db_session):
    from backend.app.schemas.product import PlateRecipeResponse, PlateUnassignedEntry, PlateYieldEntry

    farm = await rendered_farm(db_session)
    plate = PlateRecipeResponse(
        id=1,
        library_file_id=farm.file.id,
        filename=None,
        plate_index=1,
        sliced=True,
        **{"yield": [PlateYieldEntry(part_id=farm.body.id, name="Body", count=2)]},
        unassigned=[PlateUnassignedEntry(name_key="cube", count=1, identify_id=14)],
    )
    await part_images.fill(db_session, [plate])
    assert plate.yield_[0].image.status == "ready"
    assert plate.unassigned[0].image.status == "ready"


async def test_attach_fills_the_answer_and_marks_the_route(db_session):
    from backend.app.schemas.project import DroppedPartOut

    farm = await rendered_farm(db_session)

    @part_images.attach
    async def route(db):
        return DroppedPartOut(part_id=farm.body.id, name="Body", per_before=2, per_after=0, printed=0, queued=0)

    answer = await route(db=db_session)
    assert answer.image.status == "ready"
    assert route.__part_images__ == "image"
