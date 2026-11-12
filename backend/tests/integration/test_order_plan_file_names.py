"""The order's print plan names a library file only as far as the library shows it (WS-13 E1 LV5).

``GET /projects/{id}/plan`` is open to ``orders:read``, and each row and each
alternative plate carries the name of the file it prints. Before LV5 that name
went to every reader, so a caller the library would refuse learned a file's name
from an order it may read. The rule is the library's own (``library_name_scope``
+ ``file_name_visible``): ``library:read_all`` sees every file, ``library:read_own``
its own only (an ownerless file fails closed), anybody else none; an API key sees
them all only if its scope AND its owner pass ``library:read_all``. A hidden file
is ``filename: null, hidden: true`` — on the row and on each alternative — and
nothing else in the plan moves: the engine does not know who is reading.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from backend.app.api.routes import projects as projects_routes
from backend.app.core.auth import create_access_token, generate_api_key
from backend.app.core.permissions import Permission
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.models.user import User
from backend.tests.auth_helpers import bind_test_key, mint_test_access
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.unit.services.test_product_composition import counting_statements

pytestmark = pytest.mark.integration

_READ = Permission.ORDERS_READ.value
_ALL = Permission.LIBRARY_READ_ALL.value
_OWN = Permission.LIBRARY_READ_OWN.value

#: The plate files in speed order — the fastest is the row the engine picks, the
#: others its alternatives — and whose each one is.
_LAYOUTS = {
    "row_foreign": ("foreign", "own", "ownerless"),
    "row_own": ("own", "foreign", "ownerless"),
}
_MODELS = ("X1C", "P1S", "A1")
_VISIBLE = {"all": {"own", "foreign", "ownerless"}, "own": {"own"}, "none": set()}
_CALLERS = {
    "test_admin": "all",
    "pn_all": "all",
    "pn_own": "own",
    "pn_none": "none",
    "key_with_library": "all",
    "x_key_with_library": "all",
    "key_without_library": "none",
}


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


async def _key(db, owner: User) -> str:
    raw, key_hash, key_prefix = generate_api_key()
    issued_key = APIKey(
        name=f"k-{key_prefix}",
        key_hash=key_hash,
        key_prefix=key_prefix,
        enabled=True,
        user_id=owner.id,
        can_read_status=True,
    )
    await bind_test_key(db, issued_key)
    db.add(issued_key)
    await db.commit()
    return raw


async def _plate_file(db, tmp_path, *, name: str, model: str, seconds: int, owner: User | None) -> LibraryFile:
    """One sliced plate making ``1 shade + 2 arms`` — every file of the product
    yields the same, so the slower ones ride out as the row's alternatives."""
    file = LibraryFile(
        filename=name,
        file_path=str(
            write_routing_3mf(
                tmp_path / name, {1: [{"id": 1, "type": "PETG", "color": "#FFFFFF", "used_g": "1"}]}, model=model
            )
        ),
        file_size=1,
        file_type="gcode",
        created_by_id=owner.id if owner else None,
        file_metadata={
            "sliced_for_model": model,
            "plates": [
                {
                    "index": 1,
                    "printable_objects": {"1": "shade", "2": "arm", "3": "arm_2"},
                    "print_time_seconds": seconds,
                    "filaments": [{"slot_id": 1, "type": "PETG"}],
                }
            ],
        },
    )
    db.add(file)
    await db.flush()
    return file


@pytest.fixture
async def people(db_session):
    # Two service-only cases omit the HTTP client that normally seeds this account.
    if await db_session.scalar(select(User.id).where(User.username == "test_admin")) is None:
        await _user(db_session, "test_admin", [_READ, _ALL])
    reader_all = await _user(db_session, "pn_all", [_READ, _ALL])
    reader_own = await _user(db_session, "pn_own", [_READ, _OWN])
    await _user(db_session, "pn_none", [_READ])
    key_owner = await _user(db_session, "pn_key_owner", [_READ])
    return {
        "owners": {"own": reader_own, "foreign": reader_all, "ownerless": None},
        "headers": {
            **{
                name: {"Authorization": f"Bearer {await mint_test_access(db_session, username=name)}"}
                for name in ("test_admin", "pn_all", "pn_own", "pn_none")
            },
            "key_with_library": {"Authorization": f"Bearer {await _key(db_session, reader_all)}"},
            "x_key_with_library": {"X-API-Key": await _key(db_session, reader_all)},
            "key_without_library": {"Authorization": f"Bearer {await _key(db_session, key_owner)}"},
        },
    }


async def _lamp(db, tmp_path, owners: dict, labels: tuple[str, ...]) -> tuple[Product, dict[str, LibraryFile]]:
    """The product and its plate files, the i-th file owned by ``labels[i]`` and
    ``(i + 1) × 100`` s long."""
    product = Product(name="Lamp")
    db.add(product)
    await db.flush()
    db.add_all(
        [
            ProductPart(
                product_id=product.id, kind="printed", name="shade", name_key="shade", qty_per_unit=1, aliases=["shade"]
            ),
            ProductPart(
                product_id=product.id, kind="printed", name="arm", name_key="arm", qty_per_unit=2, aliases=["arm"]
            ),
        ]
    )
    files = {}
    for i, label in enumerate(labels):
        file = await _plate_file(
            db, tmp_path, name=f"lamp-{label}.gcode.3mf", model=_MODELS[i], seconds=(i + 1) * 100, owner=owners[label]
        )
        db.add(ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=0))
        files[label] = file
    await db.commit()
    return product, files


async def _order(client, product_id: int) -> int:
    r = await client.post(
        "/api/v1/projects/",
        json={"name": "O", "lines": [{"product_id": product_id, "quantity": 4, "material": "PETG"}]},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _without_names(body: dict) -> dict:
    """The plan with every file name and flag taken out — what must not depend on the reader."""
    for line in body["lines"]:
        for row in line["rows"]:
            row.pop("filename"), row.pop("hidden")
            row.pop("source_token", None)
            for alt in row["alternatives"]:
                alt.pop("filename"), alt.pop("hidden")
                alt.pop("source_token", None)
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("layout", sorted(_LAYOUTS))
@pytest.mark.parametrize("caller", sorted(_CALLERS))
async def test_the_plan_names_a_file_only_where_the_library_shows_it(
    committing_client, db_session, tmp_path, people, layout, caller
):
    labels = _LAYOUTS[layout]
    product, files = await _lamp(db_session, tmp_path, people["owners"], labels)
    pid = await _order(committing_client, product.id)

    r = await committing_client.get(f"/api/v1/projects/{pid}/plan", headers=people["headers"][caller])
    assert r.status_code == 200, r.text
    body = r.json()
    [row] = body["lines"][0]["rows"]
    by_file = {f.id: label for label, f in files.items()}
    seen = [row, *row["alternatives"]]
    # The engine's own pick: the fastest file is the row, the others its alternatives.
    assert by_file[row["library_file_id"]] == labels[0]
    assert {by_file[a["library_file_id"]] for a in row["alternatives"]} == set(labels[1:])
    visible = _VISIBLE[_CALLERS[caller]]
    for plate in seen:
        label = by_file[plate["library_file_id"]]
        shown = label in visible
        assert plate["hidden"] is (not shown), (label, plate)
        assert plate["filename"] == (files[label].filename if shown else None), (label, plate)
        assert (len(plate["source_token"]) == 64) if shown else plate["source_token"] is None

    # Rights change the names, never the plan: ids, counts, models and figures
    # are the ones the administrator is answered.
    admin = (
        await committing_client.get(f"/api/v1/projects/{pid}/plan", headers=people["headers"]["test_admin"])
    ).json()
    assert _without_names(body) == _without_names(admin)


@pytest.mark.asyncio
async def test_a_file_gone_since_the_plan_was_made_has_no_name(db_session, tmp_path, people):
    """The name is taken from the batch read alone — a file trashed or deleted
    between the engine's read and it is ``hidden``, never named from the plan's
    own copy of the row."""
    _product, files = await _lamp(db_session, tmp_path, people["owners"], ("own", "foreign"))
    trashed = files["foreign"]
    trashed.deleted_at = datetime.now(UTC)
    await db_session.commit()
    names = await projects_routes._plan_file_names(
        db_session, {files["own"].id, trashed.id, 999_999}, people["owners"]["foreign"], "all"
    )
    assert names == {files["own"].id: files["own"].filename}


@pytest.mark.asyncio
async def test_the_names_cost_one_query_and_none_without_files(db_session, test_engine, tmp_path, people):
    """LV5 adds ONE batched read over every row's and alternative's file — and
    nothing at all when the plan names no file, or when the caller may see no
    name anyway."""
    _product, files = await _lamp(db_session, tmp_path, people["owners"], ("own", "foreign", "ownerless"))
    ids = {f.id for f in files.values()}
    user = people["owners"]["foreign"]
    with counting_statements(test_engine) as seen:
        await projects_routes._plan_file_names(db_session, ids, user, "all")
    assert len(seen) == 1 and "library_files" in seen[0], seen
    for file_ids, scope in ((set(), "all"), (ids, "none")):
        with counting_statements(test_engine) as seen:
            assert await projects_routes._plan_file_names(db_session, file_ids, user, scope) == {}
        assert seen == [], (file_ids, scope)


@pytest.mark.asyncio
async def test_a_plate_more_costs_the_plan_no_query_more(committing_client, db_session, test_engine, tmp_path, people):
    """On the whole route: a plan with one more alternative plate runs exactly as
    many statements — the names are read per plan, not per row or alternative."""

    async def statements(labels: tuple[str, ...]) -> int:
        folder = tmp_path / "-".join(labels)
        folder.mkdir()
        product, _files = await _lamp(db_session, folder, people["owners"], labels)
        pid = await _order(committing_client, product.id)
        headers = people["headers"]["pn_own"]
        await committing_client.get(f"/api/v1/projects/{pid}/plan", headers=headers)  # warm the auth caches
        with counting_statements(test_engine) as seen:
            r = await committing_client.get(f"/api/v1/projects/{pid}/plan", headers=headers)
        assert r.status_code == 200, r.text
        assert len(r.json()["lines"][0]["rows"][0]["alternatives"]) == len(labels) - 1
        return len(seen)

    assert await statements(("own", "foreign")) == await statements(("own", "foreign", "ownerless"))
