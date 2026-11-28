"""The NFC integration doors take only a tray UUID a Bambu tag can carry (upstream 12e543b1b, shared part).

A reader that took the filament type for the UUID (SpoolBuddy before upstream #984 read tag
blocks 4-5) sends one value for every spool of that type. Looked up, it would find another
spool of the type; linked, it would give them all one id. The built-in lookup and link and
Spoolman's tag link drop such a value and go by the tag UID; with no tag UID beside it the
link is refused before anything is written.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from backend.app.models.spool import Spool
from backend.tests.integration.test_tag_already_linked import _client, _spoolman_spool

FILAMENT_TYPE = "504C41204D6174746500000000000000"  # "PLA Matte" as hex, what such a reader sends
REAL = "9E0B0717BEE94D7887EB1D8DFD1A14F3"  # an AMS-reported tray UUID (v4)
TAG = "AABBCCDDEEFF0011"


async def _spool(db, **kw) -> Spool:
    spool = Spool(material="PLA", label_weight=1000, core_weight=250, weight_used=0, **kw)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    return spool


@pytest.mark.asyncio
@pytest.mark.integration
class TestBuiltIn:
    async def test_a_lookup_by_a_filament_type_alone_finds_nothing(self, async_client: AsyncClient, db_session):
        await _spool(db_session, tray_uuid=FILAMENT_TYPE)

        response = await async_client.get("/api/v1/inventory/spools/by-tag", params={"tray_uuid": FILAMENT_TYPE})

        assert response.status_code == 404, response.text

    async def test_a_lookup_with_a_tag_uid_beside_it_goes_by_the_tag(self, async_client: AsyncClient, db_session):
        await _spool(db_session, tray_uuid=FILAMENT_TYPE)
        tagged = await _spool(db_session, tag_uid=TAG)

        response = await async_client.get(
            "/api/v1/inventory/spools/by-tag", params={"tray_uuid": FILAMENT_TYPE, "tag_uid": TAG}
        )

        assert response.status_code == 200, response.text
        assert response.json()["id"] == tagged.id

    async def test_a_real_tray_uuid_still_finds_its_spool(self, async_client: AsyncClient, db_session):
        spool = await _spool(db_session, tray_uuid=REAL)

        response = await async_client.get("/api/v1/inventory/spools/by-tag", params={"tray_uuid": REAL.lower()})

        assert response.status_code == 200, response.text
        assert response.json()["id"] == spool.id

    async def test_a_link_of_a_filament_type_alone_is_refused(self, async_client: AsyncClient, db_session):
        spool = await _spool(db_session)

        response = await async_client.patch(
            f"/api/v1/inventory/spools/{spool.id}/link-tag", json={"tray_uuid": FILAMENT_TYPE}
        )

        assert response.status_code == 422, response.text
        await db_session.refresh(spool)
        assert spool.tray_uuid is None

    async def test_a_link_with_a_tag_uid_links_the_tag_and_keeps_the_spools_uuid(
        self, async_client: AsyncClient, db_session
    ):
        spool = await _spool(db_session, tray_uuid=REAL)

        response = await async_client.patch(
            f"/api/v1/inventory/spools/{spool.id}/link-tag", json={"tag_uid": TAG, "tray_uuid": FILAMENT_TYPE}
        )

        assert response.status_code == 200, response.text
        await db_session.refresh(spool)
        assert (spool.tag_uid, spool.tray_uuid) == (TAG, REAL)


@pytest.mark.asyncio
@pytest.mark.integration
class TestSpoolman:
    async def test_a_link_of_a_filament_type_alone_is_refused_before_spoolman(self, async_client: AsyncClient):
        client = _client([])
        with patch("backend.app.api.routes.spoolman_inventory._get_client", new=AsyncMock(return_value=client)):
            response = await async_client.patch(
                "/api/v1/spoolman/inventory/spools/99/tag", json={"tray_uuid": FILAMENT_TYPE}
            )

        assert response.status_code == 422, response.text
        client.get_all_spools.assert_not_awaited()

    async def test_a_link_with_a_tag_uid_writes_the_tag_uid(self, async_client: AsyncClient):
        client = _client([])
        client.update_spool_full = AsyncMock(return_value=_spoolman_spool(99, {"tag": json.dumps(TAG)}))
        with (
            patch("backend.app.api.routes.spoolman_inventory._get_client", new=AsyncMock(return_value=client)),
            patch("backend.app.core.websocket.ws_manager.broadcast", new_callable=AsyncMock),
        ):
            response = await async_client.patch(
                "/api/v1/spoolman/inventory/spools/99/tag", json={"tag_uid": TAG, "tray_uuid": FILAMENT_TYPE}
            )

        assert response.status_code == 200, response.text
        written = client.update_spool_full.await_args.kwargs["extra"]["tag"]
        assert json.loads(written) == TAG
