"""The final review of WS-13 E1 (2026-09-29): refusals the lock protocol promises as 409
reached the operator as 500, a door read its order before the gates it then relied on,
the job-order wizard seeded parts into other products, and an estimate with nothing to
plan answered known zeros."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, insert, select, update

from backend.app.api.routes import projects as project_routes
from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductPartOption
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.services import finished_stock, line_intake, order_fulfilment, product_delete, product_gate
from backend.tests.fixtures.part_bindings import bind

pytestmark = pytest.mark.integration

_ORDER_CHANGED = order_fulfilment.ORDER_CHANGED


@pytest.fixture
async def shop(db_session):
    """An active order with one line of «Lamp» (one printed part), and «Stool» — another
    product with a part, on no line yet."""
    lamp, stool = Product(name="Lamp"), Product(name="Stool")
    db_session.add_all([lamp, stool])
    await db_session.flush()
    db_session.add_all(
        [
            ProductPart(product_id=lamp.id, kind="printed", name="shade", name_key="shade", qty_per_unit=1),
            ProductPart(product_id=stool.id, kind="printed", name="leg", name_key="leg", qty_per_unit=4),
        ]
    )
    order = Project(name="Order", status="active")
    db_session.add(order)
    await db_session.commit()
    return {"lamp": lamp.id, "stool": stool.id, "order": order.id}


async def _add_line(client, order_id, product_id, quantity=1):
    r = await client.post(f"/api/v1/projects/{order_id}/lines", json={"product_id": product_id, "quantity": quantity})
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _raw_line(db, order_id, product_id):
    """A line another request committed meanwhile — written past this session's ORM."""
    await db.execute(
        insert(ProjectLine.__table__).values(
            project_id=order_id, product_id=product_id, quantity=1, mode="product", sort_order=9
        )
    )


def _is_busy(r):
    return r.status_code == 409 and r.json().get("detail", {}).get("error") == "product_busy"


# ---------- Important 1: a one-off product's cascade answers 409 product_busy ----------


@pytest.mark.asyncio
async def test_deleting_a_line_whose_product_is_busy_answers_product_busy(committing_client, shop, monkeypatch):
    body = await _add_line(committing_client, shop["order"], shop["lamp"])
    line_id = body["lines"][0]["id"]

    async def busy(db, product_ids):
        raise product_gate.ProductBusy("product_parts")

    monkeypatch.setattr(product_delete, "delete_orphaned_adhoc_products", busy)
    r = await committing_client.delete(f"/api/v1/projects/{shop['order']}/lines/{line_id}")
    assert _is_busy(r), (r.status_code, r.text)


@pytest.mark.asyncio
async def test_deleting_an_order_whose_product_is_busy_answers_product_busy(committing_client, shop, monkeypatch):
    await _add_line(committing_client, shop["order"], shop["lamp"])

    async def busy(db, product_ids):
        raise product_gate.ProductBusy("product_parts")

    monkeypatch.setattr(product_delete, "delete_orphaned_adhoc_products", busy)
    r = await committing_client.delete(f"/api/v1/projects/{shop['order']}")
    assert _is_busy(r), (r.status_code, r.text)


# ---------- Important 2: a cancel that finds the stock changed answers 409 ----------


@pytest.mark.asyncio
async def test_a_cancel_that_finds_the_stock_changed_answers_409(committing_client, shop, monkeypatch):
    await _add_line(committing_client, shop["order"], shop["lamp"])

    async def changed(db, lines):
        raise finished_stock.FinishedStockError(finished_stock.STOCK_CHANGED, 409)

    monkeypatch.setattr(finished_stock, "lock_lines_with_parts", changed)
    r = await committing_client.patch(f"/api/v1/projects/{shop['order']}", json={"status": "cancelled"})
    assert r.status_code == 409, (r.status_code, r.text)


# ---------- Minor 5: a door reads its order again after the gates ----------


@pytest.mark.asyncio
async def test_completion_refuses_an_order_that_gained_a_product_behind_its_gates(
    committing_client, db_session, shop, monkeypatch
):
    await _add_line(committing_client, shop["order"], shop["lamp"])
    real = order_fulfilment.lock_order

    async def meanwhile(db, project_id):
        await _raw_line(db, project_id, shop["stool"])
        return await real(db, project_id)

    monkeypatch.setattr(order_fulfilment, "lock_order", meanwhile)
    r = await committing_client.patch(f"/api/v1/projects/{shop['order']}", json={"status": "completed"})
    assert (r.status_code, r.json()["detail"]) == (409, _ORDER_CHANGED)


@pytest.mark.asyncio
async def test_deleting_an_order_that_gained_a_product_behind_its_gates_is_refused(
    committing_client, db_session, shop, monkeypatch
):
    await _add_line(committing_client, shop["order"], shop["lamp"])
    real = project_routes.product_gate

    async def meanwhile(db, product_ids):
        await real(db, product_ids)
        await _raw_line(db, shop["order"], shop["stool"])

    monkeypatch.setattr(project_routes, "product_gate", meanwhile)
    r = await committing_client.delete(f"/api/v1/projects/{shop['order']}")
    assert (r.status_code, r.json()["detail"]) == (409, _ORDER_CHANGED)
    assert await db_session.get(Project, shop["order"]) is not None


@pytest.mark.asyncio
async def test_a_copy_refuses_a_source_that_gained_a_product_behind_its_gates(
    committing_client, db_session, shop, monkeypatch
):
    await _add_line(committing_client, shop["order"], shop["lamp"])
    real = project_routes.product_gate

    async def meanwhile(db, product_ids):
        await real(db, product_ids)
        await _raw_line(db, shop["order"], shop["stool"])

    monkeypatch.setattr(project_routes, "product_gate", meanwhile)
    r = await committing_client.post(f"/api/v1/projects/{shop['order']}/duplicate", json={})
    assert (r.status_code, r.json()["detail"]) == (409, _ORDER_CHANGED)


@pytest.mark.asyncio
async def test_lines_are_not_added_to_an_order_deleted_behind_the_gates(
    committing_client, db_session, shop, monkeypatch
):
    real = line_intake.product_gate

    async def meanwhile(db, product_ids):
        await real(db, product_ids)
        await db.execute(delete(Project.__table__).where(Project.id == shop["order"]))

    monkeypatch.setattr(line_intake, "product_gate", meanwhile)
    r = await committing_client.post(
        f"/api/v1/projects/{shop['order']}/lines", json={"product_id": shop["lamp"], "quantity": 1}
    )
    assert r.status_code == 404, (r.status_code, r.text)
    orphans = (await db_session.execute(select(ProjectLine.id).where(ProjectLine.project_id == shop["order"]))).all()
    assert orphans == []


@pytest.mark.asyncio
async def test_lines_added_to_an_order_cancelled_behind_the_gates_take_no_stock(
    committing_client, db_session, shop, monkeypatch
):
    real = line_intake.product_gate
    asked = []

    async def meanwhile(db, product_ids):
        await real(db, product_ids)
        await db.execute(update(Project.__table__).where(Project.id == shop["order"]).values(status="cancelled"))

    async def spy(db, line, spec):
        asked.append(line.id)
        return 0, 0

    monkeypatch.setattr(line_intake, "product_gate", meanwhile)
    monkeypatch.setattr(line_intake, "_asked", spy)
    await committing_client.post(
        f"/api/v1/projects/{shop['order']}/lines", json={"product_id": shop["lamp"], "quantity": 1}
    )
    assert asked == [], "a line of an order no longer active reserves nothing"


# ---------- Important 3: the job-order wizard seeds its own product only ----------


@pytest.mark.asyncio
async def test_a_job_order_seeds_its_own_parts_not_the_catalogue_products(committing_client, db_session):
    """The file already belongs to a catalogue product that lacks one of its objects:
    the wizard's one-off product must not seed it back (WS-13 E1 BL8 б)."""
    from backend.app.services.product_sync import sync_product_for_file

    f = LibraryFile(
        filename="job.gcode.3mf",
        file_path="job.gcode.3mf",
        file_size=1,
        file_type="gcode",
        file_metadata={"plates": [{"index": 1, "printable_objects": {"1": "flask", "2": "cap"}}]},
    )
    catalogue = Product(name="Catalogue flask")
    db_session.add_all([f, catalogue])
    await db_session.flush()
    await sync_product_for_file(db_session, library_file_id=f.id, product_ids=[catalogue.id])
    await db_session.flush()
    await db_session.execute(
        delete(ProductPart.__table__).where(ProductPart.product_id == catalogue.id, ProductPart.name_key == "cap")
    )
    await db_session.commit()

    r = await committing_client.post(
        "/api/v1/projects/from-files",
        json={"kind": "job", "name": "Job", "file_ids": [f.id], "targets": {"flask": 2}},
    )
    assert r.status_code in (200, 201), r.text
    kept = set(await db_session.scalars(select(ProductPart.name_key).where(ProductPart.product_id == catalogue.id)))
    assert kept == {"flask"}, "the catalogue product got a part seeded behind its back"


# ---------- implementation review, round 1 (Codex): stale reads behind the gates ----------


@pytest.fixture
async def shade_lamp(db_session):
    """«Lamp»: a Colour group (red standard, blue); part «shade» bound to red, part «base»."""
    from backend.app.services import product_variants

    lamp = Product(name="Shade lamp")
    db_session.add(lamp)
    await db_session.flush()
    group = await product_variants.create_group(db_session, lamp.id, "Colour", ["red", "blue"], record_lines=False)
    red, blue = [o.id for o in group.options]
    shade = bind(
        ProductPart(product_id=lamp.id, kind="printed", name="shade", name_key="shade", qty_per_unit=1),
        group.options[0],
    )
    base = ProductPart(product_id=lamp.id, kind="printed", name="base", name_key="base", qty_per_unit=1)
    db_session.add_all([shade, base])
    await db_session.commit()
    return {"lamp": lamp.id, "group": group.id, "red": red, "blue": blue, "shade": shade.id, "base": base.id}


@pytest.mark.asyncio
async def test_the_revision_describes_the_groups_the_same_response_returns(committing_client, db_session, monkeypatch):
    """V01: a group committed between the response's read of the groups and the end of
    the GET must not ride into its revision — an unchanged draft from that response is
    409 and the unseen group survives."""
    from backend.app.api.routes import products as product_routes
    from backend.app.services import product_variants

    lamp = Product(name="Snapshot lamp")
    db_session.add(lamp)
    await db_session.commit()
    real = product_routes._variant_groups_out
    injected = []

    async def meanwhile(db, *args):
        result = await real(db, *args)
        if not injected:
            injected.append(True)
            await product_variants.create_group(db, lamp.id, "Unseen", ["default"], record_lines=False)
        return result

    monkeypatch.setattr(product_routes, "_variant_groups_out", meanwhile)
    state = (await committing_client.get(f"/api/v1/products/{lamp.id}")).json()
    assert state["variant_groups"] == []
    saved = await committing_client.put(
        f"/api/v1/products/{lamp.id}/variants", json={"revision": state["variants_revision"], "groups": []}
    )
    assert saved.status_code == 409 and saved.json()["detail"]["error"] == "variants_changed", saved.text
    assert [
        g["name"] for g in (await committing_client.get(f"/api/v1/products/{lamp.id}")).json()["variant_groups"]
    ] == ["Unseen"]


@pytest.mark.asyncio
async def test_a_rename_after_the_read_is_not_overwritten_by_the_old_name(
    committing_client, db_session, shade_lamp, monkeypatch
):
    """V01: the response's names and its token belong to one snapshot — a rename that
    lands after the read cannot be undone by sending the response back unchanged."""
    from backend.app.api.routes import products as product_routes
    from backend.app.models.product_variant import ProductVariantOption

    real = product_routes._variant_groups_out
    injected = []

    async def meanwhile(db, *args):
        result = await real(db, *args)
        if not injected:
            injected.append(True)
            await db.execute(
                update(ProductVariantOption.__table__)
                .where(ProductVariantOption.id == shade_lamp["red"])
                .values(name="crimson")
            )
        return result

    monkeypatch.setattr(product_routes, "_variant_groups_out", meanwhile)
    state = (await committing_client.get(f"/api/v1/products/{shade_lamp['lamp']}")).json()
    monkeypatch.setattr(product_routes, "_variant_groups_out", real)
    [group] = state["variant_groups"]
    assert [o["name"] for o in group["options"]] == ["red", "blue"]
    draft = [
        {
            "id": group["id"],
            "name": group["name"],
            "default": group["default_option_id"],
            "options": [{"id": o["id"], "name": o["name"]} for o in group["options"]],
        }
    ]
    saved = await committing_client.put(
        f"/api/v1/products/{shade_lamp['lamp']}/variants",
        json={"revision": state["variants_revision"], "groups": draft},
    )
    assert saved.status_code == 409, saved.text
    names = [
        o["name"]
        for o in (await committing_client.get(f"/api/v1/products/{shade_lamp['lamp']}")).json()["variant_groups"][0][
            "options"
        ]
    ]
    assert names == ["crimson", "blue"]


@pytest.mark.asyncio
async def test_a_rebinding_reads_the_part_again_behind_the_gate(committing_client, db_session, shade_lamp, monkeypatch):
    """V02: another writer rebinds the part to blue and commits while this request waits
    at the gate; this request asked for red and must leave red — never 200 with blue."""
    from backend.app.services import product_gate as gate_module

    real = gate_module.product_gate

    async def meanwhile(db, ids):
        await db.execute(delete(ProductPartOption.__table__).where(ProductPartOption.part_id == shade_lamp["shade"]))
        await db.execute(
            insert(ProductPartOption.__table__).values(
                part_id=shade_lamp["shade"], option_id=shade_lamp["blue"], group_id=shade_lamp["group"]
            )
        )
        await real(db, ids)

    monkeypatch.setattr(gate_module, "product_gate", meanwhile)
    r = await committing_client.patch(
        f"/api/v1/products/{shade_lamp['lamp']}/parts/{shade_lamp['shade']}",
        json={"variant_option_ids": [shade_lamp["red"]]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["variant_option_ids"] == [shade_lamp["red"]]
    stored = (
        await db_session.scalars(
            select(ProductPartOption.option_id).where(ProductPartOption.part_id == shade_lamp["shade"])
        )
    ).all()
    assert stored == [shade_lamp["red"]]


@pytest.mark.asyncio
async def test_a_merge_reads_both_parts_again_behind_the_gate(committing_client, db_session, shade_lamp, monkeypatch):
    """V02's neighbour: an alias another writer gave the source while this merge waited
    is merged, not lost to the copy read before the gate."""
    from backend.app.services import product_gate as gate_module

    real = gate_module.product_gate

    async def meanwhile(db, ids):
        await db.execute(
            update(ProductPart.__table__).where(ProductPart.id == shade_lamp["base"]).values(aliases=["plinth"])
        )
        await real(db, ids)

    monkeypatch.setattr(gate_module, "product_gate", meanwhile)
    r = await committing_client.post(
        f"/api/v1/products/{shade_lamp['lamp']}/parts/{shade_lamp['shade']}/merge",
        json={"source_part_id": shade_lamp["base"]},
    )
    assert r.status_code == 200, r.text
    assert "plinth" in r.json()["aliases"]


async def _issue_ready_order(db):
    """An active order of ACME with two ready units reserved on its line."""
    from backend.app.models.customer import Customer
    from backend.app.services import finished_stock, line_config

    pipe = Product(name="Pipe for issue")
    acme, other = Customer(name="ACME issue"), Customer(name="Beta issue")
    db.add_all([pipe, acme, other])
    await db.flush()
    db.add(ProductPart(product_id=pipe.id, kind="printed", name="pipe", name_key="pipe", qty_per_unit=1))
    await db.commit()
    item = await finished_stock.item_for(db, pipe.id, {}, create=True)
    await finished_stock.receive(db, item, 2)
    await db.commit()
    order = Project(name="Issue order", status="active", customer_id=acme.id)
    db.add(order)
    await db.flush()
    line = ProjectLine(project_id=order.id, product_id=pipe.id, quantity=2)
    db.add(line)
    await db.flush()
    await line_config.seed_line(db, line, choices=None, counts=None)
    await db.commit()
    await finished_stock.reserve_for_line(db, line, 2)
    await db.commit()
    return {"order": order.id, "line": line.id, "other": other.id}


@pytest.mark.asyncio
@pytest.mark.parametrize("to_customer", ["another", "none"])
async def test_an_issue_refuses_when_the_customer_changed_behind_the_lock(
    committing_client, db_session, monkeypatch, to_customer
):
    """V03: the order's customer changed (or was taken away) and committed while this
    issue waited for the order row — no issue on the old customer, no movement: 409."""
    from backend.app.models.finished_stock import StockItemMovement
    from backend.app.models.stock_issue import StockIssue

    shop = await _issue_ready_order(db_session)
    target = shop["other"] if to_customer == "another" else None
    real = order_fulfilment.lock_order

    async def meanwhile(db, project_id):
        await db.execute(
            update(Project.__table__).where(Project.id == project_id).values(customer_id=target, contact_id=None)
        )
        await real(db, project_id)

    monkeypatch.setattr(order_fulfilment, "lock_order", meanwhile)
    movements_before = len((await db_session.execute(select(StockItemMovement.id))).all())
    r = await committing_client.post(
        f"/api/v1/projects/{shop['order']}/fulfilment",
        json={"lines": [{"line_id": shop["line"], "issue": 1}], "recipient": {"name": "Typed by hand"}},
    )
    assert (r.status_code, r.json()["detail"]) == (409, _ORDER_CHANGED), r.text
    assert (await db_session.execute(select(StockIssue.id))).all() == []
    assert len((await db_session.execute(select(StockItemMovement.id))).all()) == movements_before
