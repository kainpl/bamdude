"""Variant groups and options of a product over the API (spec workshop-product-variants, rules 18–21),
and a part bound across several groups (spec product-part-variant-combinations, rules 3, 4, 16, 19)."""

import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.app.models.line_config import ProjectLineChoice
from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductPartOption, ProductVariantGroup, ProductVariantOption
from backend.tests.fixtures.sliced_sources import slice_parts

pytestmark = pytest.mark.integration


@pytest.fixture
async def pipe(db_session):
    product = Product(name="Pipe")
    db_session.add(product)
    await db_session.flush()
    parts = [
        ProductPart(product_id=product.id, kind="printed", name=n, name_key=n, qty_per_unit=1, sort_order=i)
        for i, n in enumerate(("flask", "straight", "angled"))
    ]
    db_session.add_all(parts)
    await db_session.commit()
    return {"product": product, "parts": {p.name: p for p in parts}}


def _base(pipe):
    return f"/api/v1/products/{pipe['product'].id}"


async def _tail(client, pipe):
    r = await client.post(f"{_base(pipe)}/variant-groups", json={"name": "Хвіст", "options": ["прямий", "кутовий"]})
    assert r.status_code == 200, r.text
    group = r.json()["variant_groups"][0]
    return group, {o["name"]: o for o in group["options"]}


@pytest.mark.asyncio
async def test_a_group_with_two_options_the_first_standard(committing_client, pipe):
    group, options = await _tail(committing_client, pipe)
    assert group["name"] == "Хвіст" and group["default_option_id"] == options["прямий"]["id"]
    assert [o["name"] for o in group["options"]] == ["прямий", "кутовий"]
    assert options["прямий"]["lines_count"] == 0 and options["прямий"]["parts_count"] == 0


@pytest.mark.asyncio
async def test_names_are_unique_without_case(committing_client, pipe):
    group, _ = await _tail(committing_client, pipe)
    dup = await committing_client.post(f"{_base(pipe)}/variant-groups", json={"name": " хвіст ", "options": ["x"]})
    assert dup.status_code == 409
    empty = await committing_client.post(f"{_base(pipe)}/variant-groups", json={"name": "Колір", "options": []})
    assert empty.status_code == 422
    opt = await committing_client.post(f"{_base(pipe)}/variant-groups/{group['id']}/options", json={"name": "ПРЯМИЙ"})
    assert opt.status_code == 409
    added = await committing_client.post(f"{_base(pipe)}/variant-groups/{group['id']}/options", json={"name": "Т"})
    assert [o["name"] for o in added.json()["variant_groups"][0]["options"]] == ["прямий", "кутовий", "Т"]


@pytest.mark.asyncio
async def test_rename_and_change_the_standard(committing_client, pipe):
    group, options = await _tail(committing_client, pipe)
    r = await committing_client.patch(
        f"{_base(pipe)}/variant-groups/{group['id']}",
        json={"name": "Tail", "default_option_id": options["кутовий"]["id"]},
    )
    assert r.status_code == 200, r.text
    g = r.json()["variant_groups"][0]
    assert g["name"] == "Tail" and g["default_option_id"] == options["кутовий"]["id"]
    foreign = await committing_client.patch(
        f"{_base(pipe)}/variant-groups/{group['id']}", json={"default_option_id": 999999}
    )
    assert foreign.status_code == 422
    renamed = await committing_client.patch(
        f"{_base(pipe)}/variant-groups/{group['id']}/options/{options['прямий']['id']}", json={"name": "straight"}
    )
    assert renamed.json()["variant_groups"][0]["options"][0]["name"] == "straight"


@pytest.mark.asyncio
async def test_a_part_binds_to_an_option_of_its_own_product_only(committing_client, db_session, pipe):
    _group, options = await _tail(committing_client, pipe)
    straight = pipe["parts"]["straight"]
    r = await committing_client.patch(
        f"{_base(pipe)}/parts/{straight.id}", json={"variant_option_ids": [options["прямий"]["id"]]}
    )
    assert r.status_code == 200 and r.json()["variant_option_ids"] == [options["прямий"]["id"]]
    other = Product(name="Other")
    db_session.add(other)
    await db_session.commit()
    og = await committing_client.post(
        f"/api/v1/products/{other.id}/variant-groups", json={"name": "X", "options": ["x"]}
    )
    foreign_option = og.json()["variant_groups"][0]["options"][0]["id"]
    bad = await committing_client.patch(
        f"{_base(pipe)}/parts/{straight.id}", json={"variant_option_ids": [foreign_option]}
    )
    assert bad.status_code == 422
    unbound = await committing_client.patch(f"{_base(pipe)}/parts/{straight.id}", json={"variant_option_ids": []})
    assert unbound.json()["variant_option_ids"] == []


@pytest.mark.asyncio
async def test_deletes_refused_while_lines_or_parts_use_them(committing_client, db_session, pipe):
    # The order below needs its kit sliced (spec product-part-variant-combinations, п. 10).
    await slice_parts(db_session, pipe["product"].id, pipe["parts"].values())
    await db_session.commit()
    group, options = await _tail(committing_client, pipe)
    angled = options["кутовий"]["id"]
    await committing_client.patch(
        f"{_base(pipe)}/parts/{pipe['parts']['angled'].id}", json={"variant_option_ids": [angled]}
    )
    bound = await committing_client.delete(f"{_base(pipe)}/variant-groups/{group['id']}/options/{angled}")
    assert bound.status_code == 409
    standard = await committing_client.delete(
        f"{_base(pipe)}/variant-groups/{group['id']}/options/{options['прямий']['id']}"
    )
    assert standard.status_code == 409
    await committing_client.patch(f"{_base(pipe)}/parts/{pipe['parts']['angled'].id}", json={"variant_option_ids": []})
    order = (
        await committing_client.post(
            "/api/v1/projects/",
            json={
                "name": "O",
                "lines": [{"product_id": pipe["product"].id, "quantity": 1, "choices": {str(group["id"]): angled}}],
            },
        )
    ).json()
    chosen = await committing_client.delete(f"{_base(pipe)}/variant-groups/{group['id']}/options/{angled}")
    assert chosen.status_code == 409 and "1" in chosen.json()["detail"]
    whole = await committing_client.delete(f"{_base(pipe)}/variant-groups/{group['id']}")
    assert whole.status_code == 409
    await committing_client.delete(f"/api/v1/projects/{order['id']}")
    assert (await committing_client.delete(f"{_base(pipe)}/variant-groups/{group['id']}")).status_code == 200


@pytest.mark.asyncio
async def test_a_new_group_on_a_product_on_orders_reaches_its_lines(committing_client, db_session, pipe):
    order = (
        await committing_client.post(
            "/api/v1/projects/", json={"name": "O", "lines": [{"product_id": pipe["product"].id, "quantity": 1}]}
        )
    ).json()
    group, options = await _tail(committing_client, pipe)
    rows = (await db_session.execute(select(ProjectLineChoice.group_id, ProjectLineChoice.option_id))).all()
    assert rows == [(group["id"], options["прямий"]["id"])]
    assert order["lines"][0]["id"]
    # A later change of the standard leaves the saved choice alone.
    await committing_client.patch(
        f"{_base(pipe)}/variant-groups/{group['id']}", json={"default_option_id": options["кутовий"]["id"]}
    )
    rows = (await db_session.execute(select(ProjectLineChoice.option_id))).scalars().all()
    assert rows == [options["прямий"]["id"]]


@pytest.mark.asyncio
async def test_a_copy_of_the_product_carries_its_variants(committing_client, pipe):
    group, options = await _tail(committing_client, pipe)
    await committing_client.patch(
        f"{_base(pipe)}/parts/{pipe['parts']['angled'].id}", json={"variant_option_ids": [options["кутовий"]["id"]]}
    )
    copy = (await committing_client.post(f"{_base(pipe)}/duplicate", json={})).json()
    g = copy["variant_groups"][0]
    assert g["name"] == "Хвіст" and [o["name"] for o in g["options"]] == ["прямий", "кутовий"]
    assert g["id"] != group["id"]
    standard = next(o for o in g["options"] if o["id"] == g["default_option_id"])
    assert standard["name"] == "прямий"
    angled_copy = next(p for p in copy["parts"] if p["name"] == "angled")
    assert angled_copy["variant_option_ids"] == [next(o["id"] for o in g["options"] if o["name"] == "кутовий")]


@pytest.mark.asyncio
async def test_export_and_import_keep_the_variants(committing_client, pipe):
    _group, options = await _tail(committing_client, pipe)
    await committing_client.patch(
        f"{_base(pipe)}/parts/{pipe['parts']['angled'].id}", json={"variant_option_ids": [options["кутовий"]["id"]]}
    )
    exported = await committing_client.get(f"{_base(pipe)}/export")
    assert exported.status_code == 200
    imported = await committing_client.post(
        "/api/v1/products/import", files={"file": ("pipe.zip", exported.content, "application/zip")}
    )
    assert imported.status_code == 200, imported.text
    product = imported.json()["product"]
    g = product["variant_groups"][0]
    assert [o["name"] for o in g["options"]] == ["прямий", "кутовий"]
    angled = next(p for p in product["parts"] if p["name"] == "angled")
    assert angled["variant_option_ids"] == [next(o["id"] for o in g["options"] if o["name"] == "кутовий")]


@pytest.mark.asyncio
async def test_a_deleted_product_takes_its_variants(committing_client, db_session, pipe):
    await _tail(committing_client, pipe)
    r = await committing_client.delete(_base(pipe))
    assert r.status_code == 200, r.text
    assert (await db_session.execute(select(ProductVariantGroup.id))).scalars().all() == []
    assert (await db_session.execute(select(ProductVariantOption.id))).scalars().all() == []


# ---------- a part bound across several groups (spec product-part-variant-combinations) ----------


@pytest.fixture
async def jar(db_session):
    """Jar: a weight (400 standard, 600, 1200), a view (solid standard, mesh) and a cap
    type (screw standard, snap); two bodies and a cap, none of them bound yet."""
    product = Product(name="Jar")
    db_session.add(product)
    await db_session.flush()
    ids: dict[str, int] = {"id": product.id}
    for position, (key, name, values) in enumerate(
        (
            ("weight", "Weight", (("w400", "400"), ("w600", "600"), ("w1200", "1200"))),
            ("view", "View", (("solid", "solid"), ("mesh", "mesh"))),
            ("cap_type", "Cap", (("screw", "screw"), ("snap", "snap"))),
        )
    ):
        group = ProductVariantGroup(product_id=product.id, name=name, position=position)
        db_session.add(group)
        await db_session.flush()
        options = [ProductVariantOption(group_id=group.id, name=n, position=i) for i, (_k, n) in enumerate(values)]
        db_session.add_all(options)
        await db_session.flush()
        group.default_option_id = options[0].id
        ids[key] = group.id
        ids.update({k: option.id for (k, _n), option in zip(values, options, strict=True)})
    parts = {
        key: ProductPart(product_id=product.id, kind="printed", name=name, name_key=name, qty_per_unit=1, sort_order=i)
        for i, (key, name) in enumerate(
            (("body_400_solid", "body 400 solid"), ("body_600_solid", "body 600 solid"), ("cap", "screw cap"))
        )
    }
    db_session.add_all(parts.values())
    await db_session.commit()
    ids.update({key: part.id for key, part in parts.items()})
    return SimpleNamespace(**ids)


@pytest.fixture
async def other_product_option(committing_client, db_session):
    other = Product(name="Other")
    db_session.add(other)
    await db_session.commit()
    r = await committing_client.post(
        f"/api/v1/products/{other.id}/variant-groups", json={"name": "Weight", "options": ["400"]}
    )
    assert r.status_code == 200, r.text
    return r.json()["variant_groups"][0]["options"][0]["id"]


def _jar_part(jar, part_id: int) -> str:
    return f"/api/v1/products/{jar.id}/parts/{part_id}"


async def _bind(client, jar, part_id: int, *options: int) -> dict:
    r = await client.patch(_jar_part(jar, part_id), json={"variant_option_ids": list(options)})
    assert r.status_code == 200, r.text
    return r.json()


async def _product(client, product_id: int) -> dict:
    r = await client.get(f"/api/v1/products/{product_id}")
    assert r.status_code == 200, r.text
    return r.json()


def part_of(product: dict, part_id: int) -> dict:
    return next(p for p in product["parts"] if p["id"] == part_id)


def _named(product: dict, name: str) -> dict:
    return next(p for p in product["parts"] if p["name"] == name)


def _options_by_name(product: dict) -> dict[tuple[str, str], int]:
    return {(g["name"], o["name"]): o["id"] for g in product["variant_groups"] for o in g["options"]}


async def _links(db_session, part_id: int) -> list[tuple[int, int]]:
    rows = await db_session.execute(
        select(ProductPartOption.option_id, ProductPartOption.group_id).where(ProductPartOption.part_id == part_id)
    )
    return sorted(rows.all())


@pytest.mark.asyncio
async def test_part_takes_values_in_several_groups(committing_client, db_session, jar):
    r = await committing_client.patch(
        _jar_part(jar, jar.body_400_solid), json={"variant_option_ids": [jar.w400, jar.solid]}
    )
    assert r.status_code == 200, r.text
    assert sorted(r.json()["variant_option_ids"]) == sorted([jar.w400, jar.solid])
    assert "variant_option_id" not in r.json()
    assert await _links(db_session, jar.body_400_solid) == sorted([(jar.w400, jar.weight), (jar.solid, jar.view)])
    # The product's own read says the same.
    product = await _product(committing_client, jar.id)
    assert sorted(part_of(product, jar.body_400_solid)["variant_option_ids"]) == sorted([jar.w400, jar.solid])
    assert part_of(product, jar.cap)["variant_option_ids"] == []


@pytest.mark.asyncio
async def test_a_new_part_is_born_bound_in_several_groups(committing_client, db_session, jar):
    r = await committing_client.post(
        f"/api/v1/products/{jar.id}/parts",
        json={"kind": "printed", "name": "body 600 mesh", "variant_option_ids": [jar.w600, jar.mesh]},
    )
    assert r.status_code == 200, r.text
    assert sorted(r.json()["variant_option_ids"]) == sorted([jar.w600, jar.mesh])
    legacy = await committing_client.post(
        f"/api/v1/products/{jar.id}/parts",
        json={"kind": "purchased", "name": "lid seal", "variant_option_id": jar.snap},
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["variant_option_ids"] == [jar.snap]
    plain = await committing_client.post(f"/api/v1/products/{jar.id}/parts", json={"kind": "printed", "name": "label"})
    assert plain.json()["variant_option_ids"] == []


@pytest.mark.asyncio
async def test_legacy_single_field_still_binds_and_null_clears(committing_client, jar):
    url = _jar_part(jar, jar.cap)
    r = await committing_client.patch(url, json={"variant_option_id": jar.screw})
    assert r.status_code == 200, r.text
    assert r.json()["variant_option_ids"] == [jar.screw]
    r = await committing_client.patch(url, json={"variant_option_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["variant_option_ids"] == []
    both = await committing_client.patch(url, json={"variant_option_id": jar.screw, "variant_option_ids": [jar.snap]})
    assert both.status_code == 422
    null_list = await committing_client.patch(url, json={"variant_option_ids": None})
    assert null_list.status_code == 422
    assert part_of(await _product(committing_client, jar.id), jar.cap)["variant_option_ids"] == []


@pytest.mark.asyncio
async def test_foreign_option_is_422(committing_client, db_session, jar, other_product_option):
    await _bind(committing_client, jar, jar.body_400_solid, jar.w400)
    r = await committing_client.patch(
        _jar_part(jar, jar.body_400_solid), json={"variant_option_ids": [jar.solid, other_product_option]}
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "That option does not belong to this product"
    missing = await committing_client.patch(_jar_part(jar, jar.body_400_solid), json={"variant_option_ids": [999999]})
    assert missing.status_code == 422
    # Nothing was written: the part keeps the binding it had.
    assert await _links(db_session, jar.body_400_solid) == [(jar.w400, jar.weight)]


@pytest.mark.asyncio
async def test_option_with_bound_part_cannot_be_deleted(committing_client, jar):
    # The cap fits 400 and 600 jars: two options of one group, still ONE part bound to it.
    await _bind(committing_client, jar, jar.cap, jar.screw, jar.w400, jar.w600)
    base = f"/api/v1/products/{jar.id}/variant-groups"
    option = await committing_client.delete(f"{base}/{jar.weight}/options/{jar.w600}")
    assert option.status_code == 409
    assert option.json()["detail"] == "1 parts are bound to this option"
    group = await committing_client.delete(f"{base}/{jar.weight}")
    assert group.status_code == 409
    assert group.json()["detail"] == "1 parts are bound to this group's options"
    weight = next(g for g in (await _product(committing_client, jar.id))["variant_groups"] if g["id"] == jar.weight)
    assert weight["parts_count"] == 1
    assert {o["name"]: o["parts_count"] for o in weight["options"]} == {"400": 1, "600": 1, "1200": 0}
    # An option nobody names goes.
    assert (await committing_client.delete(f"{base}/{jar.weight}/options/{jar.w1200}")).status_code == 200


@pytest.mark.asyncio
async def test_duplicate_product_copies_multi_bindings(committing_client, db_session, jar):
    await _bind(committing_client, jar, jar.body_400_solid, jar.w400, jar.solid)
    await _bind(committing_client, jar, jar.cap, jar.screw, jar.w400, jar.w600)
    r = await committing_client.post(f"/api/v1/products/{jar.id}/duplicate", json={})
    assert r.status_code == 200, r.text
    copy = r.json()
    names = _options_by_name(copy)
    assert sorted(_named(copy, "body 400 solid")["variant_option_ids"]) == sorted(
        [names[("Weight", "400")], names[("View", "solid")]]
    )
    assert sorted(_named(copy, "screw cap")["variant_option_ids"]) == sorted(
        [names[("Cap", "screw")], names[("Weight", "400")], names[("Weight", "600")]]
    )
    assert _named(copy, "body 600 solid")["variant_option_ids"] == []
    # The copy names its own options; the source keeps its bindings.
    assert not set(names.values()) & {jar.w400, jar.w600, jar.solid, jar.screw}
    assert await _links(db_session, jar.cap) == sorted(
        [(jar.w400, jar.weight), (jar.w600, jar.weight), (jar.screw, jar.cap_type)]
    )


def _rewrite_manifest(content: bytes, change) -> bytes:
    """The exported archive with its ``product.json`` passed through ``change``."""
    source = zipfile.ZipFile(io.BytesIO(content))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename == "product.json":
                manifest = json.loads(data.decode("utf-8"))
                change(manifest)
                data = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            target.writestr(info.filename, data)
    return out.getvalue()


@pytest.fixture
async def legacy_zip(committing_client, jar):
    """An archive as BamDude wrote it before parts took several values: a part's
    ``"variant"`` is one ``[group, option]`` pair (or null) and there is no ``"variants"``."""
    exported = await committing_client.get(f"/api/v1/products/{jar.id}/export")
    assert exported.status_code == 200, exported.text

    def to_old_shape(manifest: dict) -> None:
        for part in manifest["parts"]:
            part.pop("variants", None)
            part["variant"] = ["Weight", "600"] if part["name"] == "body 600 solid" else None

    return _rewrite_manifest(exported.content, to_old_shape)


async def _import(client, content: bytes) -> dict:
    r = await client.post("/api/v1/products/import", files={"file": ("jar.zip", content, "application/zip")})
    assert r.status_code == 200, r.text
    return r.json()["product"]


@pytest.mark.asyncio
async def test_zip_roundtrip_keeps_multi_bindings_and_old_archive_imports(committing_client, jar, legacy_zip):
    await _bind(committing_client, jar, jar.body_400_solid, jar.w400, jar.solid)
    await _bind(committing_client, jar, jar.cap, jar.screw, jar.w400, jar.w600)
    exported = await committing_client.get(f"/api/v1/products/{jar.id}/export")
    assert exported.status_code == 200, exported.text
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(exported.content)).read("product.json").decode("utf-8"))
    by_name = {p["name"]: p for p in manifest["parts"]}
    # By name, in the groups' order and each group's options' order.
    assert by_name["screw cap"]["variants"] == [["Weight", "400"], ["Weight", "600"], ["Cap", "screw"]]
    assert by_name["body 400 solid"]["variants"] == [["Weight", "400"], ["View", "solid"]]
    assert by_name["body 600 solid"]["variants"] == []

    product = await _import(committing_client, exported.content)
    names = _options_by_name(product)
    assert sorted(_named(product, "screw cap")["variant_option_ids"]) == sorted(
        [names[("Weight", "400")], names[("Weight", "600")], names[("Cap", "screw")]]
    )
    assert sorted(_named(product, "body 400 solid")["variant_option_ids"]) == sorted(
        [names[("Weight", "400")], names[("View", "solid")]]
    )

    old = await _import(committing_client, legacy_zip)
    old_names = _options_by_name(old)
    assert _named(old, "body 600 solid")["variant_option_ids"] == [old_names[("Weight", "600")]]
    assert _named(old, "screw cap")["variant_option_ids"] == []


@pytest.mark.asyncio
async def test_deleting_a_part_removes_its_binding_rows(committing_client, db_session, jar):
    await _bind(committing_client, jar, jar.body_400_solid, jar.w400, jar.solid)
    await _bind(committing_client, jar, jar.cap, jar.screw)
    r = await committing_client.delete(_jar_part(jar, jar.body_400_solid))
    assert r.status_code == 200, r.text
    assert await _links(db_session, jar.body_400_solid) == []
    assert await _links(db_session, jar.cap) == [(jar.screw, jar.cap_type)]
    # A deleted product takes the rest.
    assert (await committing_client.delete(f"/api/v1/products/{jar.id}")).status_code == 200
    assert (await db_session.execute(select(ProductPartOption.part_id))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_merged_part_keeps_the_targets_binding(committing_client, db_session, jar):
    """Spec rule 19: the merge says the source IS the target — the target keeps its own
    binding, the source's rows go with it."""
    await _bind(committing_client, jar, jar.body_400_solid, jar.w400, jar.solid)
    await _bind(committing_client, jar, jar.body_600_solid, jar.w600)
    r = await committing_client.post(
        _jar_part(jar, jar.body_400_solid) + "/merge", json={"source_part_id": jar.body_600_solid}
    )
    assert r.status_code == 200, r.text
    assert sorted(r.json()["variant_option_ids"]) == sorted([jar.w400, jar.solid])
    assert await _links(db_session, jar.body_600_solid) == []
    assert await _links(db_session, jar.body_400_solid) == sorted([(jar.w400, jar.weight), (jar.solid, jar.view)])
