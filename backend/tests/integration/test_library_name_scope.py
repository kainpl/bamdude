"""A library file's NAME reaches a caller only as far as the library shows it (WS-13 E1 LV1–LV3).

The catalog searches the names of the files linked to a product. Before LV that
search ran for anyone with the catalog's read, so a caller the library would refuse
could still learn a file's name by searching for it — and the search's ``total``
and category counts told the same thing without a single row. The rule is the
library's own: ``library:read_all`` sees every file, ``library:read_own`` only
its own (an ownerless file fails closed), anybody else no file at all; an API key
sees them all only if its scope AND its owner pass ``library:read_all``. A trashed
file is never searched.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from backend.app.core.auth import create_access_token, generate_api_key
from backend.app.core.permissions import Permission
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.library import LibraryFile
from backend.app.models.product import Product
from backend.app.models.product_category import ProductCategory, category_key
from backend.app.models.user import User
from backend.app.services.product_sync import sync_product_for_file
from backend.tests.auth_helpers import bind_test_key, mint_test_access

pytestmark = pytest.mark.integration

_READ = Permission.PRODUCTS_READ.value
_ALL = Permission.LIBRARY_READ_ALL.value
_OWN = Permission.LIBRARY_READ_OWN.value


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


async def _key(db, owner: User | None) -> str:
    raw, key_hash, key_prefix = generate_api_key()
    issued_key = APIKey(
        name=f"k-{key_prefix}",
        key_hash=key_hash,
        key_prefix=key_prefix,
        enabled=True,
        user_id=owner.id if owner else None,
        can_read_status=True,
    )
    if owner is not None:
        await bind_test_key(db, issued_key, owner)
    db.add(issued_key)
    await db.commit()
    return raw


@pytest.fixture
async def shelf(db_session):
    """Four products, each linked to one «zebra-…» file: the reader-own user's own
    file, somebody else's, an ownerless one and a trashed one of the reader's own.
    Product names never contain the word, so only the file name can match."""
    reader_all = await _user(db_session, "lv_all", [_READ, _ALL])
    reader_own = await _user(db_session, "lv_own", [_READ, _OWN])
    await _user(db_session, "lv_none", [_READ])
    owner_without = await _user(db_session, "lv_key_owner", [_READ])
    shelves = ProductCategory(name="Shelves", name_key=category_key("Shelves"))
    db_session.add(shelves)
    await db_session.flush()
    from datetime import UTC, datetime

    ids = {}
    for label, product_name, owner, trashed in (
        ("own", "Alpha", reader_own, False),
        ("foreign", "Beta", reader_all, False),
        ("ownerless", "Gamma", None, False),
        ("trashed", "Delta", reader_own, True),
    ):
        product = Product(name=product_name, category_id=shelves.id)
        file = LibraryFile(
            filename=f"zebra-{label}.gcode.3mf",
            file_path=f"zebra-{label}.gcode.3mf",
            file_size=1,
            file_type="gcode",
            created_by_id=owner.id if owner else None,
            file_metadata={"plates": [{"index": 1, "printable_objects": {"1": f"{label}.stl"}}]},
        )
        db_session.add_all([product, file])
        await db_session.flush()
        await sync_product_for_file(db_session, library_file_id=file.id, product_ids=[product.id])
        if trashed:
            file.deleted_at = datetime.now(UTC)
        ids[label] = product.id
    await db_session.commit()
    return {
        "ids": ids,
        "category": shelves.id,
        "tokens": {
            name: await mint_test_access(db_session, username=name)
            for name in ("test_admin", "lv_all", "lv_own", "lv_none")
        },
        "key_with_library": await _key(db_session, reader_all),
        "key_without_library": await _key(db_session, owner_without),
        "legacy_key": await _key(db_session, None),
    }


async def _search(client: AsyncClient, headers: dict) -> dict:
    r = await client.get("/api/v1/products", params={"page": 1, "q": "zebra"}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _jwt(shelf, name):
    return {"Authorization": f"Bearer {shelf['tokens'][name]}"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller", "visible"),
    [
        ("test_admin", {"own", "foreign", "ownerless"}),
        ("lv_all", {"own", "foreign", "ownerless"}),
        ("lv_own", {"own"}),
        ("lv_none", set()),
        ("key_with_library", {"own", "foreign", "ownerless"}),
        ("x_key_with_library", {"own", "foreign", "ownerless"}),
        ("key_without_library", set()),
    ],
)
async def test_a_file_name_is_searched_only_where_the_library_shows_it(async_client, shelf, caller, visible):
    if caller.startswith("x_"):
        headers = {"X-API-Key": shelf[caller[2:]]}
    elif caller in shelf:
        headers = {"Authorization": f"Bearer {shelf[caller]}"}
    else:
        headers = _jwt(shelf, caller)
    body = await _search(async_client, headers)
    by_id = {pid: label for label, pid in shelf["ids"].items()}
    assert {by_id[item["id"]] for item in body["items"]} == visible
    # Neither the total nor the category panel may count what the rows leave out.
    assert body["meta"]["total"] == len(visible)
    counted = sum(c["count"] for c in body["categories"]) + body["uncategorized"]
    assert counted == len(visible)


@pytest.mark.asyncio
async def test_a_legacy_ownerless_key_cannot_search_file_names(async_client, shelf):
    # C strict reissue: ownerless legacy credentials cannot become a current owner.
    response = await async_client.get(
        "/api/v1/products",
        params={"page": 1, "q": "zebra"},
        headers={"Authorization": f"Bearer {shelf['legacy_key']}"},
    )
    assert response.status_code == 401, response.text


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("all", {"own": True, "foreign": True, "ownerless": True, "trashed": False}),
        ("own", {"own": True, "foreign": False, "ownerless": False, "trashed": False}),
        ("none", {"own": False, "foreign": False, "ownerless": False, "trashed": False}),
    ],
)
def test_the_python_predicate_answers_as_the_sql_one(scope, expected):
    """LV2: one decision in both languages — ``none`` hides the caller's own file too."""
    from datetime import UTC, datetime

    from backend.app.api.routes.library import file_name_visible

    me = User(username="me", email="me@example.com", password_hash="x", role="user")
    me.id = 7
    files = {
        "own": LibraryFile(filename="a", file_path="a", file_size=1, file_type="gcode", created_by_id=7),
        "foreign": LibraryFile(filename="b", file_path="b", file_size=1, file_type="gcode", created_by_id=8),
        "ownerless": LibraryFile(filename="c", file_path="c", file_size=1, file_type="gcode", created_by_id=None),
        "trashed": LibraryFile(
            filename="d", file_path="d", file_size=1, file_type="gcode", created_by_id=7, deleted_at=datetime.now(UTC)
        ),
    }
    assert {label: file_name_visible(f, me, scope) for label, f in files.items()} == expected
