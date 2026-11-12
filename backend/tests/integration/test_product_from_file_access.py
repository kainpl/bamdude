"""«New product from file» asks the library's own authority, and says what the file gave (WS-13 E10 A01–A02).

``POST /products/from-file/{id}`` used to take any active library file by id under
the catalog's create right alone: a caller who may not see a file in the library could still
create a product from it, and so read its card (title, designer, description, plates)
through the product. A01 asks exactly what the library asks — ``library_name_scope``
(``all`` / ``own`` / ``none``, an API key by its scope AND its owner) and then
``file_name_visible`` for this file — and refuses with the library's own 404 before
anything is read or written. A02 returns the card fill's notes beside the product, the
way the re-read does, instead of only logging them.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from backend.app.core.auth import create_access_token, generate_api_key
from backend.app.core.permissions import Permission
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.library import LibraryFile
from backend.app.models.product import Product
from backend.app.models.user import User
from backend.tests.auth_helpers import bind_test_key, mint_test_access

pytestmark = pytest.mark.integration

_CREATE = Permission.PRODUCTS_CREATE.value
_READ = Permission.PRODUCTS_READ.value
_ALL = Permission.LIBRARY_READ_ALL.value
_OWN = Permission.LIBRARY_READ_OWN.value

_META = {"plates": [{"index": 1, "printable_objects": {"1": "body.stl"}}]}


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


async def _key(db, owner: User | None, *, projects: bool = True, library: bool = True) -> str:
    raw, key_hash, key_prefix = generate_api_key()
    issued_key = APIKey(
        name=f"k-{key_prefix}",
        key_hash=key_hash,
        key_prefix=key_prefix,
        enabled=True,
        user_id=owner.id if owner else None,
        # The scope column behind `library:read_*` for a key.
        can_read_status=library,
        can_manage_projects=projects,
    )
    if owner is not None:
        await bind_test_key(db, issued_key, owner)
    db.add(issued_key)
    await db.commit()
    return raw


@pytest.fixture
async def library(db_session):
    """Files of every kind of owner, and callers of every kind of library authority —
    all of them allowed to create products."""
    reader_all = await _user(db_session, "ff_all", [_READ, _CREATE, _ALL])
    reader_own = await _user(db_session, "ff_own", [_READ, _CREATE, _OWN])
    await _user(db_session, "ff_none", [_READ, _CREATE])
    owner_without = await _user(db_session, "ff_key_owner", [_READ, _CREATE])
    files = {}
    for label, owner, trashed in (
        ("own", reader_own, False),
        ("foreign", reader_all, False),
        ("ownerless", None, False),
        ("trashed", reader_own, True),
    ):
        file = LibraryFile(
            filename=f"ff-{label}.gcode.3mf",
            file_path=f"ff-{label}",
            file_size=1,
            file_type="gcode",
            created_by_id=owner.id if owner else None,
            file_metadata=_META,
            deleted_at=datetime.now(UTC) if trashed else None,
        )
        db_session.add(file)
        await db_session.flush()
        files[label] = file.id
    await db_session.commit()
    return {
        "files": files,
        "jwt": {name: await mint_test_access(db_session, username=name) for name in ("ff_all", "ff_own", "ff_none")},
        "key_with_library": await _key(db_session, reader_all),
        "key_without_library": await _key(db_session, owner_without),
        # The owner reads the whole library; the key's own scope does not let it.
        "key_scope_without_library": await _key(db_session, reader_all, library=False),
    }


def _headers(library, caller: str) -> dict:
    if caller.startswith("x_"):
        return {"X-API-Key": library[caller[2:]]}
    if caller in library:
        return {"Authorization": f"Bearer {library[caller]}"}
    return {"Authorization": f"Bearer {library['jwt'][caller]}"}


async def _products(db) -> int:
    return await db.scalar(select(func.count()).select_from(Product))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller", "allowed"),
    [
        ("ff_all", {"own", "foreign", "ownerless"}),
        ("ff_own", {"own"}),
        ("ff_none", set()),
        ("key_with_library", {"own", "foreign", "ownerless"}),
        ("x_key_with_library", {"own", "foreign", "ownerless"}),
        ("key_without_library", set()),
        ("key_scope_without_library", set()),
        ("x_key_scope_without_library", set()),
    ],
)
async def test_a_product_comes_only_from_a_file_the_library_shows_the_caller(
    committing_client, db_session, library, caller, allowed
):
    for label in ("own", "foreign", "ownerless", "trashed"):
        before = await _products(db_session)
        r = await committing_client.post(
            f"/api/v1/products/from-file/{library['files'][label]}", headers=_headers(library, caller)
        )
        if label in allowed:
            assert r.status_code == 200, (caller, label, r.text)
            assert await _products(db_session) == before + 1
        else:
            # The library's own answer — the same 404 a missing file gets — and nothing written.
            assert r.status_code == 404, (caller, label, r.text)
            assert r.json()["detail"] == "Library file not found"
            assert await _products(db_session) == before


@pytest.mark.asyncio
async def test_the_answer_carries_the_product_and_what_the_file_gave(committing_client, db_session):
    file = LibraryFile(filename="m.gcode.3mf", file_path="m", file_size=1, file_type="gcode", file_metadata=_META)
    db_session.add(file)
    await db_session.commit()
    r = await committing_client.post(f"/api/v1/products/from-file/{file.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"product", "notes"}
    assert body["product"]["name"] == "m"
    assert body["product"]["library_file_ids"] == [file.id]
    # The file is not on disk here: the fill says so in a note, not only in a log line.
    assert [n["code"] for n in body["notes"]] == ["file_missing"]
