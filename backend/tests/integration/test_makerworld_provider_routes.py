"""The /makerworld/* routes go through the model-provider registry (upstream #2845).

What these pin, beyond the route-level tests in ``unit/test_makerworld_routes.py``:
the status reports an expired sign-in as its own state; the registry decides
which provider a URL or ``source_type`` belongs to, and an unknown one is
refused before anything is written; a provider whose permission differs from
the route's is checked on top of it; one import makes one design request and
still writes its meta row and cover; a legacy whole-model row keeps working;
and an API key's owner is the identity the provider is built for.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import select

from backend.app.core.permissions import Permission
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.library_file_makerworld_meta import LibraryFileMakerworldMeta
from backend.app.models.user import User
from backend.app.services.model_providers import makerworld_provider, registry
from backend.app.services.model_providers.base import ModelProvider, ProviderUnavailableError
from backend.app.services.model_providers.makerworld.service import MakerWorldService

pytestmark = pytest.mark.asyncio

_DESIGN = {
    "id": 1400373,
    "modelId": "US2bb73b106683e5",
    "title": "Seed Starter",
    "coverUrl": "https://makerworld.bblmw.com/covers/design.png",
    "instances": [{"profileId": 298919107, "title": "9 cells"}],
}
_INSTANCES = {"hits": [{"profileId": 298919107, "title": "9 cells", "cover": None}]}


class _FakeMakerWorld(MakerWorldService):
    """The real service — ``resolve`` / ``get_download`` / ``download`` run as
    shipped — with MakerWorld itself replaced below ``_get_json``."""

    def __init__(self, *, design=None, instances=None, **kwargs):
        super().__init__(client=MagicMock(spec=httpx.AsyncClient), auth_token="tok", **kwargs)
        self.design = design or _DESIGN
        self.instances = instances or _INSTANCES
        self.json_paths: list[str] = []
        self.profile_download_calls: list[tuple[int, str]] = []

    async def _get_json(self, path):
        self.json_paths.append(path)
        return self.instances if path.endswith("/instances") else self.design

    async def get_profile_download(self, profile_id, model_id):
        self.profile_download_calls.append((profile_id, model_id))
        return {"name": "benchy.3mf", "url": "https://makerworld.bblmw.com/signed/f.3mf"}

    async def download_3mf(self, signed_url):
        return b"PK\x03\x04not-a-real-3mf", "f.3mf"

    async def fetch_thumbnail(self, url):
        return b"\x89PNG\r\n\x1a\nfake", "image/png"


async def _admin(db_session) -> User:
    return (await db_session.execute(select(User).where(User.username == "test_admin"))).scalar_one()


async def _create_key(async_client, **scopes) -> dict:
    resp = await async_client.post("/api/v1/api-keys/", json={"name": f"mw-{sorted(scopes.items())}", **scopes})
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestStatus:
    async def test_a_rejected_token_is_reported_as_an_expired_sign_in(self, async_client, db_session):
        admin = await _admin(db_session)
        admin.cloud_token = "tok"
        admin.cloud_token_invalid_at = datetime.now(timezone.utc)
        await db_session.commit()

        resp = await async_client.get("/api/v1/makerworld/status")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"has_cloud_token": True, "can_download": False, "sign_in_expired": True}

    async def test_a_live_token_can_download(self, async_client, db_session):
        admin = await _admin(db_session)
        admin.cloud_token = "tok"
        await db_session.commit()

        resp = await async_client.get("/api/v1/makerworld/status")

        assert resp.json() == {"has_cloud_token": True, "can_download": True, "sign_in_expired": False}

    async def test_no_token_is_not_an_expired_sign_in(self, async_client):
        resp = await async_client.get("/api/v1/makerworld/status")

        assert resp.json() == {"has_cloud_token": False, "can_download": False, "sign_in_expired": False}


class TestRegistryRouting:
    async def test_import_of_an_unknown_source_type_refuses_before_making_a_folder(self, async_client, db_session):
        resp = await async_client.post("/api/v1/makerworld/import", json={"model_id": 1, "source_type": "thingiverse"})

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "No model provider registered for source_type 'thingiverse'"
        folders = (await db_session.execute(select(LibraryFolder).where(LibraryFolder.name == "MakerWorld"))).all()
        assert folders == []

    async def test_resolve_of_a_host_nobody_serves_is_a_400(self, async_client):
        resp = await async_client.post("/api/v1/makerworld/resolve", json={"url": "https://www.printables.com/model/1"})

        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == (
            "This link is not from a supported model site: 'https://www.printables.com/model/1'"
        )

    @pytest.mark.parametrize(
        "url",
        ["http://169.254.169.254/latest/meta-data/x.jpg", "http://127.0.0.1/x.jpg", "https://evil.example/x.png"],
    )
    async def test_the_thumbnail_proxy_refuses_other_hosts(self, async_client, monkeypatch, url):
        outbound = MagicMock(spec=httpx.AsyncClient)
        monkeypatch.setattr("backend.app.services.model_providers.makerworld.service._shared_http_client", outbound)

        resp = await async_client.get("/api/v1/makerworld/thumbnail", params={"url": url})

        assert resp.status_code == 400, resp.text
        assert "non-MakerWorld host" in resp.json()["detail"]
        outbound.get.assert_not_called()


class _GatedProvider(ModelProvider):
    """A second site whose import needs a permission no API key can hold."""

    source_type = "gated"
    display_name = "Gated"
    host_patterns = ("gated.example",)
    default_folder_name = None
    view_permission = Permission.MAKERWORLD_VIEW
    import_permission = Permission.LIBRARY_PURGE

    async def build_service(self, *, db, user, api_key_owner=None, client=None):
        class _Down(_FakeMakerWorld):
            async def get_download(self, ref):
                raise ProviderUnavailableError("gated site is down")

        return _Down()

    def parse_url(self, url):
        raise NotImplementedError

    def canonical_url(self, ref):
        return f"https://gated.example/models/{ref.external_id}"


@pytest.fixture
def gated_provider():
    provider = _GatedProvider()
    registry.register(provider)
    yield provider
    registry._providers.pop(provider.source_type, None)


class TestProviderPermission:
    async def test_a_providers_own_permission_is_checked_on_top_of_the_route(self, async_client, gated_provider):
        """``makerworld:import`` opens the route; the gated site's import needs
        ``library:purge`` too, which no API key may hold."""
        body = await _create_key(async_client, can_manage_library=True)

        admin_resp = await async_client.post("/api/v1/makerworld/import", json={"model_id": 1, "source_type": "gated"})
        del async_client.headers["Authorization"]
        key_resp = await async_client.post(
            "/api/v1/makerworld/import",
            headers={"X-API-Key": body["key"]},
            json={"model_id": 1, "source_type": "gated"},
        )

        # The admin passed the provider's gate and reached its (down) service.
        assert admin_resp.status_code == 502, admin_resp.text
        assert key_resp.status_code == 403, key_resp.text


class TestImport:
    async def test_one_design_request_serves_the_download_and_the_meta_row(self, async_client, db_session, monkeypatch):
        fake = _FakeMakerWorld()

        async def build_service(**_kwargs):
            return fake

        monkeypatch.setattr(makerworld_provider, "build_service", build_service)

        resp = await async_client.post("/api/v1/makerworld/import", json={"model_id": 1400373})

        assert resp.status_code == 200, resp.text
        assert resp.json()["profile_id"] == 298919107
        assert fake.json_paths.count("/design/1400373") == 1
        meta = (
            await db_session.execute(
                select(LibraryFileMakerworldMeta).where(
                    LibraryFileMakerworldMeta.library_file_id == resp.json()["library_file_id"]
                )
            )
        ).scalar_one()
        assert meta.title == "Seed Starter"
        assert meta.cover_path


class TestLegacyWholeModelRow:
    async def test_resolve_files_it_under_bucket_zero(self, async_client, db_session, monkeypatch):
        row = LibraryFile(
            filename="old.3mf",
            file_path="library/files/old.3mf",
            file_type="3mf",
            file_size=1,
            source_type="makerworld",
            source_url="https://makerworld.com/models/1400373",
        )
        db_session.add(row)
        await db_session.commit()

        async def build_service(**_kwargs):
            return _FakeMakerWorld()

        monkeypatch.setattr(makerworld_provider, "build_service", build_service)

        resp = await async_client.post(
            "/api/v1/makerworld/resolve", json={"url": "https://makerworld.com/models/1400373"}
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["already_imported_by_profile_id"]["0"]["library_file_id"] == row.id
        assert resp.json()["already_imported_library_ids"] == [row.id]

    async def test_redownload_takes_the_designs_first_plate(self, async_client, db_session, monkeypatch, tmp_path):
        on_disk = tmp_path / "library" / "files" / "old.3mf"
        on_disk.parent.mkdir(parents=True)
        on_disk.write_bytes(b"old")
        row = LibraryFile(
            filename="old.3mf",
            file_path="library/files/old.3mf",
            file_type="3mf",
            file_size=3,
            source_type="makerworld",
            source_url="https://makerworld.com/models/1400373",
        )
        db_session.add(row)
        await db_session.commit()
        fake = _FakeMakerWorld()

        async def build_service(**_kwargs):
            return fake

        monkeypatch.setattr(makerworld_provider, "build_service", build_service)
        monkeypatch.setattr("backend.app.core.config.settings.base_dir", tmp_path)

        resp = await async_client.post(f"/api/v1/makerworld/imports/{row.id}/redownload")

        assert resp.status_code == 200, resp.text
        assert resp.json()["profile_id"] == 298919107
        assert fake.profile_download_calls == [(298919107, "US2bb73b106683e5")]


class TestApiKeyOwner:
    async def test_status_reads_the_key_owners_token(self, async_client, db_session):
        body = await _create_key(async_client, can_access_cloud=True)
        owner = (await db_session.execute(select(User).where(User.id == body["user_id"]))).scalar_one()
        owner.cloud_token = "owner-tok"
        await db_session.commit()

        del async_client.headers["Authorization"]
        resp = await async_client.get("/api/v1/makerworld/status", headers={"X-API-Key": body["key"]})

        assert resp.status_code == 200, resp.text
        assert resp.json()["has_cloud_token"] is True

    async def test_import_builds_the_provider_for_the_key_owner(self, async_client, db_session, monkeypatch):
        body = await _create_key(async_client, can_access_cloud=True)
        seen: dict = {}

        async def build_service(**kwargs):
            seen.update(kwargs)
            return _FakeMakerWorld()

        monkeypatch.setattr(makerworld_provider, "build_service", build_service)

        del async_client.headers["Authorization"]
        resp = await async_client.post(
            "/api/v1/makerworld/import",
            headers={"X-API-Key": body["key"]},
            json={"model_id": 1400373, "profile_id": 298919107},
        )

        assert resp.status_code == 200, resp.text
        assert seen["user"] is None
        assert seen["api_key_owner"].id == body["user_id"]
        row = (
            await db_session.execute(select(LibraryFile).where(LibraryFile.id == resp.json()["library_file_id"]))
        ).scalar_one()
        assert row.created_by_id == body["user_id"]
