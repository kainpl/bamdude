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
