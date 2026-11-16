"""One renewal at a time per Bambu Cloud credential.

Every consumer of the stored sign-in renews an expired access token, and the
Slice dialog fires several of them at once — each with the same refresh token.
Bambu honours a refresh token once: the callers that lost the race used to be
refused, answered "expired" and tried to mark invalid a sign-in another caller
had renewed a moment earlier. Renewal is now serialised per credential: the
first caller renews, the others wait and use the pair it stored.

Bambu Cloud is replaced below the HTTP client with hand-built answers; its
refresh endpoint is single-use and slow enough for the callers to overlap.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import select

from backend.app.models.user import User
from backend.app.services import bambu_cloud
from backend.app.services.bambu_cloud import BambuCloudAuthError, BambuCloudService
from backend.app.services.bambu_cloud_credentials import (
    is_cloud_token_invalid,
    renew_for_service,
    renew_or_invalidate,
    renewal_credentials,
)
from backend.tests.integration.test_cloud_unreachable import REFRESH, SETTINGS

pytestmark = pytest.mark.asyncio

LISTING = {"filament": {"private": [{"setting_id": "PFUS1", "name": "Mine PLA"}], "public": []}}


class SingleUseBambu:
    """``acc`` is expired; refresh token ``ref`` buys ``acc-2``/``ref-2`` once."""

    def __init__(self, *, refuse: bool = False):
        self.refuse = refuse
        self.spent: set[str] = set()
        self.refreshes: list[str] = []
        self.bearers: list[str | None] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == REFRESH:
            import json

            token = json.loads(request.content)["refreshToken"]
            self.refreshes.append(token)
            await asyncio.sleep(0.05)  # the callers overlap here
            if self.refuse or token in self.spent or token != "ref":
                return httpx.Response(401, json={"error": "Please login."})
            self.spent.add(token)
            return httpx.Response(200, json={"accessToken": "acc-2", "refreshToken": "ref-2"})
        assert request.url.path == SETTINGS, request.url.path
        bearer = request.headers.get("authorization")
        self.bearers.append(bearer)
        await asyncio.sleep(0)
        if bearer == "Bearer acc-2":
            return httpx.Response(200, json=LISTING)
        return httpx.Response(401, json={"code": 4, "error": "Please login.", "message": ""})


@pytest.fixture
def bambu(monkeypatch):
    def install(**kwargs) -> SingleUseBambu:
        fake = SingleUseBambu(**kwargs)
        monkeypatch.setattr(bambu_cloud, "_shared_http_client", httpx.AsyncClient(transport=httpx.MockTransport(fake)))
        bambu_cloud.invalidate_validation_cache()
        return fake

    yield install
    bambu_cloud.invalidate_validation_cache()


async def _sign_in(db_session) -> User:
    admin = (await db_session.execute(select(User).where(User.username == "test_admin"))).scalar_one()
    admin.cloud_token = "acc"
    admin.cloud_refresh_token = "ref"
    admin.cloud_email = "me@example.com"
    admin.cloud_region = "global"
    await db_session.commit()
    return admin


async def _stored(db_session, user_id: int) -> tuple[str | None, str | None, bool]:
    db_session.expire_all()
    row = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    return row.cloud_token, row.cloud_refresh_token, row.cloud_token_invalid_at is not None


async def _services(db_session, user: User, count: int) -> list[BambuCloudService]:
    """``count`` independent consumers, each loaded with the same stored pair."""
    built = []
    for _ in range(count):
        captured, refresh = await renewal_credentials(db_session, user)
        cloud = BambuCloudService(region="global")
        cloud._on_auth_failure = lambda cloud=cloud, captured=captured: renew_for_service(cloud, captured)
        cloud.set_token("acc")
        cloud.set_refresh_token(refresh)
        built.append(cloud)
    await db_session.commit()
    return built


class TestConcurrentCallersOfAUserCredential:
    async def test_the_refresh_token_is_spent_once_and_every_caller_succeeds(self, async_client, db_session, bambu):
        user = await _sign_in(db_session)
        fake = bambu()
        clouds = await _services(db_session, user, 4)

        answers = await asyncio.gather(*(c.get_slicer_settings() for c in clouds), return_exceptions=True)

        assert answers == [LISTING] * 4
        assert fake.refreshes == ["ref"]
        assert await _stored(db_session, user.id) == ("acc-2", "ref-2", False)
        # Each caller holds the stored pair afterwards, not the spent one.
        assert {(c.access_token, c.refresh_token) for c in clouds} == {("acc-2", "ref-2")}

    async def test_a_caller_without_a_service_of_its_own_uses_the_renewed_pair(self, async_client, db_session, bambu):
        """MakerWorld's shape: the seam builds the service from the stored pair."""
        user = await _sign_in(db_session)
        fake = bambu()
        (cloud,) = await _services(db_session, user, 1)
        captured, _refresh = await renewal_credentials(db_session, user)
        await db_session.commit()

        listing, answer = await asyncio.gather(cloud.get_slicer_settings(), renew_or_invalidate(captured))

        assert listing == LISTING
        outcome, token, stored = answer
        assert (outcome, token) == (bambu_cloud.RENEWED, "acc-2")
        assert stored is not None and stored.generation == captured.generation + 1
        assert fake.refreshes == ["ref"]
        assert await _stored(db_session, user.id) == ("acc-2", "ref-2", False)

    async def test_a_refused_renewal_asks_bambu_once_and_marks_invalid(self, async_client, db_session, bambu):
        user = await _sign_in(db_session)
        fake = bambu(refuse=True)
        clouds = await _services(db_session, user, 3)

        answers = await asyncio.gather(*(c.get_slicer_settings() for c in clouds), return_exceptions=True)

        assert all(isinstance(a, BambuCloudAuthError) for a in answers), answers
        assert fake.refreshes == ["ref"]
        assert await _stored(db_session, user.id) == ("acc", "ref", True)


class TestConcurrentCallersOfTheOwnerlessCredential:
    async def test_the_refresh_token_is_spent_once_and_every_caller_succeeds(self, async_client, db_session, bambu):
        from backend.app.models.settings import Settings

        db_session.add_all(
            [
                Settings(key="bambu_cloud_token", value="acc"),
                Settings(key="bambu_cloud_refresh_token", value="ref"),
                Settings(key="bambu_cloud_region", value="global"),
            ]
        )
        await db_session.commit()
        fake = bambu()
        clouds = []
        for _ in range(3):
            captured, refresh = await renewal_credentials(db_session, None, allow_global=True)
            cloud = BambuCloudService(region="global")
            cloud._on_auth_failure = lambda cloud=cloud, captured=captured: renew_for_service(cloud, captured)
            cloud.set_token("acc")
            cloud.set_refresh_token(refresh)
            clouds.append(cloud)
        await db_session.commit()

        answers = await asyncio.gather(*(c.get_slicer_settings() for c in clouds), return_exceptions=True)

        assert answers == [LISTING] * 3
        assert fake.refreshes == ["ref"]
        db_session.expire_all()
        rows = {r.key: r.value for r in (await db_session.execute(select(Settings))).scalars().all()}
        assert (rows["bambu_cloud_token"], rows["bambu_cloud_refresh_token"]) == ("acc-2", "ref-2")
        assert not await is_cloud_token_invalid(db_session, None)
