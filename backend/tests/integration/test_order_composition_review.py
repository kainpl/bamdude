"""Independent tests with the candidate's committing HTTP fixture and isolated SQLite database."""

import pytest
from sqlalchemy import select, update

from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.product import Product, ProductPart
from backend.app.models.project import ProjectEvent
from backend.app.models.project_line import ProjectLine
from backend.app.models.stock_issue import StockIssue
from backend.app.services import finished_stock, line_config, part_stock
from backend.tests.integration.test_part_one_request import lamp as lamp

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def urls(db, setup, label):
    line = await db.get(ProjectLine, setup["lines"][label])
    order = f"/api/v1/projects/{line.project_id}"
    return order, f"{order}/lines/{line.id}"


async def add_catalog(http, setup, name):
    r = await http.post(f"/api/v1/products/{setup['product']}/parts", json={"kind": "purchased", "name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def add_body(http, url, part):
    r = await http.get(url + "/part-candidates")
    assert r.status_code == 200
    meta = r.json()
    return {
        "part_id": part,
        "quantity": 1,
        "expected_revision": meta["revision"],
        "catalog_version": meta["catalog_version"],
        "expected_quantity": meta["quantity"],
        "all_parts": True,
        "command_id": "operator-1",
    }


async def test_existing_line_stock_suggestion(committing_client, db_session, lamp):
    http = committing_client
    _, url = await urls(db_session, lamp, "L_A")
    await add_catalog(http, lamp, "Diffuser")
    opts = {lamp["group"]: lamp["options"]["A"]}
    new = await finished_stock.item_for(db_session, lamp["product"], opts, create=True)
    await finished_stock.receive(db_session, new, 1)
    await db_session.commit()
    suggested = await http.post(
        "/api/v1/stock/suggest",
        json={
            "items": [
                {
                    "product_id": lamp["product"],
                    "options": list(opts.values()),
                    "quantity": 1,
                    "line_id": lamp["lines"]["L_A"],
                }
            ]
        },
    )
    assert suggested.status_code == 200, suggested.text
    answer = suggested.json()["items"][0]
    actual = await http.patch(url, json={"from_finished": 1})
    assert actual.status_code == 200, actual.text
    row = next(r for r in actual.json()["lines"] if r["id"] == lamp["lines"]["L_A"])
    assert answer["from_finished"] == row["from_finished"] == 0


async def test_first_revision_needs_confirmed_variant_change(committing_client, db_session, lamp):
    http = committing_client
    _, url = await urls(db_session, lamp, "L_A")
    await add_catalog(http, lamp, "Late common diffuser")
    # Both choices were available in the seed. With no option-specific part the group remains open.
    r = await http.put(
        url + "/configuration", json={"choices": {str(lamp["group"]): lamp["options"]["B"]}, "part_counts": {}}
    )
    h = (await http.get(url + "/composition-revisions")).json()
    assert (r.status_code, len(h)) == (409, 1)
    proposed = {"choices": {str(lamp["group"]): lamp["options"]["B"]}}
    review = await http.put(url + "/configuration", json={**proposed, "dry_run": True})
    assert review.status_code == 200, review.text
    missing_review = await http.put(url + "/configuration", json={**proposed, "expected_revision": 1})
    assert missing_review.status_code == 409
    accepted = await http.put(
        url + "/configuration",
        json={
            **proposed,
            "expected_revision": 1,
            "review_version": review.json()["review_version"],
        },
    )
    assert accepted.status_code == 200, accepted.text


async def test_single_add_preserves_other_saved_labels(committing_client, db_session, lamp):
    http = committing_client
    order, url = await urls(db_session, lamp, "L_O")

    def row(doc):
        return next(r for r in doc["lines"] if r["id"] == lamp["lines"]["L_O"])

    old = row((await http.get(order)).json())["configuration"]["changed_parts"][0]
    renamed = await http.patch(
        f"/api/v1/products/{lamp['product']}/parts/{lamp['shade']}", json={"name": "Renamed shade", "qty_per_unit": 4}
    )
    assert renamed.status_code == 200, renamed.text
    new = await add_catalog(http, lamp, "Washer")
    result = await http.post(url + "/parts", json=await add_body(http, url, new))
    assert result.status_code == 200, result.text
    saved = row((await http.get(order)).json())
    after = next(p for p in saved["configuration"]["changed_parts"] if p["part_id"] == lamp["shade"])
    assert old == after
    duplicate = await http.post(order + "/duplicate", json={})
    assert duplicate.status_code == 200, duplicate.text
    assert old in duplicate.json()["lines"][2]["configuration"]["changed_parts"]


async def test_purchased_parts_with_reused_id_in_two_products(committing_client, db_session, lamp):
    http = committing_client
    order, url = await urls(db_session, lamp, "L_A")
    old_id = await add_catalog(http, lamp, "Original purchased ring")
    r = await http.post(url + "/parts", json=await add_body(http, url, old_id))
    assert r.status_code == 200, r.text
    removed = await http.delete(f"/api/v1/products/{lamp['product']}/parts/{old_id}")
    assert removed.status_code == 200, removed.text
    other = Product(name="Other lamp")
    db_session.add(other)
    await db_session.flush()
    replacement = ProductPart(
        product_id=other.id, kind="purchased", name="Unrelated switch", name_key="purchased:switch", qty_per_unit=3
    )
    db_session.add(replacement)
    await db_session.flush()
    assert replacement.id == old_id
    original = await db_session.get(ProjectLine, lamp["lines"]["L_A"])
    line = ProjectLine(project_id=original.project_id, product_id=other.id, quantity=1, mode="product")
    db_session.add(line)
    await db_session.flush()
    await line_config.seed_line(db_session, line, choices=None, counts=None)
    await db_session.commit()
    doc = await http.get(order)
    assert doc.status_code == 200, doc.text
    data = doc.json()
    rows = data["procurement"]
    assert {(p["name"], p["need"]) for p in rows} == {("Original purchased ring", 1), ("Unrelated switch", 3)}
    archived = next(row for row in rows if row["name"] == "Original purchased ring")
    live = next(row for row in rows if row["name"] == "Unrelated switch")
    assert archived["part_id"] < 0 and live["part_id"] == old_id
    assert archived["part_identity"] != live["part_identity"]
    path = order + f"/procurement/{old_id}"
    assert (
        await http.patch(path, json={"quantity_acquired": 1, "part_identity": archived["part_identity"]})
    ).status_code == 404
    assert (await http.patch(path, json={"quantity_acquired": 1})).status_code == 404
    bought = await http.patch(path, json={"quantity_acquired": 2, "part_identity": live["part_identity"]})
    assert bought.status_code == 200, bought.text
    assert {row["name"]: row["acquired"] for row in bought.json()["procurement"]} == {
        "Original purchased ring": 0,
        "Unrelated switch": 2,
    }


async def _add_saved_part(http, url, part_id, quantity, command_id):
    body = await add_body(http, url, part_id)
    response = await http.post(url + "/parts", json={**body, "quantity": quantity, "command_id": command_id})
    assert response.status_code == 200, response.text


async def _delete_catalog_part(http, product_id, part_id):
    response = await http.delete(f"/api/v1/products/{product_id}/parts/{part_id}")
    assert response.status_code == 200, response.text


async def test_edit_keeps_distinct_archived_parts_and_their_descriptions(committing_client, db_session, lamp):
    http = committing_client
    order, url = await urls(db_session, lamp, "L_A")
    line_id = lamp["lines"]["L_A"]
    ring = await add_catalog(http, lamp, "Ring")
    await _add_saved_part(http, url, ring, 2, "ring")
    await _delete_catalog_part(http, lamp["product"], ring)
    switch = await add_catalog(http, lamp, "Switch")
    assert switch == ring
    await _add_saved_part(http, url, switch, 5, "switch")
    doc = (await http.get(order)).json()
    line = next(row for row in doc["lines"] if row["id"] == line_id)
    assert {part["name"]: part["qty"] for part in line["configuration"]["changed_parts"]} == {"Ring": 2, "Switch": 5}

    await _delete_catalog_part(http, lamp["product"], switch)
    edited = await http.put(
        url + "/configuration", json={"part_edits": {str(lamp["shade"]): 3}, "expected_revision": 3}
    )
    assert edited.status_code == 200, edited.text
    doc = (await http.get(order)).json()
    line = next(row for row in doc["lines"] if row["id"] == line_id)
    assert next(part for part in line["parts"] if part["part_id"] == lamp["shade"])["qty_per_unit"] == 3
    assert {part["name"]: part["per"] for part in line["purchased"]} == {"Ring": 2, "Switch": 5}
    assert len({part["part_id"] for part in line["purchased"]}) == 2
    assert {part["name"]: part["need"] for part in doc["procurement"]} == {
        "Ring": 2 * line["quantity"],
        "Switch": 5 * line["quantity"],
    }
    assert [(part["name"], part["qty"]) for part in line["configuration"]["changed_parts"] if part["part_id"] < 0] == [
        ("Ring", 2),
        ("Switch", 5),
    ]


async def test_variant_change_retains_archived_manual_part_without_overriding_new_catalog_part(
    committing_client, db_session, lamp
):
    http = committing_client
    order, url = await urls(db_session, lamp, "L_A")
    ring = await add_catalog(http, lamp, "Ring")
    await _add_saved_part(http, url, ring, 2, "ring")
    await _delete_catalog_part(http, lamp["product"], ring)
    switch = await add_catalog(http, lamp, "Switch")
    assert switch == ring

    body = {"choices": {str(lamp["group"]): lamp["options"]["B"]}, "expected_revision": 2}
    preview = await http.put(url + "/configuration", json={**body, "dry_run": True})
    assert preview.status_code == 200, preview.text
    assert {part["name"]: part["per_after"] for part in preview.json()["changes"]} == {"Switch": 1}
    done = await http.put(url + "/configuration", json={**body, "review_version": preview.json()["review_version"]})
    assert done.status_code == 200, done.text
    line = next(row for row in (await http.get(order)).json()["lines"] if row["id"] == lamp["lines"]["L_A"])
    assert {part["name"]: part["per"] for part in line["purchased"]} == {"Ring": 2, "Switch": 1}
    assert {part["name"]: part["qty"] for part in line["configuration"]["changed_parts"]} == {"Ring": 2}


async def test_count_edit_keeps_unchanged_saved_name_and_standard(committing_client, db_session, lamp):
    http = committing_client
    order, url = await urls(db_session, lamp, "L_O")
    part = await add_catalog(http, lamp, "Washer")
    await _add_saved_part(http, url, part, 4, "washer")
    shade = lamp["shade"]

    def captions(doc):
        line = next(row for row in doc["lines"] if row["id"] == lamp["lines"]["L_O"])
        return {row["part_id"]: row for row in line["configuration"]["changed_parts"]}

    before = captions((await http.get(order)).json())
    renamed = await http.patch(
        f"/api/v1/products/{lamp['product']}/parts/{shade}", json={"name": "Current shade", "qty_per_unit": 7}
    )
    assert renamed.status_code == 200, renamed.text
    response = await http.put(url + "/configuration", json={"part_edits": {str(part): 3}, "expected_revision": 2})
    assert response.status_code == 200, response.text
    after = captions((await http.get(order)).json())
    assert after[shade] == before[shade]
    assert after[shade]["standard_qty"] == 1
    assert after[part]["qty"] == 3


async def _saved_line(http, order, line_id):
    response = await http.get(order)
    assert response.status_code == 200, response.text
    return next(line for line in response.json()["lines"] if line["id"] == line_id)


@pytest.mark.parametrize("change", ["new_hold", "compatible_shelf_emptied"])
async def test_add_part_rechecks_reviewed_reservations(committing_client, db_session, lamp, change):
    from backend.app.models.finished_stock import StockItem, StockItemMovement

    http = committing_client
    order, url = await urls(db_session, lamp, "L_A")
    line_id = lamp["lines"]["L_A"]
    shelf_id = lamp["items"]["P_A"]
    shelf = await db_session.get(StockItem, shelf_id)
    await finished_stock.receive(db_session, shelf, 1)
    await db_session.commit()

    gasket = await add_catalog(http, lamp, "Gasket")
    if change == "compatible_shelf_emptied":
        held = await http.patch(url, json={"from_finished": 1})
        assert held.status_code == 200, held.text
        updated_shelf = await finished_stock.item_for(
            db_session, lamp["product"], {lamp["group"]: lamp["options"]["A"]}, create=True
        )
        assert updated_shelf.id != shelf_id
        await finished_stock.receive(db_session, updated_shelf, 1)
        new_shelf_id = updated_shelf.id
        await db_session.commit()

    meta = (await http.get(url + "/part-candidates")).json()
    identity = next(part["part_identity"] for part in meta["parts"] if part["id"] == gasket)
    body = {**await add_body(http, url, gasket), "part_identity": identity}
    counts = ("reserved_before", "reserved_after", "finished_before", "finished_after")

    async def preview():
        response = await http.put(
            url + "/configuration",
            json={
                "dry_run": True,
                "expected_revision": body["expected_revision"],
                "catalog_part_edits": {identity: 1},
            },
        )
        assert response.status_code == 200, response.text
        data = response.json()
        return {key: data[key] for key in counts}

    reviewed = await preview()
    if change == "new_hold":
        assert (reviewed["finished_before"], reviewed["finished_after"]) == (0, 0)
        held = await http.patch(url, json={"from_finished": 1})
        assert held.status_code == 200, held.text
    else:
        assert (reviewed["finished_before"], reviewed["finished_after"]) == (1, 1)
        emptied = await http.post(
            "/api/v1/stock/moves",
            json={"kind": "stocktake", "item_id": new_shelf_id, "counted": 0, "note": "Shelf emptied during review"},
        )
        assert emptied.status_code == 200, emptied.text

    event_ids = (await db_session.scalars(select(ProjectEvent.id))).all()
    movement_ids = (await db_session.scalars(select(StockItemMovement.id))).all()
    stale = await http.post(url + "/parts", json={**body, "reservation_review": reviewed})
    assert stale.status_code == 409, stale.text
    saved = await _saved_line(http, order, line_id)
    assert saved["configuration"]["revision"] == 1
    assert saved["purchased"] == []
    assert saved["from_finished"] == 1
    assert (await http.get(f"/api/v1/stock/items/{shelf_id}")).json()["reserved"] == 1
    assert (await db_session.scalars(select(ProjectEvent.id))).all() == event_ids
    assert (await db_session.scalars(select(StockItemMovement.id))).all() == movement_ids

    updated = await preview()
    assert (updated["finished_before"], updated["finished_after"]) == (1, 0)
    confirmed = {**body, "reservation_review": updated}
    applied = await http.post(url + "/parts", json=confirmed)
    assert applied.status_code == 200, applied.text
    replay = await http.post(url + "/parts", json=confirmed)
    assert replay.status_code == 200, replay.text
    saved = await _saved_line(http, order, line_id)
    assert saved["configuration"]["revision"] == 2
    assert saved["from_finished"] == 0
    assert (await http.get(f"/api/v1/stock/items/{shelf_id}")).json()["reserved"] == 0


@pytest.mark.parametrize(
    "held,late,kind",
    [(1, False, "purchased"), (3, False, "purchased"), (1, True, "purchased"), (3, False, "printed")],
    ids=["loose", "whole-and-loose", "late-loose", "printed-releases-only-whole-kit"],
)
async def test_add_part_keeps_loose_reservations(committing_client, db_session, lamp, held, late, kind):
    http = committing_client
    order, url = await urls(db_session, lamp, "L_O")
    line_id = lamp["lines"]["L_O"]
    ring_id = lamp["shade"]
    assert (await http.patch(url, json={"quantity": 3})).status_code == 200
    product = f"/api/v1/products/{lamp['product']}"
    new = await http.post(product + "/parts", json={"kind": kind, "name": "Gasket", "qty_per_unit": 1})
    assert new.status_code == 200, new.text
    new_part = new.json()
    identity = new_part["composition_identity"]
    body = {**await add_body(http, url, new_part["id"]), "part_identity": identity}

    async def take():
        assert (
            await http.post(
                product + "/stock/adjust",
                json={"part_id": ring_id, "delta": held, "note": "Shelf receipt"},
            )
        ).status_code == 200
        line = await db_session.get(ProjectLine, line_id, populate_existing=True)
        assert await part_stock.add_parts_for_line(db_session, line, {ring_id: held}, created_by=None) == {
            ring_id: held
        }
        await db_session.commit()

    async def preview():
        response = await http.put(
            url + "/configuration",
            json={"dry_run": True, "expected_revision": body["expected_revision"], "catalog_part_edits": {identity: 1}},
        )
        assert response.status_code == 200, response.text
        impact = response.json()
        assert impact["part_reservation_version"]
        return {
            "reservation_review": {
                key: impact[key]
                for key in (
                    "reserved_before",
                    "reserved_after",
                    "finished_before",
                    "finished_after",
                    "part_reservation_version",
                )
            },
            "released_parts": impact["released_parts"],
        }

    async def movements():
        return (
            await db_session.scalars(select(ProductPartStockMovement.id).order_by(ProductPartStockMovement.id))
        ).all()

    if not late:
        await take()
    reviewed = await preview()
    if late:
        await take()
        saved = await _saved_line(http, order, line_id)
        event_ids = (await db_session.scalars(select(ProjectEvent.id))).all()
        movement_ids = await movements()
        stale = await http.post(url + "/parts", json={**body, "reservation_review": reviewed["reservation_review"]})
        assert stale.status_code == 409, stale.text
        assert await _saved_line(http, order, line_id) == saved
        assert await movements() == movement_ids
        assert (await db_session.scalars(select(ProjectEvent.id))).all() == event_ids
        fresh = await preview()
        assert fresh["reservation_review"]["reserved_before"] == reviewed["reservation_review"]["reserved_before"] == 0
        assert (
            fresh["reservation_review"]["part_reservation_version"]
            != reviewed["reservation_review"]["part_reservation_version"]
        )
        reviewed = fresh

    released = 2 if kind == "printed" else 0
    assert reviewed["released_parts"] == (
        [{"part_id": ring_id, "name": "shade", "quantity": released}] if released else []
    )
    movement_ids = await movements()
    payload = {**body, "reservation_review": reviewed["reservation_review"]}
    result = await http.post(url + "/parts", json=payload)
    assert result.status_code == 200, result.text
    after = await _saved_line(http, order, line_id)
    assert after["configuration"]["revision"] == body["expected_revision"] + 1
    assert after["from_kit_units"] == (held - released) // 2
    assert next(row["allocated_qty"] for row in after["parts"] if row["part_id"] == ring_id) == held - released
    assert (await part_stock.balances(db_session, lamp["product"]))[ring_id] == released
    if released:
        [movement_id] = set(await movements()) - set(movement_ids)
        movement = await db_session.get(ProductPartStockMovement, movement_id)
        assert (movement.reason, movement.product_part_id, movement.delta) == (
            "reservation_released",
            ring_id,
            released,
        )
    else:
        assert await movements() == movement_ids

    applied = await movements()
    assert (await http.post(url + "/parts", json=payload)).status_code == 200
    assert await _saved_line(http, order, line_id) == after
    assert await movements() == applied


async def test_add_part_refuses_old_identity_even_with_current_catalog_version(committing_client, db_session, lamp):
    http = committing_client
    order, url = await urls(db_session, lamp, "L_A")
    ring = await add_catalog(http, lamp, "Ring")
    old = (await http.get(url + "/part-candidates")).json()
    [original] = [part for part in old["parts"] if part["id"] == ring]
    await _delete_catalog_part(http, lamp["product"], ring)
    replacement = await add_catalog(http, lamp, "Ring")
    assert replacement == ring
    fresh = (await http.get(url + "/part-candidates")).json()
    [candidate] = [part for part in fresh["parts"] if part["id"] == replacement]
    assert candidate["part_identity"] != original["part_identity"]
    cmd = {
        **await add_body(http, url, replacement),
        "part_identity": original["part_identity"],
        "quantity": 4,
    }
    refused = await http.post(url + "/parts", json=cmd)
    assert refused.status_code == 409
    line = await _saved_line(http, order, lamp["lines"]["L_A"])
    assert line["configuration"]["revision"] == 1
    assert line["purchased"] == []

    accepted = {**cmd, "part_identity": candidate["part_identity"], "quantity": 1}
    first = await http.post(url + "/parts", json=accepted)
    replay = await http.post(url + "/parts", json=accepted)
    assert (first.status_code, replay.status_code) == (200, 200)
    line = await _saved_line(http, order, lamp["lines"]["L_A"])
    assert line["configuration"]["revision"] == 2
    assert [
        (part["identity"], part["qty"]) for part in line["configuration"]["saved_parts"] if part["name"] == "Ring"
    ] == [(candidate["part_identity"], 1)]


async def test_catalog_draft_refuses_reused_identity_in_preview_and_write(committing_client, db_session, lamp):
    http = committing_client
    order, url = await urls(db_session, lamp, "L_A")
    catalog = f"/api/v1/products/{lamp['product']}/parts"
    attrs = {"kind": "purchased", "name": "Ring", "qty_per_unit": 1}
    created = await http.post(catalog, json=attrs)
    assert created.status_code == 200, created.text
    old = created.json()
    base = {"choices": {str(lamp["group"]): lamp["options"]["B"]}, "expected_revision": 1}
    draft = {**base, "catalog_part_edits": {old["composition_identity"]: 4}}
    review = await http.put(url + "/configuration", json={**draft, "dry_run": True})
    assert review.status_code == 200, review.text
    assert [(p["name"], p["per_after"]) for p in review.json()["changes"]] == [("Ring", 4)]

    await _delete_catalog_part(http, lamp["product"], old["id"])
    replacement = await http.post(catalog, json=attrs)
    assert replacement.status_code == 200, replacement.text
    assert replacement.json()["id"] == old["id"]
    assert replacement.json()["composition_identity"] != old["composition_identity"]

    for request in [{**draft, "dry_run": True}, {**draft, "review_version": review.json()["review_version"]}]:
        refused = await http.put(url + "/configuration", json=request)
        assert refused.status_code == 409
        assert refused.json()["detail"]["error"] == "line_configuration_stale"
    line_id = lamp["lines"]["L_A"]
    assert (await _saved_line(http, order, line_id))["configuration"]["revision"] == 1

    latest = await http.put(url + "/configuration", json={**base, "dry_run": True})
    assert latest.status_code == 200, latest.text
    assert [(p["name"], p["per_after"]) for p in latest.json()["changes"]] == [("Ring", 1)]
    accepted = await http.put(url + "/configuration", json={**base, "review_version": latest.json()["review_version"]})
    assert accepted.status_code == 200, accepted.text
    line = await _saved_line(http, order, line_id)
    assert [(p["name"], p["per"]) for p in line["purchased"]] == [("Ring", 1)]


async def test_part_counts_and_saved_part_edits_apply_together(committing_client, db_session, lamp):
    http = committing_client
    order, url = await urls(db_session, lamp, "L_P")
    line_id = lamp["lines"]["L_P"]
    ring = await add_catalog(http, lamp, "Ring")
    await _add_saved_part(http, url, ring, 2, "ring")
    await _delete_catalog_part(http, lamp["product"], ring)
    line = await _saved_line(http, order, line_id)
    [archived] = [part for part in line["configuration"]["saved_parts"] if not part["in_catalog"]]
    draft = {
        "expected_revision": 2,
        "part_counts": {str(lamp["shade"]): 3},
        "saved_part_edits": {archived["identity"]: 1},
    }
    review = await http.put(url + "/configuration", json={**draft, "dry_run": True})
    assert review.status_code == 200, review.text
    assert {part["part_id"]: part["per_after"] for part in review.json()["changes"]} == {
        lamp["shade"]: 3,
        archived["part_id"]: 1,
    }
    accepted = await http.put(url + "/configuration", json=draft)
    assert accepted.status_code == 200, accepted.text
    line = await _saved_line(http, order, line_id)
    assert {part["part_id"]: part["qty"] for part in line["configuration"]["saved_parts"]} == {
        lamp["shade"]: 3,
        archived["part_id"]: 1,
    }


async def test_parts_count_delta_preserves_archived_purchase_and_validates_the_result(
    committing_client, db_session, lamp
):
    http = committing_client
    line_id = lamp["lines"]["L_P"]
    order, url = await urls(db_session, lamp, "L_P")
    ring = await add_catalog(http, lamp, "Ring")
    await _add_saved_part(http, url, ring, 2, "ring")
    await _delete_catalog_part(http, lamp["product"], ring)

    edit = {"part_edits": {str(lamp["shade"]): 2}, "expected_revision": 2}
    preview = await http.put(url + "/configuration", json={**edit, "dry_run": True})
    assert preview.status_code == 200, preview.text
    assert [(row["part_id"], row["per_after"]) for row in preview.json()["changes"]] == [(lamp["shade"], 2)]
    response = await http.put(url + "/configuration", json=edit)
    assert response.status_code == 200, response.text

    response = await http.put(
        url + "/configuration", json={"part_edits": {str(lamp["shade"]): 0}, "expected_revision": 3}
    )
    assert response.status_code == 200, response.text
    line = await _saved_line(http, order, line_id)
    assert [(row["name"], row["per"]) for row in line["purchased"]] == [("Ring", 2)]
    assert not any(row["qty_per_unit"] > 0 for row in line["parts"])
    [saved_ring] = line["configuration"]["saved_parts"]
    empty = {"expected_revision": 4, "saved_part_edits": {saved_ring["identity"]: 0}}
    for dry_run in [True, False]:
        refused = await http.put(url + "/configuration", json={**empty, "dry_run": dry_run})
        assert refused.status_code == 422
        assert refused.json()["detail"] == "A parts line needs at least one part"
    assert await _saved_line(http, order, line_id) == line


@pytest.mark.parametrize("quantity", [0, 1])
async def test_variant_edit_applies_explicit_manual_count_at_catalog_standard(
    committing_client, db_session, lamp, quantity
):
    http = committing_client
    line_id = lamp["lines"]["L_A"]
    order, url = await urls(db_session, lamp, "L_A")
    catalog = f"/api/v1/products/{lamp['product']}/parts"
    attrs = {"kind": "purchased", "name": "Seal", "qty_per_unit": 1}
    if quantity == 0:
        attrs["variant_option_ids"] = [lamp["options"]["A"]]
    created = await http.post(catalog, json=attrs)
    assert created.status_code == 200, created.text
    part = created.json()["id"]
    await _add_saved_part(http, url, part, 2, "seal")
    body = {
        "choices": {str(lamp["group"]): lamp["options"]["B"]},
        "saved_part_edits": {created.json()["composition_identity"]: quantity},
        "expected_revision": 2,
    }
    preview = await http.put(url + "/configuration", json={**body, "dry_run": True})
    assert preview.status_code == 200, preview.text
    [change] = preview.json()["changes"]
    assert (change["per_before"], change["per_after"]) == (2, quantity)
    response = await http.put(url + "/configuration", json={**body, "review_version": preview.json()["review_version"]})
    assert response.status_code == 200, response.text
    after = await _saved_line(http, order, line_id)
    assert [(row["name"], row["per"], row["need"]) for row in after["purchased"]] == (
        [("Seal", 1, after["quantity"])] if quantity else []
    )


async def test_excluded_count_does_not_reference_reused_catalog_id(committing_client, db_session, lamp):
    http = committing_client
    line_id = lamp["lines"]["L_A"]
    order, url = await urls(db_session, lamp, "L_A")
    ring = await add_catalog(http, lamp, "Ring")
    await _add_saved_part(http, url, ring, 2, "ring")
    response = await http.put(url + "/configuration", json={"part_edits": {str(ring): 0}, "expected_revision": 2})
    assert response.status_code == 200, response.text
    await _delete_catalog_part(http, lamp["product"], ring)
    switch = await add_catalog(http, lamp, "Switch")
    assert switch == ring
    line = await _saved_line(http, order, line_id)
    [excluded] = line["configuration"]["changed_parts"]
    assert (excluded["name"], excluded["qty"]) == ("Ring", 0)
    assert excluded["part_id"] < 0
    assert str(switch) not in line["configuration"]["count_identities"]


async def test_legacy_zero_override_stays_archived_after_unrelated_edit(committing_client, db_session, lamp):
    http = committing_client
    line_id = lamp["lines"]["L_A"]
    order, url = await urls(db_session, lamp, "L_A")
    old = await add_catalog(http, lamp, "Seal")
    await _add_saved_part(http, url, old, 2, "seal")
    response = await http.put(url + "/configuration", json={"part_edits": {str(old): 0}, "expected_revision": 2})
    assert response.status_code == 200, response.text
    stored = await db_session.get(ProjectLine, line_id, populate_existing=True)
    history = stored.composition_revisions
    [saved_override] = history[-1]["configuration"]["changed_parts"]
    saved_override.pop("identity")
    await db_session.execute(update(ProjectLine).where(ProjectLine.id == line_id).values(composition_revisions=history))
    await db_session.commit()
    await _delete_catalog_part(http, lamp["product"], old)
    new = await add_catalog(http, lamp, "Seal")
    assert new == old
    response = await http.put(
        url + "/configuration", json={"part_edits": {str(lamp["shade"]): 2}, "expected_revision": 3}
    )
    assert response.status_code == 200, response.text
    line = await _saved_line(http, order, line_id)
    excluded = next(row for row in line["configuration"]["changed_parts"] if row["name"] == "Seal")
    assert excluded["part_id"] < 0
    assert str(new) not in line["configuration"]["count_identities"]


async def test_archived_count_can_change_and_removal_requires_review(committing_client, db_session, lamp):
    http = committing_client
    line_id = lamp["lines"]["L_A"]
    order, url = await urls(db_session, lamp, "L_A")
    ring = await add_catalog(http, lamp, "Ring")
    await _add_saved_part(http, url, ring, 2, "ring")
    await _delete_catalog_part(http, lamp["product"], ring)
    line = await _saved_line(http, order, line_id)
    [archived] = [row for row in line["configuration"]["saved_parts"] if not row["in_catalog"]]
    identity = archived["identity"]
    adjusted = await http.put(url + "/configuration", json={"saved_part_edits": {identity: 1}, "expected_revision": 2})
    assert adjusted.status_code == 200, adjusted.text
    line = await _saved_line(http, order, line_id)
    assert [(row["name"], row["per"]) for row in line["purchased"]] == [("Ring", 1)]
    assert line["configuration"]["changed_parts"][0]["qty"] == 1

    body = {"part_edits": {str(archived["part_id"]): 0}, "expected_revision": 3}
    preview = await http.put(url + "/configuration", json={**body, "dry_run": True})
    assert preview.status_code == 200, preview.text
    [change] = preview.json()["changes"]
    assert (change["name"], change["per_before"], change["per_after"]) == ("Ring", 1, 0)
    unconfirmed = await http.put(url + "/configuration", json=body)
    assert unconfirmed.status_code == 409
    confirmed = await http.put(
        url + "/configuration", json={**body, "review_version": preview.json()["review_version"]}
    )
    assert confirmed.status_code == 200, confirmed.text
    after = await _saved_line(http, order, line_id)
    assert after["purchased"] == []
    history = (await http.get(url + "/composition-revisions")).json()
    assert [(row["name"], row["qty"]) for row in history[1]["parts"] if row["kind"] == "purchased"] == [("Ring", 2)]


async def test_equivalent_legacy_shelves_keep_reservation(committing_client, db_session, lamp):
    from sqlalchemy import update

    from backend.app.migrations import m258_order_line_composition as migration
    from backend.app.models.finished_stock import StockItem, StockItemMovement

    opts = {lamp["group"]: lamp["options"]["A"]}
    line = await db_session.get(ProjectLine, lamp["lines"]["L_O"])
    second = await finished_stock.item_for(db_session, lamp["product"], opts, {lamp["shade"]: 2}, create=True)
    await finished_stock.receive(db_session, second, 1)
    await db_session.commit()
    assert await finished_stock.reserve_for_line(db_session, line, 1) == 1
    await db_session.commit()
    # Represent the legacy catalog after the standard caught up with an explicit count.
    # Both historical config keys and the original, already-posted movement rows are retained.
    await db_session.execute(update(ProjectLine).values(composition_revisions=None))
    await db_session.execute(update(StockItem).values(composition_parts=None, variant_key=None))
    await db_session.execute(update(ProductPart).where(ProductPart.id == lamp["shade"]).values(qty_per_unit=2))
    await db_session.flush()
    conn = await db_session.connection()
    await migration.upgrade(conn)
    await migration.upgrade(conn)
    await db_session.commit()
    await db_session.refresh(line)
    held_before = await finished_stock.held_by_item(db_session, line.id)
    assert held_before == {second.id: 1}
    assert (await finished_stock.position_for_line(db_session, line, create=False)).id == second.id
    before = (await db_session.scalars(select(StockItemMovement.id).order_by(StockItemMovement.id))).all()
    r = await committing_client.patch(f"/api/v1/projects/{line.project_id}/lines/{line.id}", json={"from_finished": 1})
    assert r.status_code == 200, r.text
    after = next(row for row in r.json()["lines"] if row["id"] == line.id)["from_finished"]
    assert after == 1
    assert await finished_stock.held_by_item(db_session, line.id) == held_before
    assert (await db_session.scalars(select(StockItemMovement.id).order_by(StockItemMovement.id))).all() == before

    first = await db_session.get(StockItem, lamp["items"]["P_A"])
    await finished_stock.receive(db_session, first, 1)
    await finished_stock.receive(db_session, second, 1)
    await db_session.commit()
    offer = await committing_client.post(
        "/api/v1/stock/suggest",
        json={
            "items": [
                {"product_id": lamp["product"], "quantity": 3, "line_id": line.id},
            ]
        },
    )
    assert offer.status_code == 200, offer.text
    assert offer.json()["items"][0]["from_finished"] == 3
    assert offer.json()["items"][0]["position_id"] == second.id
    grew = await committing_client.patch(
        f"/api/v1/projects/{line.project_id}/lines/{line.id}", json={"quantity": 3, "from_finished": 3}
    )
    assert grew.status_code == 200, grew.text
    assert await finished_stock.held_by_item(db_session, line.id) == {first.id: 1, second.id: 2}
    issue = StockIssue(project_id=line.project_id)
    db_session.add(issue)
    await db_session.flush()
    await finished_stock.issue_from_line(db_session, line, 3, stock_issue=issue)
    issued = (
        await db_session.execute(
            select(StockItemMovement.item_id, StockItemMovement.delta_on_hand).where(
                StockItemMovement.stock_issue_id == issue.id
            )
        )
    ).all()
    assert dict(issued) == {first.id: -1, second.id: -2}
