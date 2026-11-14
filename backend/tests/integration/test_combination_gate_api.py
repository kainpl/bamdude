"""Every door that makes a product line, and a line's change of choice, refuses a closed
combination (spec product-part-variant-combinations, п. 10–14).

The jar: a weight (400 standard, 600, 1200), a kind (solid standard, mesh) and a lid
(screw standard, snap). Bodies exist for solid 400 and solid 600 only, the screw lid
fits 400 and 600, the snap lid any weight. Each part is sliced in a file of its own,
so trashing one file takes exactly one part's source away.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.models.library import LibraryFile
from backend.app.models.line_config import ProjectLineChoice, ProjectLinePartCount
from backend.app.models.product import Product, ProductPart, ProductPlate, product_files
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.tests.fixtures.part_bindings import bind

pytestmark = pytest.mark.integration

_GROUPS = (
    ("weight", "Weight", (("w400", "400"), ("w600", "600"), ("w1200", "1200"))),
    ("kind", "Kind", (("solid", "solid"), ("mesh", "mesh"))),
    ("lid", "Lid", (("screw", "screw"), ("snap", "snap"))),
)
_PARTS = (
    ("body_400", "body 400 solid", ("w400", "solid")),
    ("body_600", "body 600 solid", ("w600", "solid")),
    ("screw_lid", "screw lid", ("screw", "w400", "w600")),
    ("snap_lid", "snap lid", ("snap",)),
)


async def _sliced_file(db, name: str) -> LibraryFile:
    f = LibraryFile(
        filename=f"{name}.gcode.3mf",
        file_path=f"{name}.gcode.3mf",
        file_size=1,
        file_type="gcode",
        file_metadata={"plates": [{"index": 1, "printable_objects": {"1": name}, "print_time_seconds": 600}]},
    )
    db.add(f)
    await db.flush()
    return f


async def _make_jar(db, *, weight_standard: str = "w400") -> SimpleNamespace:
    product = Product(name="Jar")
    db.add(product)
    await db.flush()
    ids: dict[str, int] = {"id": product.id}
    options: dict[str, ProductVariantOption] = {}
    for position, (key, name, values) in enumerate(_GROUPS):
        group = ProductVariantGroup(product_id=product.id, name=name, position=position)
        db.add(group)
        await db.flush()
        made = [ProductVariantOption(group_id=group.id, name=n, position=i) for i, (_k, n) in enumerate(values)]
        db.add_all(made)
        await db.flush()
        by_key = {k: option for (k, _n), option in zip(values, made, strict=True)}
        group.default_option_id = (by_key.get(weight_standard) or made[0]).id
        options.update(by_key)
        ids[key] = group.id
        ids.update({k: option.id for k, option in by_key.items()})
    files: dict[str, int] = {}
    for i, (key, name, bound) in enumerate(_PARTS):
        part = bind(
            ProductPart(product_id=product.id, kind="printed", name=name, name_key=name, qty_per_unit=1, sort_order=i),
            *(options[k] for k in bound),
        )
        db.add(part)
        await db.flush()
        f = await _sliced_file(db, name)
        db.add(ProductPlate(product_id=product.id, library_file_id=f.id, plate_index=0))
        await db.execute(product_files.insert().values(product_id=product.id, library_file_id=f.id))
        ids[key] = part.id
        files[key] = f.id
    await db.commit()
    return SimpleNamespace(**ids, files=files)


@pytest.fixture
async def jar(db_session):
    return await _make_jar(db_session)


@pytest.fixture
async def jar_closed_standard(db_session):
    """The same jar whose standard weight is 1200 — no part names it."""
    return await _make_jar(db_session, weight_standard="w1200")


@pytest.fixture
async def order(db_session):
    project = Project(name="Order", status="active")
    db_session.add(project)
    await db_session.commit()
    return project


@pytest.fixture
async def saved_line(committing_client, jar, order):
    """A line of the jar's standard combination — 400, solid, screw: open."""
    r = await committing_client.post(f"/api/v1/projects/{order.id}/lines", json={"product_id": jar.id, "quantity": 1})
    assert r.status_code == 200, r.text
    return SimpleNamespace(order=order, id=r.json()["lines"][0]["id"])


async def _trash(db, file_id: int, *, trashed: bool = True) -> None:
    f = await db.get(LibraryFile, file_id)
    f.deleted_at = datetime.now() if trashed else None
    await db.commit()


def _refusal(r) -> dict:
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "combination_unavailable"
    assert detail["message"] == "That combination is not available"
    return detail


async def _line_ids(db, project_id: int) -> list[int]:
    return (await db.execute(select(ProjectLine.id).where(ProjectLine.project_id == project_id))).scalars().all()


async def _choices(db, line_id: int) -> dict[int, int]:
    rows = await db.execute(
        select(ProjectLineChoice.group_id, ProjectLineChoice.option_id).where(ProjectLineChoice.line_id == line_id)
    )
    return dict(rows.all())


@pytest.mark.asyncio
async def test_add_line_with_closed_combination_is_409(committing_client, db_session, jar, order):
    r = await committing_client.post(
        f"/api/v1/projects/{order.id}/lines",
        json={"product_id": jar.id, "quantity": 1, "choices": {str(jar.kind): jar.mesh}},
    )
    detail = _refusal(r)
    assert detail["reasons"] == [{"code": "option_without_part", "group": "Kind", "option": "mesh", "part": None}]
    assert await _line_ids(db_session, order.id) == []
    # 600 + solid + screw is open: a body and a lid name it, both sliced.
    ok = await committing_client.post(
        f"/api/v1/projects/{order.id}/lines",
        json={"product_id": jar.id, "quantity": 1, "choices": {str(jar.weight): jar.w600}},
    )
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_batch_with_one_closed_line_adds_nothing(committing_client, db_session, jar, order):
    r = await committing_client.post(
        f"/api/v1/projects/{order.id}/lines/batch",
        json={
            "lines": [
                {"kind": "product", "product_id": jar.id, "quantity": 1},
                {"kind": "product", "product_id": jar.id, "quantity": 2, "choices": {str(jar.weight): jar.w1200}},
            ]
        },
    )
    detail = _refusal(r)
    assert {(x["code"], x["group"], x["option"]) for x in detail["reasons"]} == {
        ("option_without_part", "Weight", "1200"),
        ("option_without_part", "Kind", "solid"),
        ("option_without_part", "Lid", "screw"),
    }
    assert await _line_ids(db_session, order.id) == []
    # An order created with a closed line is not created at all.
    created = await committing_client.post(
        "/api/v1/projects/",
        json={
            "name": "Born closed",
            "lines": [{"product_id": jar.id, "quantity": 1, "choices": {str(jar.kind): jar.mesh}}],
        },
    )
    _refusal(created)
    assert (await db_session.execute(select(Project.id).where(Project.name == "Born closed"))).first() is None


@pytest.mark.asyncio
async def test_order_from_files_on_catalog_product_with_closed_standard_is_409(
    committing_client, db_session, jar_closed_standard
):
    jar = jar_closed_standard
    r = await committing_client.post(
        "/api/v1/projects/from-files",
        json={
            "kind": "catalog",
            "name": "From files",
            "product_id": jar.id,
            "file_ids": [jar.files["body_400"]],
            "quantity": 1,
        },
    )
    detail = _refusal(r)
    assert ("option_without_part", "Weight", "1200") in {
        (x["code"], x["group"], x["option"]) for x in detail["reasons"]
    }
    assert (await db_session.execute(select(Project.id).where(Project.name == "From files"))).first() is None


@pytest.mark.asyncio
async def test_changing_choice_to_closed_is_409_and_counts_only_change_is_allowed(
    committing_client, db_session, jar, saved_line
):
    url = f"/api/v1/projects/{saved_line.order.id}/lines/{saved_line.id}/configuration"
    before = await _choices(db_session, saved_line.id)
    detail = _refusal(await committing_client.put(url, json={"choices": {str(jar.kind): jar.mesh}}))
    assert detail["reasons"][0]["code"] == "option_without_part"
    assert await _choices(db_session, saved_line.id) == before
    # The combination closes under the saved line (its body's only source goes to the trash):
    # a change of counts alone, on the same choice, is not checked.
    await _trash(db_session, jar.files["body_400"])
    r = await committing_client.put(url, json={"part_counts": {str(jar.body_400): 2}})
    assert r.status_code == 200, r.text
    counts = await db_session.execute(
        select(ProjectLinePartCount.part_id, ProjectLinePartCount.qty).where(
            ProjectLinePartCount.line_id == saved_line.id
        )
    )
    assert dict(counts.all()) == {jar.body_400: 2}
    # A change of choice is checked against the standard kit — the count does not open it.
    other = await committing_client.put(
        url, json={"choices": {str(jar.lid): jar.snap}, "part_counts": {str(jar.body_400): 2}}
    )
    assert {x["code"] for x in _refusal(other)["reasons"]} == {"part_not_sliced"}


@pytest.mark.asyncio
async def test_dry_run_of_a_closed_choice_is_409_too(committing_client, db_session, jar, saved_line):
    url = f"/api/v1/projects/{saved_line.order.id}/lines/{saved_line.id}/configuration"
    _refusal(await committing_client.put(url, json={"choices": {str(jar.kind): jar.mesh}, "dry_run": True}))
    open_preview = await committing_client.put(url, json={"choices": {str(jar.weight): jar.w600}, "dry_run": True})
    assert open_preview.status_code == 200, open_preview.text


@pytest.mark.asyncio
async def test_saved_line_survives_its_combination_closing(committing_client, db_session, jar, saved_line):
    r = await committing_client.delete(f"/api/v1/products/{jar.id}/parts/{jar.body_400}")
    assert r.status_code == 200, r.text
    got = await committing_client.get(f"/api/v1/projects/{saved_line.order.id}")
    assert got.status_code == 200, got.text
    assert [line["id"] for line in got.json()["lines"]] == [saved_line.id]
    # Re-sending the choice it already has changes nothing and is not checked.
    url = f"/api/v1/projects/{saved_line.order.id}/lines/{saved_line.id}/configuration"
    same = await committing_client.put(url, json={"choices": {str(jar.weight): jar.w400}})
    assert same.status_code == 200, same.text


@pytest.mark.asyncio
async def test_order_copy_is_not_checked(committing_client, db_session, jar, saved_line):
    await _trash(db_session, jar.files["body_400"])  # the saved line's combination is now closed
    r = await committing_client.post(f"/api/v1/projects/{saved_line.order.id}/duplicate", json={})
    assert r.status_code == 200, r.text
    (line,) = r.json()["lines"]
    assert await _choices(db_session, line["id"]) == await _choices(db_session, saved_line.id)


@pytest.mark.asyncio
async def test_parts_line_and_groupless_product_are_not_checked(committing_client, db_session, jar, order):
    await _trash(db_session, jar.files["body_400"])
    parts = await committing_client.post(
        f"/api/v1/projects/{order.id}/lines",
        json={"product_id": jar.id, "mode": "parts", "part_counts": {str(jar.body_400): 3}},
    )
    assert parts.status_code == 200, parts.text
    # A product without variant groups orders as before — its printed part has no source at all.
    draft = Product(name="Draft")
    db_session.add(draft)
    await db_session.flush()
    db_session.add(
        bind(ProductPart(product_id=draft.id, kind="printed", name="shell", name_key="shell", qty_per_unit=1))
    )
    await db_session.commit()
    groupless = await committing_client.post(
        f"/api/v1/projects/{order.id}/lines", json={"product_id": draft.id, "quantity": 2}
    )
    assert groupless.status_code == 200, groupless.text


@pytest.mark.asyncio
async def test_stock_receipt_of_a_closed_configuration_is_allowed(committing_client, jar):
    r = await committing_client.post(
        "/api/v1/stock/moves", json={"kind": "receipt", "product_id": jar.id, "options": [jar.mesh], "qty": 2}
    )
    assert r.status_code == 200, r.text
    assert r.json()["on_hand"] == 2


@pytest.mark.asyncio
async def test_trashed_only_source_closes_then_restore_reopens(committing_client, db_session, jar, order):
    await _trash(db_session, jar.files["body_400"])
    detail = _refusal(
        await committing_client.post(f"/api/v1/projects/{order.id}/lines", json={"product_id": jar.id, "quantity": 1})
    )
    assert detail["reasons"] == [{"code": "part_not_sliced", "group": None, "option": None, "part": "body 400 solid"}]
    await _trash(db_session, jar.files["body_400"], trashed=False)
    r = await committing_client.post(f"/api/v1/projects/{order.id}/lines", json={"product_id": jar.id, "quantity": 1})
    assert r.status_code == 200, r.text
