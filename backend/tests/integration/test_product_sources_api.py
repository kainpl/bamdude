"""Where each part of a product can be printed from, and the product's files (WS-13 E1 PS, K3, K5, K7, LV4).

A part's sources are the plates that make it: sliced ones in the plan's own order
(``plan_engine.rank_key``) with the first recommended, unsliced ones after. A file
the caller may not see in the library keeps its numbers — model, plate, yield, time,
grams (Z7) — and loses its name and folder (``hidden``).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from backend.app.core.auth import create_access_token
from backend.app.core.permissions import Permission
from backend.app.models.group import Group
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.product import Product, ProductPart
from backend.app.models.user import User
from backend.app.services.product_sync import sync_product_for_file

pytestmark = pytest.mark.integration

_READ = Permission.PRODUCTS_READ.value


async def _user(db, username: str, permissions: list[str]) -> User:
    group = Group(name=f"grp-{username}", description="test", permissions=permissions)
    db.add(group)
    await db.flush()
    user = User(username=username, email=f"{username}@example.com", password_hash="x", role="user", is_active=True)
    user.groups.append(group)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _sliced(model: str, objects: dict[str, str], seconds: int, grams: float) -> dict:
    return {
        "sliced_for_model": model,
        "plates": [
            {
                "index": 1,
                "printable_objects": objects,
                "print_time_seconds": seconds,
                "filament_used_grams": grams,
                "filaments": [{"type": "PETG", "color": "#000000"}],
            }
        ],
    }


@pytest.fixture
async def hanger(db_session):
    """«Hanger»: parts hook and bar.
    - p1s — sliced for P1S, 2 hooks + 1 bar per hour, in folder «Shelf A», somebody else's;
    - x1c — sliced for X1C, 4 hooks per hour, the reader-own user's;
    - raw — an unsliced 3MF project with one hook, ownerless;
    - stl — a bare STL, the reader-own user's, no plates at all;
    - gone — sliced, trashed: never listed."""
    reader_all = await _user(db_session, "ps_all", [_READ, Permission.LIBRARY_READ_ALL.value])
    reader_own = await _user(db_session, "ps_own", [_READ, Permission.LIBRARY_READ_OWN.value])
    await _user(db_session, "ps_none", [_READ])
    folder = LibraryFolder(name="Shelf A")
    product = Product(name="Hanger")
    db_session.add_all([folder, product])
    await db_session.flush()
    specs = {
        "p1s": (
            "hanger-p1s.gcode.3mf",
            "gcode",
            _sliced("P1S", {"1": "hook.stl", "2": "hook.stl_2", "3": "bar.stl"}, 3600, 20.0),
            reader_all.id,
            folder.id,
        ),
        "x1c": (
            "hanger-x1c.gcode.3mf",
            "gcode",
            _sliced("X1C", {str(i): "hook.stl" if i == 1 else f"hook.stl_{i}" for i in range(1, 5)}, 3600, 30.0),
            reader_own.id,
            None,
        ),
        "raw": (
            "hanger-raw.3mf",
            "3mf",
            {"has_sliced_gcode": False, "plates": [{"index": 1, "printable_objects": {"1": "hook.stl"}}]},
            None,
            None,
        ),
        "stl": ("hanger.stl", "stl", {}, reader_own.id, None),
        "gone": ("hanger-gone.gcode.3mf", "gcode", _sliced("A1", {"1": "hook.stl"}, 60, 1.0), reader_own.id, None),
    }
    files = {}
    for label, (filename, file_type, meta, owner, folder_id) in specs.items():
        f = LibraryFile(
            filename=filename,
            file_path=filename,
            file_size=1,
            file_type=file_type,
            file_metadata=meta,
            created_by_id=owner,
            folder_id=folder_id,
        )
        db_session.add(f)
        await db_session.flush()
        await sync_product_for_file(db_session, library_file_id=f.id, product_ids=[product.id])
        files[label] = f.id
    (await db_session.get(LibraryFile, files["gone"])).deleted_at = datetime.now(UTC)
    await db_session.commit()
    parts = {
        p.name_key.removesuffix(".stl"): p.id
        for p in (await db_session.execute(ProductPart.__table__.select().where(ProductPart.product_id == product.id)))
    }
    return {"product": product.id, "files": files, "parts": parts, "folder": folder.id}


def _as(name: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(data={'sub': name})}"}


async def _sources(client, hanger, who="test_admin") -> dict[int, dict]:
    r = await client.get(f"/api/v1/products/{hanger['product']}/sources", headers=_as(who))
    assert r.status_code == 200, r.text
    return {row["part_id"]: row for row in r.json()["parts"]}


@pytest.mark.asyncio
async def test_a_parts_sources_follow_the_plans_order_and_recommend_the_first(async_client, hanger):
    """PS1 / PS2: the X1C plate makes 4 hooks an hour, the P1S plate 2 — the plan's key
    puts X1C first and recommends it; the unsliced project comes after, shown but
    outside ``yield_*``; K3 models are the sliced sources' models."""
    hook = (await _sources(async_client, hanger))[hanger["parts"]["hook"]]
    files = hanger["files"]
    assert [(s["library_file_id"], s["sliced"], s["yield"], s["recommended"]) for s in hook["sources"]] == [
        (files["x1c"], True, 4, True),
        (files["p1s"], True, 2, False),
        (files["raw"], False, 1, False),
    ]
    p1s = hook["sources"][1]
    assert (p1s["filename"], p1s["folder_id"], p1s["folder_name"], p1s["hidden"]) == (
        "hanger-p1s.gcode.3mf",
        hanger["folder"],
        "Shelf A",
        False,
    )
    assert (p1s["printer_model"], p1s["print_time_seconds"], p1s["filament_used_grams"]) == ("P1S", 3600, 20.0)
    assert (hook["has_sliced_source"], hook["yield_min"], hook["yield_max"], hook["hidden_sources"]) == (True, 2, 4, 0)


@pytest.mark.asyncio
async def test_a_hidden_source_keeps_its_numbers_and_loses_its_name(async_client, hanger):
    """LV4 for a read-own caller: somebody else's P1S file and the ownerless project
    are hidden — no name, no folder — while plate, model, yield, time and grams stay."""
    hook = (await _sources(async_client, hanger, "ps_own"))[hanger["parts"]["hook"]]
    by_file = {s["library_file_id"]: s for s in hook["sources"]}
    hidden = by_file[hanger["files"]["p1s"]]
    assert (hidden["filename"], hidden["folder_id"], hidden["folder_name"], hidden["hidden"]) == (
        None,
        None,
        None,
        True,
    )
    assert (hidden["printer_model"], hidden["yield"], hidden["print_time_seconds"]) == ("P1S", 2, 3600)
    assert by_file[hanger["files"]["x1c"]]["filename"] == "hanger-x1c.gcode.3mf"
    assert (hook["hidden_sources"], hook["yield_min"], hook["yield_max"]) == (2, 2, 4)
    nobody = (await _sources(async_client, hanger, "ps_none"))[hanger["parts"]["hook"]]
    assert all(s["filename"] is None and s["hidden"] for s in nobody["sources"])
    assert nobody["hidden_sources"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("who", "named", "hidden_count"),
    [
        ("test_admin", {"p1s", "x1c", "raw", "stl"}, 0),
        ("ps_all", {"p1s", "x1c", "raw", "stl"}, 0),
        ("ps_own", {"x1c", "stl"}, 2),
        ("ps_none", set(), 4),
    ],
)
async def test_the_files_tab_lists_every_linked_file_and_names_what_the_library_shows(
    async_client, hanger, who, named, hidden_count
):
    """PS7: every linked file outside the trash — the STL without plates too — with its
    plates; a hidden one is listed without its name."""
    r = await async_client.get(f"/api/v1/products/{hanger['product']}/files", headers=_as(who))
    assert r.status_code == 200, r.text
    body = r.json()
    files = hanger["files"]
    label = {fid: name for name, fid in files.items()}
    listed = {label[f["library_file_id"]]: f for f in body["files"]}
    assert set(listed) == {"p1s", "x1c", "raw", "stl"}
    assert {name for name, f in listed.items() if f["filename"] is not None} == named
    assert body["hidden_files"] == hidden_count
    stl, raw, p1s = listed["stl"], listed["raw"], listed["p1s"]
    assert (stl["plan_eligible"], stl["sliced_any"], stl["plates"]) == (False, False, [])
    assert (raw["plan_eligible"], raw["sliced_any"], len(raw["plates"])) == (True, False, 1)
    assert (p1s["printer_model"], p1s["sliced_any"], p1s["plates"][0]["print_time_seconds"]) == ("P1S", True, 3600)
    assert p1s["plates"][0]["filename"] == p1s["filename"]
    assert p1s["plates"][0]["hidden"] is p1s["hidden"]


@pytest.mark.asyncio
async def test_the_plates_list_hides_a_files_name_and_keeps_the_plate(async_client, hanger):
    """K5: ``filename`` is ``null`` + ``hidden`` for a file the library would not show."""
    r = await async_client.get(f"/api/v1/products/{hanger['product']}/plates", headers=_as("ps_own"))
    assert r.status_code == 200, r.text
    by_file = {p["library_file_id"]: p for p in r.json()}
    assert (by_file[hanger["files"]["p1s"]]["filename"], by_file[hanger["files"]["p1s"]]["hidden"]) == (None, True)
    assert by_file[hanger["files"]["p1s"]]["printer_model"] == "P1S"
    assert (by_file[hanger["files"]["x1c"]]["filename"], by_file[hanger["files"]["x1c"]]["hidden"]) == (
        "hanger-x1c.gcode.3mf",
        False,
    )


async def _parts(client, who="test_admin", **params):
    r = await client.get("/api/v1/products/parts", params={"page": 1, **params}, headers=_as(who))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_the_parts_picker_carries_sources_and_searches_visible_file_names(async_client, hanger):
    """PS2 / PS3: a row carries its sources; ``q`` finds a part by a file name the caller
    may see, and never by one it may not."""
    rows = {r["part_id"]: r for r in (await _parts(async_client))["items"]}
    hook = rows[hanger["parts"]["hook"]]
    assert (hook["has_sliced_source"], hook["yield_min"], hook["yield_max"], hook["models"]) == (
        True,
        2,
        4,
        ["P1S", "X1C"],
    )
    assert rows[hanger["parts"]["bar"]]["models"] == ["P1S"]
    assert len((await _parts(async_client, q="hanger-x1c"))["items"]) == 2
    assert len((await _parts(async_client, "ps_own", q="hanger-x1c"))["items"]) == 2
    assert (await _parts(async_client, "ps_own", q="hanger-p1s"))["items"] == []
    assert (await _parts(async_client, "ps_none", q="hanger-x1c"))["items"] == []
    own_row = next(
        r for r in (await _parts(async_client, "ps_own"))["items"] if r["part_id"] == hanger["parts"]["hook"]
    )
    assert own_row["hidden_sources"] == 2


@pytest.mark.asyncio
async def test_model_none_keeps_the_products_nothing_is_sliced_for(async_client, db_session, hanger):
    """PS4: the model filter is the product's; ``none`` — no printable file at all."""
    bare = Product(name="Bare")
    db_session.add(bare)
    await db_session.flush()
    db_session.add(ProductPart(product_id=bare.id, kind="printed", name="leg", name_key="leg", qty_per_unit=1))
    await db_session.commit()
    names = {r["name"] for r in (await _parts(async_client, model="none"))["items"]}
    assert names == {"leg"}
    assert {r["name"] for r in (await _parts(async_client, model="X1C"))["items"]} == {"hook.stl", "bar.stl"}


@pytest.mark.asyncio
async def test_the_picker_has_no_whole_list(async_client, hanger):
    """K7."""
    r = await async_client.get("/api/v1/products/parts", params={"page": 1, "all": "true"})
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_a_page_reads_the_library_once(async_client, hanger, test_engine):
    from backend.tests.unit.services.test_product_composition import counting_statements

    await _parts(async_client)
    with counting_statements(test_engine, match="FROM library_files") as seen:
        await _parts(async_client)
    # the page's own read, and the parts' pictures (part thumbnails E4) -- one batch each, never one per row
    assert len(seen) == 2, seen


# ---------- WS-13 E1 ES: one standard unit, from scratch, in whole plates ----------


async def _product_with(db, name: str, parts: list[tuple[str, str, int, float | None]], files: list[dict]) -> int:
    """A product: ``parts`` = (name, kind, per, unit_price); ``files`` = LibraryFile
    kwargs, each linked (its plates yield by object name)."""
    product = Product(name=name)
    db.add(product)
    await db.flush()
    for part_name, kind, per, price in parts:
        key = part_name if kind == "printed" else f"purchased:{part_name}"
        db.add(
            ProductPart(
                product_id=product.id, kind=kind, name=part_name, name_key=key, qty_per_unit=per, unit_price=price
            )
        )
    await db.flush()
    for kwargs in files:
        f = LibraryFile(file_size=1, **kwargs)
        db.add(f)
        await db.flush()
        await sync_product_for_file(db, library_file_id=f.id, product_ids=[product.id])
    await db.commit()
    return product.id


def _gcode(name: str, objects: dict[str, str], *, seconds: int | None = 600, grams: float | None = 10.0) -> dict:
    plate: dict = {"index": 1, "printable_objects": objects, "filaments": [{"type": "PLA"}]}
    if seconds is not None:
        plate["print_time_seconds"] = seconds
    if grams is not None:
        plate["filament_used_grams"] = grams
    return {"filename": name, "file_path": name, "file_type": "gcode", "file_metadata": {"plates": [plate]}}


async def _estimate(client, product_id: int) -> dict:
    r = await client.get(f"/api/v1/products/{product_id}/estimate")
    assert r.status_code == 200, r.text
    return r.json()


async def _rate(db, per_kg: str | None) -> None:
    from sqlalchemy import delete

    from backend.app.api.routes.settings import set_setting
    from backend.app.models.settings import Settings

    if per_kg is None:
        await db.execute(delete(Settings).where(Settings.key == "default_filament_cost"))
    else:
        await set_setting(db, "default_filament_cost", per_kg)
    await db.commit()


@pytest.mark.asyncio
async def test_a_plate_with_two_parts_is_printed_once(async_client, db_session):
    """ES1: whole plates — one plate yields both parts of the unit, so one print."""
    await _rate(db_session, "20")
    pid = await _product_with(
        db_session,
        "Pair",
        [("a", "printed", 1, None), ("b", "printed", 1, None)],
        [_gcode("pair.gcode.3mf", {"1": "a", "2": "b", "3": "a_2"})],
    )
    body = await _estimate(async_client, pid)
    assert (body["prints"], body["print_time_seconds"], body["filament_grams"], body["filament_cost"]) == (
        1,
        600,
        10.0,
        0.2,
    )
    assert body["surplus"] == [{"part_id": body["surplus"][0]["part_id"], "name": "a", "count": 1}]
    assert (body["complete"], body["reasons"]) == (True, [])
    assert (body["purchased_cost"], body["purchased_known_cost"], body["purchased_partial"]) == (0.0, 0.0, False)


@pytest.mark.asyncio
async def test_every_reason_on_its_own_state(async_client, db_session):
    """ES3, in its fixed order: a part without a plate (units), a part only on an
    unsliced project (units), a row without time, a row without grams (the numbers
    are the known part), a purchased part without a price."""
    await _rate(db_session, "20")
    raw = {
        "filename": "raw.3mf",
        "file_path": "raw.3mf",
        "file_type": "3mf",
        "file_metadata": {"has_sliced_gcode": False, "plates": [{"index": 1, "printable_objects": {"1": "d"}}]},
    }
    pid = await _product_with(
        db_session,
        "Mixed",
        [
            ("a", "printed", 1, None),
            ("b", "printed", 1, None),
            ("c", "printed", 2, None),
            ("d", "printed", 3, None),
            ("screw", "purchased", 4, None),
            ("nut", "purchased", 1, 0.5),
        ],
        [_gcode("a.gcode.3mf", {"1": "a"}, seconds=None), _gcode("b.gcode.3mf", {"1": "b"}, grams=None), raw],
    )
    body = await _estimate(async_client, pid)
    assert body["reasons"] == [
        {"code": "no_plate", "count": 2},
        {"code": "needs_slicing", "count": 3},
        {"code": "unknown_time", "count": 1},
        {"code": "unknown_weight", "count": 1},
        {"code": "unknown_purchase_price", "count": 1},
    ]
    assert body["complete"] is False
    assert (body["prints"], body["print_time_seconds"], body["filament_grams"]) == (2, None, 10.0)
    assert body["filament_cost"] == 0.2  # the known part: one row of 10 g at 20 per kg
    assert (body["purchased_cost"], body["purchased_known_cost"], body["purchased_partial"]) == (None, 0.5, True)


@pytest.mark.asyncio
async def test_a_stopped_plan_is_a_reason_with_nothing_unplaced(async_client, db_session, monkeypatch):
    from backend.app.services import plan_engine

    pid = await _product_with(db_session, "Twice", [("a", "printed", 2, None)], [_gcode("a.gcode.3mf", {"1": "a"})])
    monkeypatch.setattr(plan_engine, "MAX_ITERATIONS", 1)
    body = await _estimate(async_client, pid)
    assert body["reasons"] == [{"code": "truncated", "count": None}] and body["complete"] is False


@pytest.mark.asyncio
async def test_no_filament_rate_is_no_cost_and_not_a_reason(async_client, db_session):
    """ES3: the rate is the farm's setting, not the product's data."""
    await _rate(db_session, None)
    pid = await _product_with(db_session, "Plain", [("a", "printed", 1, None)], [_gcode("a.gcode.3mf", {"1": "a"})])
    body = await _estimate(async_client, pid)
    assert (body["filament_cost"], body["complete"], body["reasons"]) == (None, True, [])


@pytest.mark.asyncio
async def test_bought_parts_only_are_complete_exactly_when_every_price_is_known(async_client, db_session):
    """ES4: nothing to print is a known zero; a zero price is a price."""
    await _rate(db_session, "20")
    priced = await _product_with(
        db_session, "Kit", [("screw", "purchased", 4, 0.25), ("free", "purchased", 1, 0.0)], []
    )
    body = await _estimate(async_client, priced)
    assert (body["prints"], body["print_time_seconds"], body["filament_grams"], body["filament_cost"]) == (
        0,
        0,
        0.0,
        0.0,
    )
    assert (body["purchased_cost"], body["complete"], body["reasons"]) == (1.0, True, [])
    unpriced = await _product_with(db_session, "Bag", [("bolt", "purchased", 2, None)], [])
    body = await _estimate(async_client, unpriced)
    assert (body["complete"], body["reasons"]) == (False, [{"code": "unknown_purchase_price", "count": 1}])
    assert (body["purchased_cost"], body["purchased_known_cost"], body["purchased_partial"]) == (None, 0.0, True)


@pytest.mark.asyncio
async def test_an_empty_composition_is_not_a_complete_estimate(async_client, db_session):
    pid = await _product_with(db_session, "Nothing", [("zero", "printed", 0, None)], [])
    body = await _estimate(async_client, pid)
    assert (body["complete"], body["reasons"]) == (False, [{"code": "empty_composition", "count": None}])


@pytest.mark.asyncio
async def test_a_printed_part_without_a_plate_is_no_known_zero(async_client, db_session):
    """Final review I4 (Z8): nothing to plan for a printed part is unknown time and cost,
    not 0 s and 0.00 — the known zero belongs to a product of bought parts alone."""
    await _rate(db_session, "20")
    pid = await _product_with(db_session, "Plateless", [("a", "printed", 1, None)], [])
    body = await _estimate(async_client, pid)
    assert (body["prints"], body["print_time_seconds"], body["filament_cost"]) == (0, None, None)
    assert body["reasons"] == [{"code": "no_plate", "count": 1}] and body["complete"] is False
