"""An expired Bambu Cloud sign-in renews itself wherever it is used.

The cloud routes and MakerWorld renewed an expired access token with the stored
refresh token; the Slice dialog's preset list and the Git backup built their
own ``BambuCloudService`` without the failure handler, so they answered
"expired" (the dialog) or silently collected nothing (the backup) while a
working refresh token sat in the database. Both now go through the credential
seam's renewal, with its rules: a renewal that could not reach Bambu writes
nothing, and only a refusal marks the sign-in dead.

Bambu Cloud is replaced below the HTTP client with hand-built answers.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from backend.app.api.routes import slicer_presets
from backend.app.models.user import User
from backend.app.services import bambu_cloud
from backend.app.services.git_backup import GitBackupService
from backend.tests.integration.test_cloud_unreachable import EXPIRED, REFRESH, SETTINGS, FakeBambu, _down

pytestmark = pytest.mark.asyncio

LISTING = {
    "filament": {"private": [{"setting_id": "PFUS1", "name": "Mine PLA"}], "public": []},
    "printer": {"private": [], "public": []},
    "print": {"private": [], "public": []},
}
RENEWED_PAIR = (200, {"accessToken": "acc-2", "refreshToken": "ref-2"})


@pytest.fixture
def bambu(monkeypatch):
    def install(answers: dict) -> FakeBambu:
        fake = FakeBambu(answers)
        monkeypatch.setattr(bambu_cloud, "_shared_http_client", httpx.AsyncClient(transport=httpx.MockTransport(fake)))
        bambu_cloud.invalidate_validation_cache()
        slicer_presets._cloud_cache.clear()
        return fake

    yield install
    bambu_cloud.invalidate_validation_cache()
    slicer_presets._cloud_cache.clear()


async def _sign_in(db_session, *, refresh: str | None = "ref") -> User:
    admin = (await db_session.execute(select(User).where(User.username == "test_admin"))).scalar_one()
    admin.cloud_token = "acc"
    admin.cloud_refresh_token = refresh
    admin.cloud_email = "me@example.com"
    admin.cloud_region = "global"
    await db_session.commit()
    return admin


async def _stored(db_session, user_id: int) -> tuple[str | None, str | None, bool]:
    db_session.expire_all()
    row = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    return row.cloud_token, row.cloud_refresh_token, row.cloud_token_invalid_at is not None


class TestTheSliceDialogsPresetList:
    async def test_an_expired_token_is_renewed_and_the_list_arrives(self, async_client, db_session, bambu):
        user = await _sign_in(db_session)
        fake = bambu({("GET", SETTINGS): [EXPIRED, (200, LISTING)], ("POST", REFRESH): RENEWED_PAIR})

        slots, status = await slicer_presets._fetch_cloud_presets(db_session, user)

        assert status == "ok"
        assert [p.name for p in slots["filament"]] == ["Mine PLA"]
        assert fake.bearers("GET", SETTINGS) == ["Bearer acc", "Bearer acc-2"]
        assert await _stored(db_session, user.id) == ("acc-2", "ref-2", False)

    async def test_a_refused_renewal_is_an_expired_sign_in(self, async_client, db_session, bambu):
        user = await _sign_in(db_session)
        bambu({("GET", SETTINGS): EXPIRED, ("POST", REFRESH): (401, {"error": "Please login."})})

        slots, status = await slicer_presets._fetch_cloud_presets(db_session, user)

        assert status == "expired"
        assert slots["filament"] == []
        assert await _stored(db_session, user.id) == ("acc", "ref", True)

    async def test_a_renewal_that_could_not_reach_bambu_marks_nothing(self, async_client, db_session, bambu):
        user = await _sign_in(db_session)
        bambu({("GET", SETTINGS): EXPIRED, ("POST", REFRESH): _down})

        _slots, status = await slicer_presets._fetch_cloud_presets(db_session, user)

        assert status == "unreachable"
        assert await _stored(db_session, user.id) == ("acc", "ref", False)

    async def test_no_database_transaction_is_held_while_bambu_is_asked(self, async_client, db_session, bambu):
        """Bambu may take 30 s to answer; the request's connection is not kept
        in a transaction meanwhile (freeze what is needed, finish, then call)."""
        user = await _sign_in(db_session)
        during: list[bool] = []

        def listing(_request):
            during.append(db_session.in_transaction())
            return httpx.Response(200, json=LISTING)

        bambu({("GET", SETTINGS): listing})

        _slots, status = await slicer_presets._fetch_cloud_presets(db_session, user)

        assert status == "ok"
        assert during == [False]


class TestTheGitBackup:
    async def test_an_expired_token_is_renewed_and_the_presets_are_collected(self, async_client, db_session, bambu):
        user = await _sign_in(db_session)
        fake = bambu({("GET", SETTINGS): [EXPIRED, (200, LISTING)], ("POST", REFRESH): RENEWED_PAIR})
        files: dict = {}

        await GitBackupService()._collect_cloud_profiles(db_session, files)

        assert files["cloud_profiles/filament.json"]["profiles"] == LISTING["filament"]["private"]
        assert fake.count("POST", REFRESH) == 1
        assert await _stored(db_session, user.id) == ("acc-2", "ref-2", False)

    @pytest.mark.parametrize(
        "failure",
        [_down, (503, {"error": "upstream"}), httpx.Response(502, text="<html>Bad gateway</html>")],
        ids=["network", "503", "502-html"],
    )
    async def test_a_network_failure_never_marks_the_sign_in_invalid(self, async_client, db_session, bambu, failure):
        user = await _sign_in(db_session)
        bambu({("GET", SETTINGS): failure, ("POST", REFRESH): failure})
        files: dict = {}

        await GitBackupService()._collect_cloud_profiles(db_session, files)

        assert files == {}
        assert await _stored(db_session, user.id) == ("acc", "ref", False)

    async def test_an_expiry_whose_renewal_could_not_reach_bambu_marks_nothing(self, async_client, db_session, bambu):
        user = await _sign_in(db_session)
        bambu({("GET", SETTINGS): EXPIRED, ("POST", REFRESH): _down})
        files: dict = {}

        await GitBackupService()._collect_cloud_profiles(db_session, files)

        assert files == {}
        assert await _stored(db_session, user.id) == ("acc", "ref", False)


class TestTheOwnerlessCredential:
    """The installation-level sign-in lives in ``Settings`` rows under its own
    keys; a renewed pair must land in those rows, not under ``User`` column names."""

    KEYS = ("bambu_cloud_token", "bambu_cloud_refresh_token", "bambu_cloud_token_invalid_at")

    async def _sign_in_ownerless(self, db_session, *, invalid: str | None = None) -> None:
        from backend.app.models.settings import Settings

        db_session.add_all(
            [
                Settings(key="bambu_cloud_token", value="acc"),
                Settings(key="bambu_cloud_refresh_token", value="ref"),
                Settings(key="bambu_cloud_region", value="global"),
            ]
        )
        if invalid is not None:
            db_session.add(Settings(key="bambu_cloud_token_invalid_at", value=invalid))
        await db_session.commit()

    async def _rows(self, db_session) -> dict:
        from backend.app.models.settings import Settings

        db_session.expire_all()
        rows = (await db_session.execute(select(Settings))).scalars().all()
        return {r.key: r.value for r in rows}

    async def test_a_renewed_pair_is_stored_under_the_ownerless_keys(self, async_client, db_session, bambu):
        from backend.app.services.bambu_cloud_credentials import is_cloud_token_invalid

        await self._sign_in_ownerless(db_session)
        fake = bambu({("GET", SETTINGS): [EXPIRED, (200, LISTING)], ("POST", REFRESH): RENEWED_PAIR})
        files: dict = {}

        await GitBackupService()._collect_cloud_profiles(db_session, files)

        assert files["cloud_profiles/filament.json"]["profiles"] == LISTING["filament"]["private"]
        assert fake.count("POST", REFRESH) == 1
        rows = await self._rows(db_session)
        assert rows["bambu_cloud_token"] == "acc-2"
        assert rows["bambu_cloud_refresh_token"] == "ref-2"
        assert not await is_cloud_token_invalid(db_session, None)
        # Nothing is written under the User column names.
        assert not {"cloud_token", "cloud_refresh_token", "cloud_token_invalid_at"} & set(rows)

    async def test_a_renewal_clears_an_earlier_invalid_mark(self, async_client, db_session, bambu):
        from backend.app.services.bambu_cloud import BambuCloudService
        from backend.app.services.bambu_cloud_credentials import (
            is_cloud_token_invalid,
            renew_for_service,
            renewal_credentials,
        )

        await self._sign_in_ownerless(db_session, invalid="2026-01-01T00:00:00+00:00")
        bambu({("POST", REFRESH): RENEWED_PAIR})
        captured, refresh = await renewal_credentials(db_session, None, allow_global=True)
        await db_session.commit()
        cloud = BambuCloudService(region="global")
        cloud.set_token("acc")
        cloud.set_refresh_token(refresh)

        outcome = await renew_for_service(cloud, captured)

        assert outcome == bambu_cloud.RENEWED
        rows = await self._rows(db_session)
        assert (rows["bambu_cloud_token"], rows["bambu_cloud_refresh_token"]) == ("acc-2", "ref-2")
        assert not await is_cloud_token_invalid(db_session, None)

    async def test_a_refused_renewal_marks_the_ownerless_sign_in_invalid(self, async_client, db_session, bambu):
        from backend.app.services.bambu_cloud_credentials import is_cloud_token_invalid

        await self._sign_in_ownerless(db_session)
        bambu({("GET", SETTINGS): EXPIRED, ("POST", REFRESH): (401, {"error": "Please login."})})

        await GitBackupService()._collect_cloud_profiles(db_session, {})

        rows = await self._rows(db_session)
        assert (rows["bambu_cloud_token"], rows["bambu_cloud_refresh_token"]) == ("acc", "ref")
        assert await is_cloud_token_invalid(db_session, None)


class TestTheUserCredential:
    async def test_a_renewal_clears_an_earlier_invalid_mark(self, async_client, db_session, bambu):
        from datetime import datetime, timezone

        from backend.app.services.bambu_cloud import BambuCloudService
        from backend.app.services.bambu_cloud_credentials import renew_for_service, renewal_credentials

        user = await _sign_in(db_session)
        user.cloud_token_invalid_at = datetime.now(timezone.utc)
        await db_session.commit()
        bambu({("POST", REFRESH): RENEWED_PAIR})
        captured, refresh = await renewal_credentials(db_session, user)
        await db_session.commit()
        cloud = BambuCloudService(region="global")
        cloud.set_token("acc")
        cloud.set_refresh_token(refresh)

        assert await renew_for_service(cloud, captured) == bambu_cloud.RENEWED
        assert await _stored(db_session, user.id) == ("acc-2", "ref-2", False)


@pytest.mark.parametrize("path", ["/api/v1/slicer/presets", "/api/v1/cloud/settings"], ids=["slice", "profiles"])
@pytest.mark.parametrize("empty_type", ["filament", "printer", "print"])
async def test_null_cloud_preset_types_and_tiers_keep_slice_and_profiles_available(
    async_client, db_session, bambu, path, empty_type
):
    from backend.app.models.local_preset import LocalPreset

    await _sign_in(db_session)
    db_session.add(LocalPreset(name="Local PLA", preset_type="filament", setting="{}"))
    await db_session.commit()

    listing = {
        key: {
            "private": [{"setting_id": f"own-{key}", "name": f"My {key}"}] if key != "print" else None,
            "public": [{"setting_id": "stock-process", "name": "Stock process"}] if key == "print" else None,
        }
        for key in ("filament", "printer", "print")
    }
    listing[empty_type] = None
    bambu({("GET", SETTINGS): (200, listing)})

    response = await async_client.get(path)
    assert response.status_code == 200, response.text
    presets = response.json()
    if path == "/api/v1/slicer/presets":
        assert presets["cloud_status"] == "ok"
        assert [preset["name"] for preset in presets["local"]["filament"]] == ["Local PLA"]
        presets = presets["cloud"]

    expected = {"filament": ["My filament"], "printer": ["My printer"], "print": ["Stock process"]}
    expected[empty_type] = []
    for cloud_type, slot in (("filament", "filament"), ("printer", "printer"), ("print", "process")):
        assert [preset["name"] for preset in presets[slot]] == expected[cloud_type]


async def test_stock_process_classification_uses_cloud_public_list_and_same_cache(
    async_client, db_session, bambu, monkeypatch
):
    from backend.app.schemas.slicer import PresetRef

    monkeypatch.setattr(slicer_presets, "_cloud_cache", {})
    user = await _sign_in(db_session)
    listing = {
        "print": {
            "public": [{"setting_id": "public", "name": "Stock Process"}],
            "private": [{"setting_id": "private", "name": "Own Process"}],
        },
    }
    fake = bambu({("GET", SETTINGS): (200, listing)})
    assert await slicer_presets.is_stock_bambu_process(db_session, user, PresetRef(source="cloud", id="public"))
    assert not await slicer_presets.is_stock_bambu_process(db_session, user, PresetRef(source="cloud", id="private"))
    assert not await slicer_presets.is_stock_bambu_process(db_session, user, PresetRef(source="cloud", id="unknown"))
    assert fake.count("GET", SETTINGS) == 1


async def test_stock_process_lookup_does_not_renew_a_slices_credential(async_client, db_session, bambu, monkeypatch):
    from backend.app.schemas.slicer import PresetRef

    monkeypatch.setattr(slicer_presets, "_cloud_cache", {})
    user = await _sign_in(db_session)
    fake = bambu({("GET", SETTINGS): EXPIRED, ("POST", REFRESH): RENEWED_PAIR})
    assert not await slicer_presets.is_stock_bambu_process(db_session, user, PresetRef(source="cloud", id="public"))
    assert fake.count("POST", REFRESH) == 0
    assert await _stored(db_session, user.id) == ("acc", "ref", False)
