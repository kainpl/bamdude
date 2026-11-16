"""«Generate Thumbnails» works for ``library:update_own`` on the caller's own files (upstream #2990, G11 U5).

The toolbar button and the file menu entry are offered to anyone who may edit
library files, their own included; the route asked ``library:update_all`` and
answered 403. It now takes either right, and with ``update_own`` every selection
— all missing, a folder, named ids — is narrowed to the caller's own files; the
picture is published under that same right and only onto a row still theirs.
"""

import secrets

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.core.database import Base, import_all_models
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.tests.auth_helpers import mint_test_access

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_PW = "Aa1!" + secrets.token_urlsafe(12)  # pragma: allowlist secret
URL = "/api/v1/library/generate-stl-thumbnails"


async def _admin_headers() -> dict:
    return {"Authorization": f"Bearer {await mint_test_access(username='test_admin')}"}


async def _make_user(async_client: AsyncClient, *, username: str, permissions: list[str]) -> tuple[dict, int]:
    headers = await _admin_headers()
    grp = await async_client.post(
        "/api/v1/groups/", headers=headers, json={"name": f"lt_{username}", "permissions": permissions}
    )
    assert grp.status_code == 201, grp.text
    user = await async_client.post(
        "/api/v1/users/",
        headers=headers,
        json={"username": username, "password": _PW, "role": "user", "group_ids": [grp.json()["id"]]},
    )
    assert user.status_code == 201, user.text
    login = await async_client.post("/api/v1/auth/login", json={"username": username, "password": _PW})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, user.json()["id"]


@pytest.fixture
async def rendered(monkeypatch):
    """The renderer is another process; what this route decides is WHICH files reach it, and under what right."""
    from backend.app.api.routes import library as routes

    seen = []

    async def record(db, source, **_kwargs):
        seen.append(source)
        return True

    monkeypatch.setattr(routes, "generate_library_preview", record)
    return seen


@pytest.fixture
async def files(async_client, db_session, tmp_path):
    alice, alice_id = await _make_user(
        async_client, username="lt_alice", permissions=["library:read_own", "library:update_own"]
    )
    _bob, bob_id = await _make_user(async_client, username="lt_bob", permissions=["library:read_own"])
    folder = LibraryFolder(name="models")
    db_session.add(folder)
    await db_session.flush()

    def a_file(name, owner):
        path = tmp_path / name
        path.write_bytes(b"solid x\n" + b"facet normal 0 0 1\n" * 200 + b"endsolid x\n")
        return LibraryFile(
            filename=name,
            file_path=str(path),
            file_type="stl",
            file_size=path.stat().st_size,
            folder_id=folder.id,
            created_by_id=owner,
        )

    rows = {
        "mine": a_file("mine.stl", alice_id),
        "theirs": a_file("theirs.stl", bob_id),
        "nobodys": a_file("n.stl", None),
    }
    db_session.add_all(rows.values())
    await db_session.commit()
    return {"alice": alice, "alice_id": alice_id, "folder": folder, **rows}


def _names(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {item["filename"] for item in response.json()["results"]}


async def test_update_own_generates_for_its_own_files_only(async_client, files, rendered):
    response = await async_client.post(URL, headers=files["alice"], json={"all_missing": True})

    assert _names(response) == {"mine.stl"}
    assert [source.filename for source in rendered] == ["mine.stl"]


async def test_update_own_cannot_name_another_users_file(async_client, files, rendered):
    ids = [files["mine"].id, files["theirs"].id, files["nobodys"].id]

    response = await async_client.post(URL, headers=files["alice"], json={"file_ids": ids})

    assert _names(response) == {"mine.stl"}
    assert [source.id for source in rendered] == [files["mine"].id]


async def test_update_own_in_a_folder_takes_only_its_own(async_client, files, rendered):
    response = await async_client.post(URL, headers=files["alice"], json={"folder_id": files["folder"].id})

    assert _names(response) == {"mine.stl"}


async def test_the_picture_is_published_under_the_right_the_caller_has(async_client, files, rendered):
    await async_client.post(URL, headers=files["alice"], json={"all_missing": True})
    await async_client.post(URL, json={"file_ids": [files["theirs"].id]})

    own, everything = rendered
    assert (own.permission, own.owner_id) == ("library:update_own", files["alice_id"])
    assert (everything.permission, everything.owner_id) == ("library:update_all", None)


async def test_update_all_keeps_its_scope(async_client, files, rendered):
    response = await async_client.post(URL, json={"all_missing": True})

    assert _names(response) == {"mine.stl", "theirs.stl", "n.stl"}


async def test_neither_right_is_still_refused(async_client, files, rendered):
    reader, _id = await _make_user(async_client, username="lt_reader", permissions=["library:read_own"])

    response = await async_client.post(URL, headers=reader, json={"all_missing": True})

    assert response.status_code == 403
    assert rendered == []


async def test_a_picture_is_not_attached_to_a_row_that_is_no_longer_the_callers(tmp_path, monkeypatch):
    """The publication's own check: the row changed hands while the picture was drawn."""
    from PIL import Image

    from backend.app.services.library_preview import Source, attach

    import_all_models()
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "library_dir", tmp_path / "library")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'publication.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    picture = tmp_path / "render.png"
    Image.new("RGBA", (32, 32), (0, 170, 30, 255)).save(picture)
    try:
        async with factory() as db:
            row = LibraryFile(
                filename="cube.stl",
                file_path="library/cube.stl",
                file_type="stl",
                file_size=1000,
                file_hash="a" * 64,
                created_by_id=7,
            )
            db.add(row)
            await db.commit()
            theirs = Source.capture(row, owner_id=8)
            mine = Source.capture(row, owner_id=7)

        async with factory() as db:
            assert await attach(db, theirs, picture) is False
            assert (await db.get(LibraryFile, row.id)).thumbnail_path is None
        async with factory() as db:
            assert await attach(db, mine, picture) is True
    finally:
        await engine.dispose()
