"""``/model-sources/manyfold/…`` end to end against the hand-built Manyfold. No network."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text

from backend.app.core.config import settings as app_settings
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.settings import Settings
from backend.app.services import library_import_meta
from backend.app.services.model_providers import outbound
from backend.app.services.model_providers.manyfold import service as manyfold
from backend.tests._fixtures.manyfold import BASE, CLIENT_ID, CLIENT_SECRET, DNS, MODEL, STL, FakeManyfold, openapi
from backend.tests.auth_helpers import mint_test_access

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

API = "/api/v1/model-sources/manyfold"
CONNECTION = f"{API}/connection"
STL_FILE = "f1c2d3g4h5j6"
SUPPORTED_FILE = "k7l8m9n0p1q2"


@pytest.fixture
def fake(monkeypatch, tmp_path: Path):
    """The fake Manyfold behind the real transport: its socket layer is the only thing replaced."""
    server = FakeManyfold()

    async def resolve(host, port):
        if host not in DNS:
            raise OSError("unscripted name")
        return list(DNS[host])

    monkeypatch.setattr(outbound, "resolve_host", resolve)
    monkeypatch.setattr(outbound, "_pinned_http", lambda sockets=None: httpx.MockTransport(server.handle))
    monkeypatch.setattr(app_settings, "archive_dir", str(tmp_path / "data"), raising=False)
    manyfold.clear_caches()
    yield server
    manyfold.clear_caches()


async def connect(async_client, **overrides) -> None:
    body = {"enabled": True, "url": BASE, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, **overrides}
    response = await async_client.put(CONNECTION, json=body)
    assert response.status_code == 200, response.text


async def limited(username: str, permissions: list[str]) -> dict[str, str]:
    """Headers of a user who holds exactly ``permissions``."""
    from backend.app.core.auth import get_password_hash
    from backend.app.core.database import async_session
    from backend.app.models.group import Group
    from backend.app.models.user import User

    async with async_session() as db:
        user = User(username=username, password_hash=get_password_hash("TestPass123!"), role="user")
        group = Group(name=f"{username}-group", permissions=permissions)
        db.add_all([user, group])
        await db.commit()
        await db.execute(
            text("INSERT INTO user_groups (user_id, group_id) VALUES (:uid, :gid)"), {"uid": user.id, "gid": group.id}
        )
        await db.commit()
    return {"Authorization": f"Bearer {await mint_test_access(username=username)}"}


class TestConnection:
    async def test_the_secret_is_never_returned(self, async_client, fake, db_session):
        await connect(async_client)
        response = await async_client.get(CONNECTION)
        assert response.json() == {
            "enabled": True,
            "url": BASE,
            "client_id": CLIENT_ID,
            "client_secret_configured": True,
        }
        assert CLIENT_SECRET not in response.text
        general = await async_client.get("/api/v1/settings/")
        assert CLIENT_SECRET not in general.text and "manyfold" not in general.text
        stored = (
            await db_session.execute(select(Settings.value).where(Settings.key == "manyfold_client_secret"))
        ).scalar()
        assert stored == CLIENT_SECRET

    async def test_an_omitted_secret_is_kept_while_the_destination_stays(self, async_client, fake):
        await connect(async_client)
        response = await async_client.put(CONNECTION, json={"enabled": False})
        assert response.status_code == 200
        assert response.json()["client_secret_configured"] is True and response.json()["enabled"] is False

    @pytest.mark.parametrize("change", [{"url": "http://other.lan:3214"}, {"client_id": "another-client"}])
    async def test_a_kept_secret_does_not_follow_a_changed_destination(self, async_client, fake, change):
        await connect(async_client)
        for route in (CONNECTION, f"{CONNECTION}/test"):
            method = async_client.put if route == CONNECTION else async_client.post
            response = await method(route, json=change)
            assert response.status_code == 400, response.text
            assert "replace or clear credential" in response.text
        assert (await async_client.get(CONNECTION)).json()["url"] == BASE
        assert all(request.url.host == "manyfold.lan" for request in fake.requests)

    async def test_the_destination_changes_with_a_new_secret_or_a_cleared_one(self, async_client, fake):
        await connect(async_client)
        cleared = await async_client.put(
            CONNECTION,
            json={
                "enabled": False,
                "url": "http://other.lan:3214",
                "secret_actions": {"manyfold_client_secret": "clear"},
            },
        )
        assert cleared.status_code == 200 and cleared.json()["client_secret_configured"] is False
        replaced = await async_client.put(
            CONNECTION,
            json={"url": BASE, "client_secret": "new-secret", "secret_actions": {"manyfold_client_secret": "replace"}},
        )
        assert replaced.status_code == 200 and replaced.json()["client_secret_configured"] is True

    async def test_it_cannot_be_switched_on_half_configured(self, async_client, fake):
        response = await async_client.put(CONNECTION, json={"enabled": True, "url": BASE})
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "url", ["http://169.254.169.254", "ftp://manyfold.lan", "http://user:pw@manyfold.lan", "http://2852039166"]
    )
    async def test_an_address_the_server_must_not_call_is_not_saved(self, async_client, fake, url):
        response = await async_client.put(CONNECTION, json={"url": url, "client_id": "x", "client_secret": "y"})
        assert response.status_code == 422, response.text
        assert (await async_client.get(CONNECTION)).json()["url"] == ""

    async def test_a_draft_is_tested_without_being_saved(self, async_client, fake):
        body = {"url": BASE, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
        response = await async_client.post(f"{CONNECTION}/test", json=body)
        assert response.json() == {"ok": True, "models": 3, "error": None, "message": None}
        assert (await async_client.get(CONNECTION)).json() == {
            "enabled": False,
            "url": "",
            "client_id": "",
            "client_secret_configured": False,
        }

    async def test_the_saved_secret_is_tested_without_being_typed_again(self, async_client, fake):
        await connect(async_client)
        assert (await async_client.post(f"{CONNECTION}/test", json={})).json()["ok"] is True

    async def test_the_test_names_what_is_wrong(self, async_client, fake):
        body = {"url": BASE, "client_id": CLIENT_ID, "client_secret": "wrong"}
        assert (await async_client.post(f"{CONNECTION}/test", json=body)).json()["error"] == "manyfold_credentials"
        fake.spec = openapi(raw=False)
        body["client_secret"] = CLIENT_SECRET
        too_old = (await async_client.post(f"{CONNECTION}/test", json=body)).json()
        assert (too_old["ok"], too_old["error"]) == (False, "manyfold_too_old") and "0.150" in too_old["message"]

    async def test_connecting_is_an_administrators_right(self, async_client, fake):
        headers = await limited("mf-viewer", ["manyfold:view", "manyfold:import", "settings:read"])
        assert (await async_client.get(CONNECTION, headers=headers)).status_code == 200
        body = {"url": BASE, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
        assert (await async_client.put(CONNECTION, json=body, headers=headers)).status_code == 403
        assert (await async_client.post(f"{CONNECTION}/test", json=body, headers=headers)).status_code == 403
        assert fake.requests == []


class TestCatalogue:
    async def test_status_says_whether_it_is_connected(self, async_client, fake):
        assert (await async_client.get(f"{API}/status")).json() == {
            "source": "manyfold",
            "name": "Manyfold",
            "configured": False,
            "usable": False,
            "base_url": None,
        }
        await connect(async_client)
        assert (await async_client.get(f"{API}/status")).json()["usable"] is True

    async def test_a_page_and_a_search_are_the_sources(self, async_client, fake):
        await connect(async_client)
        page = (await async_client.get(f"{API}/models")).json()
        assert (page["total"], page["pages"], page["has_next"], len(page["models"])) == (3, 2, True, 2)
        found = (await async_client.get(f"{API}/models", params={"q": "clip"})).json()
        assert [model["id"] for model in found["models"]] == [MODEL]

    async def test_a_model_shows_its_files_and_what_is_already_in_the_library(self, async_client, fake):
        await connect(async_client)
        imported = await async_client.post(f"{API}/import", json={"model_id": MODEL, "file_id": STL_FILE})
        assert imported.status_code == 200, imported.text
        model = (await async_client.get(f"{API}/models/{MODEL}")).json()
        assert (model["name"], model["creator"], model["license"], model["has_preview"]) == (
            "Cable Clip",
            "Jane Doe",
            "CC-BY-4.0",
            True,
        )
        assert model["url"] == f"{BASE}/models/{MODEL}"
        assert set(model["already_imported"]) == {STL_FILE}
        assert model["already_imported"][STL_FILE]["library_file_id"] == imported.json()["library_file_id"]

    async def test_a_sources_refusal_is_never_our_401_or_403(self, async_client, fake):
        not_connected = await async_client.get(f"{API}/models")
        assert not_connected.status_code == 409
        assert not_connected.json()["detail"]["error"] == "manyfold_not_configured"
        await connect(async_client, client_secret="wrong")
        refused = await async_client.get(f"{API}/models")
        assert (refused.status_code, refused.json()["detail"]["error"]) == (409, "manyfold_credentials")
        await connect(async_client, secret_actions={"manyfold_client_secret": "replace"})
        missing = await async_client.get(f"{API}/models/nosuchmodel0")
        assert (missing.status_code, missing.json()["detail"]["error"]) == (404, "manyfold_not_found")

    async def test_the_preview_is_an_image_fetched_by_id(self, async_client, fake):
        await connect(async_client)
        response = await async_client.get(f"{API}/models/{MODEL}/preview")
        assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
        assert response.headers["x-content-type-options"] == "nosniff"
        none = await async_client.get(f"{API}/models/b4n8tq1zk0fw/preview")
        assert none.status_code == 404


class TestRights:
    async def test_manyfold_rights_alone_are_enough(self, async_client, fake):
        """No MakerWorld permission anywhere: the source's own rights open its routes."""
        await connect(async_client)
        headers = await limited("mf-only", ["manyfold:view", "manyfold:import", "library:upload", "library:read"])
        assert (await async_client.get(f"{API}/models", headers=headers)).status_code == 200
        assert (await async_client.get(f"{API}/models/{MODEL}", headers=headers)).status_code == 200
        done = await async_client.post(f"{API}/import", json={"model_id": MODEL, "file_id": STL_FILE}, headers=headers)
        assert done.status_code == 200, done.text
        assert (await async_client.get("/api/v1/makerworld/status", headers=headers)).status_code == 403

    async def test_makerworld_rights_open_nothing_here(self, async_client, fake):
        await connect(async_client)
        headers = await limited("mw-only", ["makerworld:view", "makerworld:import", "library:upload"])
        for path in (f"{API}/status", f"{API}/models", f"{API}/models/{MODEL}", f"{API}/models/{MODEL}/preview"):
            assert (await async_client.get(path, headers=headers)).status_code == 403, path
        refused = await async_client.post(
            f"{API}/import", json={"model_id": MODEL, "file_id": STL_FILE}, headers=headers
        )
        assert refused.status_code == 403
        assert fake.requests == []

    async def test_viewing_is_not_importing(self, async_client, fake):
        await connect(async_client)
        headers = await limited("mf-view", ["manyfold:view", "library:upload"])
        refused = await async_client.post(
            f"{API}/import", json={"model_id": MODEL, "file_id": STL_FILE}, headers=headers
        )
        assert refused.status_code == 403

    async def test_importing_still_needs_the_librarys_own_upload_right(self, async_client, fake, db_session):
        await connect(async_client)
        headers = await limited("mf-no-upload", ["manyfold:view", "manyfold:import"])
        refused = await async_client.post(
            f"{API}/import", json={"model_id": MODEL, "file_id": STL_FILE}, headers=headers
        )
        assert refused.status_code == 403
        assert (await db_session.execute(select(LibraryFile))).all() == []
        assert not any("/raw/" in path for path in fake.paths())

    async def test_the_preview_takes_no_anonymous_request(self, async_client, fake):
        await connect(async_client)
        async_client.headers.pop("Authorization", None)
        response = await async_client.get(f"{API}/models/{MODEL}/preview")
        assert response.status_code == 401
        assert fake.requests == []


class TestImport:
    async def test_a_file_lands_in_the_manyfold_folder_with_its_origin(self, async_client, fake, db_session):
        await connect(async_client)
        response = await async_client.post(f"{API}/import", json={"model_id": MODEL, "file_id": SUPPORTED_FILE})
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["filename"], body["was_existing"]) == ("clip v2.stl", False)
        row = await db_session.get(LibraryFile, body["library_file_id"])
        folder = await db_session.get(LibraryFolder, row.folder_id)
        assert (row.source_type, row.file_type, row.file_size, folder.name) == (
            "manyfold",
            "stl",
            len(STL) + 8,
            "Manyfold",
        )
        assert row.source_url == f"{BASE}/models/{MODEL}#file-{SUPPORTED_FILE}"
        origin = await library_import_meta.for_file(db_session, row.id)
        assert (origin.source_type, origin.source_model_id, origin.source_file_id) == (
            "manyfold",
            MODEL,
            SUPPORTED_FILE,
        )
        assert (origin.title, origin.creator, origin.license, origin.canonical_url) == (
            "Cable Clip",
            "Jane Doe",
            "CC-BY-4.0",
            f"{BASE}/models/{MODEL}",
        )

    async def test_the_same_file_again_is_the_same_row_and_no_download(self, async_client, fake):
        await connect(async_client)
        first = (await async_client.post(f"{API}/import", json={"model_id": MODEL, "file_id": STL_FILE})).json()
        downloads = sum("/raw/" in path for path in fake.paths())
        again = (await async_client.post(f"{API}/import", json={"model_id": MODEL, "file_id": STL_FILE})).json()
        assert (again["library_file_id"], again["was_existing"]) == (first["library_file_id"], True)
        assert sum("/raw/" in path for path in fake.paths()) == downloads

    async def test_a_file_someone_else_imported_is_not_handed_to_a_user_who_cannot_see_it(self, async_client, fake):
        await connect(async_client)
        await async_client.post(f"{API}/import", json={"model_id": MODEL, "file_id": STL_FILE})
        headers = await limited("mf-own", ["manyfold:view", "manyfold:import", "library:upload", "library:read_own"])
        response = await async_client.post(
            f"{API}/import", json={"model_id": MODEL, "file_id": STL_FILE}, headers=headers
        )
        assert response.status_code in (400, 403, 404), response.text
        assert "library_file_id" not in response.text
        model = (await async_client.get(f"{API}/models/{MODEL}", headers=headers)).json()
        assert model["already_imported"] == {}

    @pytest.mark.parametrize("file_id", ["z9b0c1d2f3g4", "h5j6k7l8m9n0", "p1q2r3s4t5v6"])
    async def test_pictures_documents_and_archives_are_not_imported(self, async_client, fake, db_session, file_id):
        await connect(async_client)
        response = await async_client.post(f"{API}/import", json={"model_id": MODEL, "file_id": file_id})
        assert (response.status_code, response.json()["detail"]["error"]) == (400, "manyfold_not_importable")
        assert (await db_session.execute(select(LibraryFile))).all() == []

    async def test_a_3mf_that_is_not_a_zip_is_refused_by_the_librarys_own_check(self, async_client, fake, db_session):
        await connect(async_client)
        fake.models[MODEL]["files"]["r3s4t5v6w7x8"] = ("Plate", "model/3mf", "plate.3mf", b"not a zip at all" * 4)
        response = await async_client.post(f"{API}/import", json={"model_id": MODEL, "file_id": "r3s4t5v6w7x8"})
        assert response.status_code == 400
        assert (await db_session.execute(select(LibraryFile))).all() == []

    async def test_a_name_a_printer_card_would_refuse_is_replaced(self, async_client, fake):
        await connect(async_client)
        fake.models[MODEL]["files"][STL_FILE] = ("Clip", "model/stl", 'cl:ip?".stl', STL)
        response = await async_client.post(f"{API}/import", json={"model_id": MODEL, "file_id": STL_FILE})
        assert response.status_code == 200, response.text
        assert response.json()["filename"] == f"manyfold-{STL_FILE}.stl"

    async def test_a_file_in_object_storage_is_imported_too(self, async_client, fake, db_session):
        await connect(async_client)
        fake.storage_redirect = True
        response = await async_client.post(f"{API}/import", json={"model_id": MODEL, "file_id": STL_FILE})
        assert response.status_code == 200, response.text
        row = await db_session.get(LibraryFile, response.json()["library_file_id"])
        assert row.file_size == len(STL)

    async def test_a_connection_changed_during_the_download_publishes_nothing(self, async_client, fake, db_session):
        await connect(async_client)
        original = fake._raw

        def raw_then_switch_off(model_id, path, request):
            # An administrator disables the connection while the bytes are on their way.
            import sqlite3  # noqa: F401 - documentation of intent only

            fake.switched = True
            return original(model_id, path, request)

        fake._raw = raw_then_switch_off
        from backend.app.api.routes import model_sources

        real_load = model_sources.manyfold_config.load_config

        async def load(db):
            config = await real_load(db)
            if getattr(fake, "switched", False):
                from dataclasses import replace

                return replace(config, client_id="another-client")
            return config

        model_sources.manyfold_config.load_config = load
        try:
            response = await async_client.post(f"{API}/import", json={"model_id": MODEL, "file_id": STL_FILE})
        finally:
            model_sources.manyfold_config.load_config = real_load
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"] == "source_connection_changed"
        assert (await db_session.execute(select(LibraryFile))).all() == []

    @pytest.mark.parametrize("body", [{"model_id": "../x", "file_id": STL_FILE}, {"model_id": MODEL, "file_id": "a/b"}])
    async def test_an_id_that_is_not_an_id_is_refused(self, async_client, fake, body):
        await connect(async_client)
        response = await async_client.post(f"{API}/import", json=body)
        assert response.status_code == 404
        assert not any("/raw/" in path for path in fake.paths())

    async def test_makerworlds_import_door_does_not_import_from_manyfold(self, async_client, fake, db_session):
        await connect(async_client)
        response = await async_client.post("/api/v1/makerworld/import", json={"model_id": 1, "source_type": "manyfold"})
        assert response.status_code >= 400
        assert (await db_session.execute(select(LibraryFile))).all() == []


class TestProvenance:
    async def test_the_file_details_say_where_an_imported_file_came_from(self, async_client, fake):
        await connect(async_client)
        imported = (await async_client.post(f"{API}/import", json={"model_id": MODEL, "file_id": STL_FILE})).json()
        details = (await async_client.get(f"/api/v1/library/files/{imported['library_file_id']}")).json()
        assert details["source_type"] == "manyfold"
        assert details["source_url"] == f"{BASE}/models/{MODEL}#file-{STL_FILE}"
        assert details["import_origin"] == {
            "source_type": "manyfold",
            "title": "Cable Clip",
            "creator": "Jane Doe",
            "license": "CC-BY-4.0",
            "url": f"{BASE}/models/{MODEL}",
        }

    async def test_a_file_nobody_imported_has_no_origin(self, async_client, fake):
        upload = await async_client.post(
            "/api/v1/library/files", files={"file": ("plain.stl", STL, "application/octet-stream")}
        )
        assert upload.status_code == 200, upload.text
        details = (await async_client.get(f"/api/v1/library/files/{upload.json()['id']}")).json()
        assert details["import_origin"] is None
